"""
importers.py - turn non-EPUB books into EPUB so the rest of Xalibre works unchanged.

Supported inputs
  Text   : .txt  .fb2  .fb2.zip  .fbz
  Kindle : .mobi .azw .azw3 .prc   (Calibre's ebook-convert if installed, else the optional `mobi` package)
  Comics : .cbz  .cbr  .cb7        (stored as a fixed-layout "comic EPUB" with the original page images;
                                    page cropping / splitting / dithering happen later in the renderer)
  PDF    : .pdf                    (stored as a fixed-layout "comic EPUB" with rendered page images)

Public API
  SUPPORTED_EXTS, is_importable(path), file_dialog_types()
  convert_to_epub(src, work_dir, progress=None) -> dict(epub_path, title, author, description, cover, is_comic)
  read_epub_light(epub_path)  -> metadata + cover WITHOUT loading the whole EPUB into memory
  ImportProblem               -> raised with a human-readable message
"""
import base64
from collections import Counter
import datetime
import html
import io
import json
import locale
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import uuid
import zipfile
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as _esc, quoteattr as _qa

from PIL import Image

try:
    import fitz
except ImportError:
    fitz = None

COMIC_MARKER = "xalibre-comic.json"

TEXT_EXTS = (".txt", ".fb2", ".fbz")
KINDLE_EXTS = (".mobi", ".azw", ".azw3", ".prc")
COMIC_EXTS = (".cbz", ".cbr", ".cb7")
PDF_EXTS = (".pdf",)
SUPPORTED_EXTS = TEXT_EXTS + KINDLE_EXTS + COMIC_EXTS + PDF_EXTS + (".epub",)


class ImportProblem(Exception):
    """Raised with a message that is safe to show to the user."""


def _lower_name(path):
    return os.path.basename(path).lower()


def is_importable(path):
    n = _lower_name(path)
    return n.endswith(SUPPORTED_EXTS) or n.endswith(".fb2.zip")


def needs_conversion(path):
    return not _lower_name(path).endswith(".epub")


def file_dialog_types():
    exts = " ".join(f"*{e}" for e in SUPPORTED_EXTS) + " *.fb2.zip"
    return [
        ("All supported books", exts),
        ("EPUB", "*.epub"),
        ("PDF documents", "*.pdf"),
        ("Comics (CBZ / CBR / CB7)", "*.cbz *.cbr *.cb7"),
        ("Kindle (MOBI / AZW3)", "*.mobi *.azw *.azw3 *.prc"),
        ("FictionBook / Text", "*.fb2 *.fb2.zip *.fbz *.txt"),
    ]


def _stem(path):
    n = os.path.basename(path)
    low = n.lower()
    if low.endswith(".fb2.zip"):
        return n[:-8]
    return os.path.splitext(n)[0]


def _noop(*_a, **_k):
    pass


# =============================================================================
# STREAMING EPUB WRITER
# =============================================================================
class EpubWriter:
    """Minimal EPUB3 (+NCX) writer that streams into the zip, so big comics don't sit in RAM."""

    def __init__(self, path, title, author="Unknown Author", lang="en", description="",
                 rtl=False, fixed_layout=False, extra_meta=None):
        self.path = path
        self.title, self.author, self.lang = title or "Untitled", author or "Unknown Author", lang or "en"
        self.description = description or ""
        self.rtl, self.fixed_layout = rtl, fixed_layout
        self.extra_meta = list(extra_meta or [])
        self.uid = f"urn:uuid:{uuid.uuid4()}"
        self.manifest = []  # (id, href, media_type, properties)
        self.spine = []     # ids
        self.toc = []       # (title, href)
        self._ids = set()
        self._cover_id = None
        self.z = zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED)
        self.z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        self.z.writestr("META-INF/container.xml",
                        '<?xml version="1.0" encoding="UTF-8"?>\n'
                        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                        '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                        '</rootfiles></container>')

    def _new_id(self, base):
        base = re.sub(r"[^A-Za-z0-9_.-]", "_", base)
        if not re.match(r"[A-Za-z_]", base):
            base = "i_" + base
        i, cand = 1, base
        while cand in self._ids:
            i += 1
            cand = f"{base}_{i}"
        self._ids.add(cand)
        return cand

    def add_resource(self, href, data, media_type, cover=False, compress=True):
        rid = self._new_id(os.path.splitext(os.path.basename(href))[0])
        self.z.writestr("OEBPS/" + href, data,
                        compress_type=zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED)
        self.manifest.append((rid, href, media_type, "cover-image" if cover else ""))
        if cover:
            self._cover_id = rid
        return rid

    def mark_cover(self, resource_id):
        self._cover_id = resource_id
        self.manifest = [(i, h, m, ("cover-image" if i == resource_id else p)) for (i, h, m, p) in self.manifest]

    def add_extra_file(self, href, data):
        """Packaged but intentionally NOT in the manifest (internal metadata)."""
        self.z.writestr("OEBPS/" + href, data, compress_type=zipfile.ZIP_DEFLATED)

    def add_xhtml(self, href, title, body_html, in_toc=True, head_extra="", css="style.css", linear=True):
        doc = ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
               f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
               f'lang={_qa(self.lang)} xml:lang={_qa(self.lang)}><head><meta charset="utf-8"/>'
               f'<title>{_esc(title or self.title)}</title>'
               + (f'<link rel="stylesheet" type="text/css" href="{css}"/>' if css else "")
               + head_extra + f'</head><body>{body_html}</body></html>')
        rid = self._new_id(os.path.splitext(os.path.basename(href))[0])
        self.z.writestr("OEBPS/" + href, doc.encode("utf-8"))
        self.manifest.append((rid, href, "application/xhtml+xml", ""))
        if linear:
            self.spine.append(rid)
        if in_toc:
            self.toc.append((title or self.title, href))
        return rid

    def close(self):
        if not self.toc and self.spine:
            first = next(h for (i, h, m, p) in self.manifest if i == self.spine[0])
            self.toc.append((self.title, first))
        # --- nav.xhtml (EPUB3) ---
        items = "".join(f'<li><a href={_qa(h)}>{_esc(t)}</a></li>' for t, h in self.toc)
        nav = ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
               '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
               f'<head><meta charset="utf-8"/><title>{_esc(self.title)}</title></head><body>'
               f'<nav epub:type="toc" id="toc"><h1>{_esc(self.title)}</h1><ol>{items}</ol></nav></body></html>')
        self.z.writestr("OEBPS/nav.xhtml", nav.encode("utf-8"))
        # --- toc.ncx (EPUB2 readers / ebooklib) ---
        pts = "".join(
            f'<navPoint id="np{i}" playOrder="{i}"><navLabel><text>{_esc(t)}</text></navLabel>'
            f'<content src={_qa(h)}/></navPoint>' for i, (t, h) in enumerate(self.toc, 1))
        ncx = ('<?xml version="1.0" encoding="utf-8"?>\n'
               '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
               f'<head><meta name="dtb:uid" content={_qa(self.uid)}/></head>'
               f'<docTitle><text>{_esc(self.title)}</text></docTitle><navMap>{pts}</navMap></ncx>')
        self.z.writestr("OEBPS/toc.ncx", ncx.encode("utf-8"))
        # --- content.opf ---
        man = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
               '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>']
        for rid, href, mt, props in self.manifest:
            man.append(f'<item id={_qa(rid)} href={_qa(href)} media-type={_qa(mt)}'
                       + (f' properties={_qa(props)}' if props else "") + "/>")
        spine = "".join(f'<itemref idref={_qa(r)}/>' for r in self.spine)
        metas = []
        if self._cover_id:
            metas.append(f'<meta name="cover" content={_qa(self._cover_id)}/>')
        if self.fixed_layout:
            metas.append('<meta property="rendition:layout">pre-paginated</meta>')
            metas.append('<meta property="rendition:spread">none</meta>')
        for name, content in self.extra_meta:
            metas.append(f'<meta name={_qa(name)} content={_qa(content)}/>')
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        opf = ('<?xml version="1.0" encoding="utf-8"?>\n'
               '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" '
               'prefix="rendition: http://www.idpf.org/vocab/rendition/#">'
               '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">'
               f'<dc:identifier id="bookid">{_esc(self.uid)}</dc:identifier>'
               f'<dc:title>{_esc(self.title)}</dc:title><dc:creator>{_esc(self.author)}</dc:creator>'
               f'<dc:language>{_esc(self.lang)}</dc:language>'
               + (f'<dc:description>{_esc(self.description)}</dc:description>' if self.description else "")
               + f'<meta property="dcterms:modified">{now}</meta>' + "".join(metas) + '</metadata>'
               f'<manifest>{"".join(man)}</manifest>'
               f'<spine toc="ncx"' + (' page-progression-direction="rtl"' if self.rtl else "") + f'>{spine}</spine></package>')
        self.z.writestr("OEBPS/content.opf", opf.encode("utf-8"))
        self.z.close()


BASE_CSS = """body { margin: 0; padding: 0; }
p { margin: 0 0 0.7em 0; }
h1, h2, h3, h4, h5 { margin: 1.2em 0 0.6em 0; line-height: 1.25; }
blockquote { margin: 0.8em 1.5em; }
.subtitle { font-weight: bold; text-align: center; margin: 1em 0; }
.text-author { text-align: right; font-style: italic; }
.empty { margin: 0; }
.poem p, .stanza p { margin: 0; }
.stanza { margin: 0 0 1em 0; }
.img { text-align: center; margin: 1em 0; }
img { max-width: 100%; }
"""


# =============================================================================
# LIGHT METADATA READER (no ebooklib -> safe for huge comic EPUBs)
# =============================================================================
def _opf_path(z):
    try:
        root = ET.fromstring(z.read("META-INF/container.xml"))
        for el in root.iter():
            if el.tag.endswith("rootfile"):
                return el.attrib["full-path"]
    except Exception:
        pass
    for n in z.namelist():
        if n.lower().endswith(".opf"):
            return n
    return None


def is_comic_epub(epub_path):
    """True if this EPUB was produced by the comic importer (checks the zip directory only)."""
    try:
        with zipfile.ZipFile(epub_path) as z:
            return any(n.endswith(COMIC_MARKER) for n in z.namelist())
    except Exception:
        return False


def read_epub_light(epub_path, want_cover=True, cover_max=700):
    out = {"title": None, "author": None, "description": "", "language": "en", "cover": None, "is_comic": False}
    with zipfile.ZipFile(epub_path) as z:
        names = z.namelist()
        out["is_comic"] = any(n.endswith(COMIC_MARKER) for n in names)
        opf = _opf_path(z)
        if not opf:
            return out
        base = os.path.dirname(opf)
        root = ET.fromstring(z.read(opf))

        def local(t):
            return t.split("}")[-1]

        cover_id, items = None, {}
        for el in root.iter():
            t = local(el.tag)
            txt = (el.text or "").strip()
            if t == "title" and txt and not out["title"]:
                out["title"] = txt
            elif t == "creator" and txt and not out["author"]:
                out["author"] = txt
            elif t == "description" and txt:
                out["description"] = txt
            elif t == "language" and txt:
                out["language"] = txt
            elif t == "meta" and el.attrib.get("name") == "cover":
                cover_id = el.attrib.get("content")
            elif t == "item":
                items[el.attrib.get("id")] = (el.attrib.get("href", ""), el.attrib.get("media-type", ""),
                                              el.attrib.get("properties", ""))
        if want_cover:
            href = None
            if cover_id in items:
                href = items[cover_id][0]
            else:
                for (h, mt, pr) in items.values():
                    if "cover-image" in pr:
                        href = h
                        break
            if href:
                try:
                    from urllib.parse import unquote
                    data = z.read(os.path.normpath(os.path.join(base, unquote(href))).replace("\\", "/"))
                    im = Image.open(io.BytesIO(data))
                    im.load()
                    if max(im.size) > cover_max:
                        im.thumbnail((cover_max, cover_max))
                    out["cover"] = im.convert("RGB")
                except Exception:
                    pass
    return out


# =============================================================================
# TXT
# =============================================================================
_GUT_START = re.compile(r"\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*?\*\*\*", re.I | re.S)
_GUT_END = re.compile(r"\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*", re.I | re.S)
_HEAD_RE = re.compile(
    r"^(chapter|part|book|section|prologue|epilogue|prolog|epilog|interlude|rozdzia[łl]|cz[ęe][śs][ćc]|ksi[ęe]ga|"
    r"kapitel|teil|chapitre|partie|cap[ií]tulo|cap[ií]tulo|capitolo|parte|глава|часть)\b[\s.:\-–—IVXLCDM\d]*.{0,70}$",
    re.I)
_NUM_RE = re.compile(r"^(\d{1,3}|[IVXLCDM]{1,8})\.?$")


def decode_text(raw):
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", "replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for mod in ("charset_normalizer", "chardet"):
        try:
            m = __import__(mod)
            if mod == "charset_normalizer":
                best = m.from_bytes(raw).best()
                if best:
                    return str(best)
            else:
                enc = m.detect(raw).get("encoding")
                if enc:
                    return raw.decode(enc, "replace")
        except Exception:
            continue
    enc = (locale.getpreferredencoding(False) or "").lower()
    candidates = [enc] if enc and enc not in ("ascii", "utf-8", "utf8", "ansi_x3.4-1968") else []
    for e in candidates + ["cp1252"]:
        try:
            return raw.decode(e)
        except Exception:
            continue
    return raw.decode("latin-1")


def _txt_paragraphs(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip()]
    lines = [l for l in text.split("\n") if l.strip()]
    if not lines:
        return []
    lens = sorted(len(l.strip()) for l in lines)
    p95 = lens[int(len(lens) * 0.95) - 1] if len(lens) > 1 else lens[0]
    hard_wrapped = 45 <= p95 <= 120 and len(lines) > 1.6 * max(1, len(blocks)) and statistics.median(lens) >= 35
    paras = []
    for b in blocks:
        ls = [l.strip() for l in b.split("\n") if l.strip()]
        if hard_wrapped:
            cur = ls[0]
            for nxt in ls[1:]:
                if re.search(r"\w-$", cur) and nxt[:1].islower():
                    cur += nxt            # hyphenated at line end: keep the hyphen, drop the space
                else:
                    cur += " " + nxt
            paras.append(cur)
        else:
            paras.extend(ls)
    return paras


def guess_language(text):
    """Cheap script/diacritic based guess so hyphenation and XTC metadata get a sensible language."""
    sample = text[:200000]
    letters = [c for c in sample if c.isalpha()]
    if len(letters) < 50:
        return "en"
    n = len(letters)
    frac = lambda chars: sum(1 for c in letters if c in chars) / n
    if sum(1 for c in letters if "\u0400" <= c <= "\u04ff") / n > 0.3:
        return "ru"
    if frac("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ") > 0.012:
        return "pl"
    if frac("ěřůťďňĚŘŮŤĎŇ") > 0.008:
        return "cs"
    if frac("äöüßÄÖÜ") > 0.01:
        return "de"
    if frac("ñ¿¡") > 0.003:
        return "es"
    if frac("éèêàçùâîôœÉÈÊÀÇ") > 0.015:
        return "fr"
    return "en"


def _split_big(chapters, limit=70000):
    """Break very long chapters so MuPDF lays them out quickly (extra parts are named 'Section N')."""
    out, n = [], 0
    for title, paras in chapters:
        buf, size, first = [], 0, True
        for p in paras:
            buf.append(p)
            size += len(p)
            if size >= limit:
                n += 1
                out.append((title if first else f"Section {n}", buf))
                buf, size, first = [], 0, False
        if buf or first:
            if not first:
                n += 1
            out.append((title if first else f"Section {n}", buf))
    return out


def cur_title_first(chapters, heads, paras):
    """True when the very first paragraph is itself a heading (so there is no preamble)."""
    return bool(heads) and heads[0] == 0


def convert_txt(src, dest, progress=_noop):
    with open(src, "rb") as f:
        text = decode_text(f.read())
    title, author, lang = _stem(src), "Unknown Author", None
    m = _GUT_START.search(text)
    if m:
        head = text[:m.start()]
        t = re.search(r"^Title:\s*(.+)$", head, re.M)
        a = re.search(r"^Author:\s*(.+)$", head, re.M)
        l = re.search(r"^Language:\s*(\w+)", head, re.M | re.I)
        title = t.group(1).strip() if t else title
        author = a.group(1).strip() if a else author
        lang = {"English": "en", "Polish": "pl", "German": "de", "French": "fr", "Spanish": "es"}.get(
            l.group(1).strip(), l.group(1).strip().lower()[:2]) if l else lang
        text = text[m.end():]
    text = _GUT_END.sub("", text)
    lang = lang or guess_language(text)
    progress(0.2, "Detecting paragraphs…")
    paras = _txt_paragraphs(text)
    if not paras:
        raise ImportProblem("The text file is empty.")

    chapters, cur_title, cur = [], None, []
    heads = [i for i, p in enumerate(paras) if len(p) <= 80 and (_HEAD_RE.match(p) or _NUM_RE.match(p))]
    if len(heads) >= 2:
        head_set = set(heads)
        for i, p in enumerate(paras):
            if i in head_set:
                if cur or cur_title:
                    chapters.append((cur_title or title, cur))
                cur_title, cur = p, []
            else:
                cur.append(p)
        chapters.append((cur_title or title, cur))
        # A tiny preamble (usually just the book's title line) becomes the title instead of a chapter
        if chapters and chapters[0][0] == title and not cur_title_first(chapters, heads, paras):
            pre = chapters[0][1]
            if len(pre) <= 2 and sum(len(p) for p in pre) < 160:
                if pre and title == _stem(src) and len(pre[0]) <= 80:
                    title = pre[0]
                chapters = chapters[1:]
        chapters = [(t, ps) for t, ps in chapters if ps or t != title]
        has_heading = True
    else:
        chapters, has_heading = [(title, paras)], False

    chapters = _split_big(chapters)
    w = EpubWriter(dest, title, author, lang)
    w.add_resource("style.css", BASE_CSS.encode("utf-8"), "text/css")
    for i, (t, ps) in enumerate(chapters, 1):
        progress(0.3 + 0.7 * i / len(chapters), "Writing chapters…")
        body = ""
        if has_heading and not re.match(r"^Section \d+$", t):
            body += f"<h2>{_esc(t)}</h2>"
        body += "".join(f"<p>{_esc(p)}</p>" for p in ps if p != t)
        w.add_xhtml(f"chapter_{i:04d}.xhtml", t, body)
    w.close()


# =============================================================================
# FB2
# =============================================================================
def _strip_ns(root):
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        el.tag = el.tag.split("}")[-1]
        for k in list(el.attrib):
            nk = k.split("}")[-1]
            if nk != k:
                el.attrib[nk] = el.attrib.pop(k)
    return root


def _fb2_bytes(src):
    low = src.lower()
    if low.endswith((".zip", ".fbz")):
        with zipfile.ZipFile(src) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".fb2")]
            if not names:
                raise ImportProblem("No .fb2 file found inside the archive.")
            return z.read(names[0])
    with open(src, "rb") as f:
        return f.read()


def _parse_xml_lenient(data):
    try:
        from lxml import etree
        return _strip_ns(etree.fromstring(data, parser=etree.XMLParser(recover=True, huge_tree=True)))
    except ImportError:
        return _strip_ns(ET.fromstring(data))


def _sniff_image(data):
    try:
        im = Image.open(io.BytesIO(data))
        fmt = (im.format or "").upper()
    except Exception:
        return None, None
    return {"JPEG": ("jpg", "image/jpeg"), "PNG": ("png", "image/png"), "GIF": ("gif", "image/gif"),
            "WEBP": ("webp", "image/webp")}.get(fmt, (None, None))


class _Fb2Ctx:
    def __init__(self, id_to_file, img_map):
        self.id_to_file, self.img_map = id_to_file, img_map
        self.used_images = set()


def _fb2_text(el):
    return " ".join("".join(el.itertext()).split())


def _fb2_inline(el, ctx):
    out = [_esc(el.text or "")]
    for ch in el:
        t = ch.tag if isinstance(ch.tag, str) else ""
        inner = _fb2_inline(ch, ctx)
        if t == "emphasis":
            out.append(f"<em>{inner}</em>")
        elif t == "strong":
            out.append(f"<strong>{inner}</strong>")
        elif t == "strikethrough":
            out.append(f"<s>{inner}</s>")
        elif t in ("sub", "sup", "code"):
            out.append(f"<{t}>{inner}</{t}>")
        elif t == "a":
            href = ch.attrib.get("href", "")
            if href.startswith("#"):
                tid = href[1:]
                f = ctx.id_to_file.get(tid)
                target = f"{f}#{tid}" if f else href
                is_note = ch.attrib.get("type") == "note" or (f or "").startswith("notes")
                out.append(f'<a href={_qa(target)}' + (' epub:type="noteref"' if is_note else "") + f">{inner}</a>")
            elif href:
                out.append(f"<a href={_qa(href)}>{inner}</a>")
            else:
                out.append(inner)
        elif t == "image":
            out.append(_fb2_image(ch, ctx, inline=True))
        else:
            out.append(inner)
        out.append(_esc(ch.tail or ""))
    return "".join(out)


def _fb2_image(el, ctx, inline=False):
    href = el.attrib.get("href", "").lstrip("#")
    src = ctx.img_map.get(href)
    if not src:
        return ""
    ctx.used_images.add(href)
    tag = f'<img src={_qa(src)} alt={_qa(el.attrib.get("alt", ""))}/>'
    return tag if inline else f'<div class="img">{tag}</div>'


def _fb2_block(el, ctx, depth):
    t = el.tag if isinstance(el.tag, str) else ""
    idattr = f' id={_qa(el.attrib["id"])}' if el.attrib.get("id") else ""
    if t == "p":
        return f"<p{idattr}>{_fb2_inline(el, ctx)}</p>"
    if t == "subtitle":
        return f'<p class="subtitle"{idattr}>{_fb2_inline(el, ctx)}</p>'
    if t == "empty-line":
        return '<p class="empty">&#160;</p>'
    if t == "text-author":
        return f'<p class="text-author">{_fb2_inline(el, ctx)}</p>'
    if t == "image":
        return _fb2_image(el, ctx)
    if t in ("epigraph", "cite"):
        return f'<blockquote class="{t}"{idattr}>' + "".join(_fb2_block(c, ctx, depth) for c in el) + "</blockquote>"
    if t == "poem":
        return '<div class="poem">' + "".join(_fb2_block(c, ctx, depth) for c in el) + "</div>"
    if t == "stanza":
        return '<div class="stanza">' + "".join(
            f"<p>{_fb2_inline(v, ctx)}</p>" if v.tag == "v" else _fb2_block(v, ctx, depth) for v in el) + "</div>"
    if t == "title":
        lvl = min(6, max(1, depth + 1))
        return f"<h{lvl}>" + "<br/>".join(_fb2_inline(p, ctx) for p in el if p.tag == "p") + f"</h{lvl}>"
    if t == "table":
        rows = []
        for tr in el.iter("tr"):
            cells = "".join(f"<{c.tag}>{_fb2_inline(c, ctx)}</{c.tag}>" for c in tr if c.tag in ("td", "th"))
            rows.append(f"<tr>{cells}</tr>")
        return f"<table>{''.join(rows)}</table>"
    if t == "section":
        return f"<div{idattr}>" + "".join(_fb2_block(c, ctx, depth + 1) for c in el) + "</div>"
    if t == "annotation":
        return "".join(_fb2_block(c, ctx, depth) for c in el)
    return ""


def _fb2_chapters(body):
    """Top-level sections become chapters; descend through a single wrapper section."""
    secs = [c for c in body if c.tag == "section"]
    while len(secs) == 1 and len([c for c in secs[0] if c.tag == "section"]) >= 2:
        pre = [c for c in secs[0] if c.tag != "section"]
        secs = [c for c in secs[0] if c.tag == "section"]
        if any(c.tag != "title" for c in pre):  # keep the wrapper's own intro (a plain list of loose elements) as a leading chapter
            secs.insert(0, pre)
    return secs


def convert_fb2(src, dest, progress=_noop):
    data = _fb2_bytes(src)
    root = _parse_xml_lenient(data)
    if root is None or root.tag != "FictionBook":
        raise ImportProblem("This does not look like a valid FictionBook (.fb2) file.")

    ti = root.find("description/title-info")
    title = _fb2_text(ti.find("book-title")) if ti is not None and ti.find("book-title") is not None else _stem(src)
    authors = []
    if ti is not None:
        for a in ti.findall("author"):
            parts = [_fb2_text(a.find(k)) for k in ("first-name", "middle-name", "last-name") if a.find(k) is not None]
            name = " ".join(p for p in parts if p) or (_fb2_text(a.find("nickname")) if a.find("nickname") is not None else "")
            if name:
                authors.append(name)
    author = ", ".join(authors) or "Unknown Author"
    lang = _fb2_text(ti.find("lang")) if ti is not None and ti.find("lang") is not None else "en"
    ann = ti.find("annotation") if ti is not None else None
    description = _fb2_text(ann) if ann is not None else ""
    seq = ti.find("sequence") if ti is not None else None
    extra = []
    if seq is not None and seq.attrib.get("name"):
        extra.append(("calibre:series", seq.attrib["name"]))
        if seq.attrib.get("number"):
            extra.append(("calibre:series_index", seq.attrib["number"]))

    w = EpubWriter(dest, title, author, lang, description, extra_meta=extra)
    w.add_resource("style.css", BASE_CSS.encode("utf-8"), "text/css")

    # --- images ---
    progress(0.1, "Extracting images…")
    img_map, img_res = {}, {}
    for i, b in enumerate(root.findall("binary")):
        bid = b.attrib.get("id")
        try:
            raw = base64.b64decode("".join((b.text or "").split()), validate=False)
        except Exception:
            continue
        ext, mt = _sniff_image(raw)
        if not ext or not bid:
            continue
        href = f"images/img_{i:04d}.{ext}"
        img_res[bid] = (href, raw, mt)
        img_map[bid] = href

    # --- structure ---
    bodies = root.findall("body")
    main_bodies = [b for b in bodies if b.attrib.get("name") not in ("notes", "comments")]
    note_bodies = [b for b in bodies if b.attrib.get("name") in ("notes", "comments")]
    chapter_defs = []   # (title, [elements])
    for body in main_bodies:
        secs = _fb2_chapters(body)
        btitle = body.find("title")
        if secs:
            lead = [c for c in body if c.tag in ("epigraph", "image")]
            for k, s in enumerate(secs):
                if isinstance(s, list):   # loose intro elements of a wrapper section
                    st = next((c for c in s if c.tag == "title"), None)
                    t = _fb2_text(st) if st is not None else "Introduction"
                    els = (lead if k == 0 else []) + s
                else:
                    st = s.find("title")
                    t = _fb2_text(st) if st is not None else (title if k == 0 else f"Section {len(chapter_defs) + 1}")
                    els = (lead if k == 0 else []) + [s]
                chapter_defs.append((t, els, btitle if (k == 0 and btitle is not None) else None))
        else:
            chapter_defs.append((title, [c for c in body if c.tag != "title"], btitle))
    if not chapter_defs:
        raise ImportProblem("The FB2 file contains no readable text.")

    # id -> file map (so links and footnotes resolve)
    id_to_file = {}
    for i, (t, els, _bt) in enumerate(chapter_defs, 1):
        fn = f"chapter_{i:04d}.xhtml"
        for e in els:
            for sub in e.iter():
                if isinstance(sub.tag, str) and sub.attrib.get("id"):
                    id_to_file.setdefault(sub.attrib["id"], fn)
    for nb in note_bodies:
        for sub in nb.iter():
            if isinstance(sub.tag, str) and sub.attrib.get("id"):
                id_to_file.setdefault(sub.attrib["id"], "notes.xhtml")

    ctx = _Fb2Ctx(id_to_file, img_map)

    # cover
    cover_href = None
    if ti is not None:
        ci = ti.find("coverpage/image")
        if ci is not None:
            cid = ci.attrib.get("href", "").lstrip("#")
            if cid in img_res:
                href, raw, mt = img_res[cid]
                w.add_resource(href, raw, mt, cover=True, compress=False)
                cover_href = cid

    for i, (t, els, btitle) in enumerate(chapter_defs, 1):
        progress(0.2 + 0.7 * i / len(chapter_defs), "Converting chapters…")
        parts = []
        if btitle is not None and i == 1:
            parts.append(_fb2_block(btitle, ctx, 1))
        for e in els:
            if e.tag == "section":
                has_title = e.find("title") is not None
                inner = "".join(_fb2_block(c, ctx, 1) for c in e)
                sid = f' id={_qa(e.attrib["id"])}' if e.attrib.get("id") else ""
                parts.append(f"<div{sid}>{inner}</div>")
            else:
                parts.append(_fb2_block(e, ctx, 1))
        w.add_xhtml(f"chapter_{i:04d}.xhtml", t, "".join(parts))

    if note_bodies:
        parts = ["<h2>Notes</h2>"]
        for nb in note_bodies:
            for s in nb.iter("section"):
                nid = s.attrib.get("id")
                if not nid:
                    continue
                inner = "".join(_fb2_block(c, ctx, 3) for c in s if c.tag != "title")
                ntitle = s.find("title")
                label = f"<strong>{_esc(_fb2_text(ntitle))}</strong> " if ntitle is not None else ""
                parts.append(f'<aside epub:type="footnote" id={_qa(nid)}>{label}{inner}</aside>')
        w.add_xhtml("notes.xhtml", "Notes", "".join(parts))

    for bid in ctx.used_images:
        if bid in img_res and bid != cover_href:
            href, raw, mt = img_res[bid]
            w.add_resource(href, raw, mt, compress=False)
    w.close()


# =============================================================================
# MOBI / AZW3
# =============================================================================
def find_calibre():
    env = os.environ.get("XALIBRE_EBOOK_CONVERT")
    if env and os.path.exists(env):
        return env
    p = shutil.which("ebook-convert")
    if p:
        return p
    cands = []
    if sys.platform.startswith("win"):
        for root in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
            if root:
                cands.append(os.path.join(root, "Calibre2", "ebook-convert.exe"))
                cands.append(os.path.join(root, "Calibre", "ebook-convert.exe"))
    elif sys.platform == "darwin":
        cands.append("/Applications/calibre.app/Contents/MacOS/ebook-convert")
    else:
        cands += ["/opt/calibre/ebook-convert", "/usr/bin/ebook-convert", "/usr/local/bin/ebook-convert"]
    return next((c for c in cands if c and os.path.exists(c)), None)


def _run_quiet(cmd, timeout):
    kw = {}
    if sys.platform.startswith("win"):
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


def _looks_drm(text):
    t = (text or "").lower()
    return "drm" in t or "encrypted" in t


def _read_mobi7_package(base):
    """Metadata, cover path and NCX titles that KindleUnpack writes next to book.html."""
    meta, cover_path, ncx_points = {}, None, []
    for f in os.listdir(base):
        low = f.lower()
        try:
            if low.endswith(".opf"):
                root = ET.parse(os.path.join(base, f)).getroot()
                cover_id, items = None, {}
                for el in root.iter():
                    t = el.tag.split("}")[-1]
                    txt = (el.text or "").strip()
                    if t in ("title", "creator", "language", "description") and txt and t not in meta:
                        meta[t] = txt
                    elif t == "meta" and el.attrib.get("name") == "cover":
                        cover_id = el.attrib.get("content")
                    elif t == "item":
                        items[el.attrib.get("id")] = el.attrib.get("href", "")
                if cover_id in items:
                    p = os.path.normpath(os.path.join(base, items[cover_id]))
                    cover_path = p if os.path.isfile(p) else None
            elif low.endswith(".ncx"):
                root = ET.parse(os.path.join(base, f)).getroot()
                for np_ in root.iter():
                    if np_.tag.split("}")[-1] == "navPoint":
                        label = next((x.text for x in np_.iter() if x.tag.split("}")[-1] == "text"), "") or ""
                        src = next((x.attrib.get("src", "") for x in np_.iter() if x.tag.split("}")[-1] == "content"), "")
                        ncx_points.append((label.strip(), src.split("#", 1)[1] if "#" in src else ""))
        except Exception:
            continue
    return meta, cover_path, ncx_points


_TOC_TITLES = {"table of contents", "contents", "spis treści", "spis tresci", "inhalt", "table des matières",
               "índice", "indice", "оглавление", "содержание"}


def _html_to_epub(html_path, dest, fallback_title, progress):
    """Wrap an extracted MOBI7-style HTML book (plus images / OPF / NCX) into a proper multi-chapter EPUB."""
    from bs4 import BeautifulSoup
    base = os.path.dirname(html_path)
    with open(html_path, "rb") as f:
        text = decode_text(f.read())
    meta, cover_path, ncx_points = _read_mobi7_package(base)
    soup = BeautifulSoup(text, "html.parser")
    for tag in soup.find_all(["guide", "head"]):
        tag.decompose()

    # --- tidy legacy markup: size-marked bold paragraphs become headings; drop width/height hacks ---
    for p in soup.find_all(["p", "blockquote"]):
        for attr in ("height", "width"):
            p.attrs.pop(attr, None)
        font = p.find("font") if p.name == "p" else None
        if font and p.get_text(strip=True) == font.get_text(strip=True) and font.find("b") is not None:
            size = (font.get("size") or "").lstrip("+")
            if size.isdigit() and int(size) >= 4:
                p.name = "h2" if int(size) >= 5 else "h3"
    for font in soup.find_all("font"):
        font.unwrap()

    w = EpubWriter(dest, meta.get("title") or fallback_title, meta.get("creator") or "Unknown Author",
                   (meta.get("language") or "en")[:8], meta.get("description", ""))
    w.add_resource("style.css", BASE_CSS.encode("utf-8"), "text/css")

    # --- images: copy those referenced by the text, plus the cover ---
    seen = {}

    def add_image(p, cover=False):
        key = os.path.abspath(p)
        if key in seen:
            return seen[key]
        with open(p, "rb") as fh:
            data = fh.read()
        ext, mt = _sniff_image(data)
        if not ext:
            return None
        href = f"images/m_{len(seen):04d}.{ext}"
        w.add_resource(href, data, mt, cover=cover, compress=False)
        seen[key] = href
        return href

    if cover_path:
        add_image(cover_path, cover=True)
    for img in soup.find_all("img"):
        p = os.path.normpath(os.path.join(base, img.get("src") or ""))
        href = add_image(p) if os.path.isfile(p) else None
        if href:
            img["src"] = href
            for attr in ("align", "recindex"):
                img.attrs.pop(attr, None)
        else:
            img.decompose()

    # --- chapters: split at <mbp:pagebreak/>; fall back to h1/h2 ---
    body = soup.body or soup
    def blank(nodes):
        return not any(str(n).strip() and (getattr(n, "name", None) not in (None, "a", "br") or not hasattr(n, "name")
                                           or n.get_text(strip=True) or getattr(n, "name", "") == "img")
                       for n in nodes)
    chunks, cur = [], []
    for node in list(body.children):
        if getattr(node, "name", None) == "mbp:pagebreak":
            if not blank(cur):
                chunks.append(cur)
            cur = []
            continue
        cur.append(node)
    if not blank(cur):
        chunks.append(cur)
    if len(chunks) <= 1:
        chunks, cur = [], []
        for node in list(body.children):
            if getattr(node, "name", None) in ("h1", "h2") and not blank(cur):
                chunks.append(cur)
                cur = []
            cur.append(node)
        if not blank(cur):
            chunks.append(cur)
    if not chunks:
        raise ImportProblem("No readable text was found in this Kindle file.")

    # id -> chapter file (so '#filepos123' links keep working after splitting)
    frags = ["".join(str(n) for n in chunk) for chunk in chunks]
    id_to_file = {}
    for i, frag in enumerate(frags, 1):
        for m in re.finditer(r'\bid="([^"]+)"', frag):
            id_to_file.setdefault(m.group(1), f"chapter_{i:04d}.xhtml")

    for i, frag in enumerate(frags, 1):
        piece = BeautifulSoup(frag, "html.parser")
        for a in piece.find_all("a", href=True):
            if a["href"].startswith("#") and a["href"][1:] in id_to_file:
                a["href"] = f"{id_to_file[a['href'][1:]]}{a['href']}"
        ids = set(re.findall(r'\bid="([^"]+)"', frag))
        title = next((t for (t, fid) in ncx_points if fid in ids and t), None)
        if not title:
            h = piece.find(["h1", "h2", "h3", "b"])
            title = h.get_text(" ", strip=True)[:100] if h and h.get_text(strip=True) else f"Section {i}"
        # Kindle files often end with a generated "Table of Contents" page (nothing but links): drop it
        total_txt = len(piece.get_text(strip=True)) or 1
        link_txt = sum(len(a.get_text(strip=True)) for a in piece.find_all("a"))
        if title.strip().lower() in _TOC_TITLES and link_txt / total_txt >= 0.6 and len(frags) > 1:
            continue
        w.add_xhtml(f"chapter_{i:04d}.xhtml", title, str(piece))
        progress(0.6 + 0.4 * i / len(frags), "Writing chapters…")
    w.close()


def convert_kindle(src, dest, progress=_noop):
    calibre = find_calibre()
    if calibre:
        progress(0.1, "Converting with Calibre…")
        try:
            r = _run_quiet([calibre, src, dest, "--no-default-epub-cover"], timeout=900)
        except subprocess.TimeoutExpired:
            raise ImportProblem("Calibre took too long to convert this book (over 15 minutes).")
        if r.returncode != 0 or not os.path.exists(dest):
            msg = (r.stderr or r.stdout or "").strip().splitlines()
            if _looks_drm("\n".join(msg)):
                raise ImportProblem("This book is DRM-protected and can't be imported.")
            raise ImportProblem("Calibre could not convert this book:\n" + (msg[-1] if msg else "unknown error"))
        return

    try:
        import mobi  # pip install mobi
    except ImportError:
        raise ImportProblem(
            "Importing MOBI / AZW3 needs a converter.\n\n"
            "Install Calibre (https://calibre-ebook.com - it provides 'ebook-convert') "
            "or run:  pip install mobi")
    progress(0.1, "Unpacking Kindle file…")
    try:
        tmpdir, outpath = mobi.extract(src)
    except Exception as e:
        if _looks_drm(str(e)):
            raise ImportProblem("This book is DRM-protected and can't be imported.")
        raise ImportProblem(f"Could not unpack this Kindle file: {e}")
    try:
        low = (outpath or "").lower()
        if low.endswith(".epub"):
            shutil.copy2(outpath, dest)
        elif low.endswith((".html", ".htm")):
            _html_to_epub(outpath, dest, _stem(src), progress)
        else:
            raise ImportProblem("This Kindle file unpacked to an unsupported format "
                                f"({os.path.splitext(low)[1] or 'unknown'}). Installing Calibre will handle it.")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# =============================================================================
# COMICS (CBZ / CBR / CB7)
# =============================================================================
_IMG_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff")
COMIC_MAX_SIDE = 3200   # larger pages are downscaled on import (keeps EPUB small and rendering fast)


def _natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


class _Reader:
    def names(self):
        raise NotImplementedError

    def read(self, name):
        raise NotImplementedError

    def close(self):
        pass


class _ZipReader(_Reader):
    def __init__(self, path):
        self.z = zipfile.ZipFile(path)

    def names(self):
        return [i.filename for i in self.z.infolist() if not i.is_dir()]

    def read(self, name):
        return self.z.read(name)

    def close(self):
        self.z.close()


class _RarReader(_Reader):
    def __init__(self, path):
        import rarfile

        # 1. On Windows, locate UnRAR.exe or tar.exe if not already in PATH
        if sys.platform.startswith("win"):
            if not shutil.which(rarfile.UNRAR_TOOL or "unrar"):
                for root in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
                    if root:
                        candidate = os.path.join(root, "WinRAR", "UnRAR.exe")
                        if os.path.exists(candidate):
                            rarfile.UNRAR_TOOL = candidate
                            break
            if not (rarfile.UNRAR_TOOL and os.path.exists(rarfile.UNRAR_TOOL)) and not shutil.which("unrar"):
                tar_bin = shutil.which("tar") or shutil.which("bsdtar")
                if tar_bin:
                    rarfile.BSDTAR_TOOL = tar_bin

        # 2. Fix upstream rarfile bug where BSDTAR_TOOL open_cmdline adds '--' after '-f',
        # causing bsdtar to treat '--' as the filename and fail with:
        # "bsdtar: Error opening archive: Failed to open '--'" (52 bytes).
        if not getattr(rarfile.ToolSetup, "_xalibre_patched", False):
            def safe_open_cmdline(self, pwd, rarfn, filefn=None):
                is_bsdtar = bool(self.setup and 'BSDTAR_TOOL' in self.setup.get('open_cmd', ()))
                cmdline = self.get_cmdline('open_cmd', pwd, nodash=is_bsdtar)
                cmdline.append(rarfn)
                if filefn:
                    self.add_file_arg(cmdline, filefn)
                return cmdline

            rarfile.ToolSetup.open_cmdline = safe_open_cmdline
            rarfile.ToolSetup._xalibre_patched = True

        try:
            rarfile.tool_setup(unrar=True, unar=True, bsdtar=True, force=True)
        except Exception:
            pass

        self.r = rarfile.RarFile(path)
        # Verify decompression actually works; if not, raise to fall back to _DirReader
        test_names = [i.filename for i in self.r.infolist() if not i.is_dir()]
        if test_names:
            self.r.read(test_names[0])

    def names(self):
        return [i.filename for i in self.r.infolist() if not i.is_dir()]

    def read(self, name):
        return self.r.read(name)

    def close(self):
        self.r.close()


class _DirReader(_Reader):
    """Fallback: extract with a command-line tool (7-Zip / WinRAR / bsdtar / unar) then read from disk."""

    def __init__(self, path):
        self.tmp = tempfile.mkdtemp(prefix="xalibre_cb_")
        tools = []
        for exe in ("7z", "7za", "7zr", "7zz"):
            p = shutil.which(exe)
            if p:
                tools.append([p, "x", "-y", f"-o{self.tmp}", path])
        if sys.platform.startswith("win"):
            for root in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
                if root:
                    p_7z = os.path.join(root, "7-Zip", "7z.exe")
                    if os.path.exists(p_7z):
                        tools.append([p_7z, "x", "-y", f"-o{self.tmp}", path])
                    p_unrar = os.path.join(root, "WinRAR", "UnRAR.exe")
                    if os.path.exists(p_unrar):
                        tools.append([p_unrar, "x", "-y", path, self.tmp + os.sep])
                    p_winrar = os.path.join(root, "WinRAR", "WinRAR.exe")
                    if os.path.exists(p_winrar):
                        tools.append([p_winrar, "x", "-ibck", "-y", path, self.tmp + os.sep])
        for exe in ("bsdtar", "tar" if sys.platform.startswith("win") else None):
            p = shutil.which(exe) if exe else None
            if p:
                tools.append([p, "-xf", path, "-C", self.tmp])
        p = shutil.which("unar")
        if p:
            tools.append([p, "-q", "-o", self.tmp, path])
        ok = False
        for cmd in tools:
            try:
                if _run_quiet(cmd, 600).returncode == 0 and any(os.scandir(self.tmp)):
                    ok = True
                    break
            except Exception:
                continue
        if not ok:
            shutil.rmtree(self.tmp, ignore_errors=True)
            raise ImportProblem(
                "Could not open this comic archive.\n\nFor CBR / CB7 files install 7-Zip (or WinRAR / UnRAR) "
                "and make sure it is on your PATH, or run:  pip install rarfile")

    def names(self):
        out = []
        for root, _d, files in os.walk(self.tmp):
            for f in files:
                out.append(os.path.relpath(os.path.join(root, f), self.tmp).replace("\\", "/"))
        return out

    def read(self, name):
        with open(os.path.join(self.tmp, name), "rb") as f:
            return f.read()

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def _open_archive(path):
    with open(path, "rb") as f:
        magic = f.read(8)
    if magic[:2] == b"PK":
        return _ZipReader(path)
    if magic[:4] == b"Rar!":
        try:
            import rarfile  # noqa: F401
            return _RarReader(path)
        except Exception as e:
            print(f"RarReader failed ({e}), falling back to DirReader for {os.path.basename(path)}")
            return _DirReader(path)
    return _DirReader(path)  # 7z and anything else


def _comic_info(xml_bytes):
    info = {}
    try:
        root = ET.fromstring(xml_bytes)
        for el in root:
            tag = el.tag.split("}")[-1]
            if el.text and el.text.strip():
                info[tag] = el.text.strip()
    except Exception:
        pass
    return info


def _prep_page(data, max_side=COMIC_MAX_SIDE):
    """Return (bytes, ext, media_type, w, h). Keeps original bytes when possible."""
    im = Image.open(io.BytesIO(data))
    fmt = (im.format or "").upper()
    w, h = im.size
    if fmt in ("JPEG", "PNG", "GIF", "WEBP") and max(w, h) <= max_side and im.mode != "CMYK":
        ext, mt = {"JPEG": ("jpg", "image/jpeg"), "PNG": ("png", "image/png"), "GIF": ("gif", "image/gif"),
                   "WEBP": ("webp", "image/webp")}[fmt]
        return data, ext, mt, w, h
    im.load()
    if max(w, h) > max_side:
        s = max_side / max(w, h)
        im = im.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)
    out = io.BytesIO()
    if im.mode in ("1", "L"):
        im.convert("L").save(out, "PNG", optimize=True)
        ext, mt = "png", "image/png"
    else:
        im.convert("RGB").save(out, "JPEG", quality=90, optimize=True)
        ext, mt = "jpg", "image/jpeg"
    return out.getvalue(), ext, mt, im.size[0], im.size[1]


def convert_comic(src, dest, progress=_noop):
    rd = _open_archive(src)
    try:
        names = rd.names()
        pages = []
        info = {}
        for n in names:
            base = os.path.basename(n)
            low = base.lower()
            if "__macosx" in n.lower() or base.startswith(".") or low == "thumbs.db":
                continue
            if low == "comicinfo.xml":
                try:
                    info = _comic_info(rd.read(n))
                except Exception:
                    pass
                continue
            if low.endswith(_IMG_EXTS):
                pages.append(n)
        if not pages:
            raise ImportProblem("No image pages were found in this comic archive.")
        pages.sort(key=lambda n: _natural_key(n.replace("\\", "/")))

        # --- chapters from sub-folders ---
        dirs = []
        for n in pages:
            d = os.path.dirname(n.replace("\\", "/"))
            if not dirs or dirs[-1] != d:
                dirs.append(d)
        multi = len(set(dirs)) >= 2

        stem = _stem(src)
        series, number, ctitle = info.get("Series"), info.get("Number"), info.get("Title")
        if series:
            title = f"{series} {number}".strip() if number else series
            if ctitle and ctitle.lower() != series.lower():
                title += f" - {ctitle}"
        else:
            title = ctitle or stem
        author = info.get("Writer") or info.get("Penciller") or "Unknown Author"
        rtl = "righttoleft" in (info.get("Manga", "") or "").lower().replace(" ", "")
        lang = (info.get("LanguageISO") or "en")[:8]

        w = EpubWriter(dest, title, author, lang, info.get("Summary", ""), rtl=rtl, fixed_layout=True,
                       extra_meta=[("xalibre:comic", "1")])
        w.add_resource("style.css", b"html,body{margin:0;padding:0}img{display:block;margin:0 auto}", "text/css")
        page_meta, chap_meta, cur_dir = [], [], object()
        total = len(pages)
        for i, n in enumerate(pages):
            progress(0.05 + 0.9 * i / total, f"Packing page {i + 1}/{total}…")
            try:
                data, ext, mt, pw, ph = _prep_page(rd.read(n))
            except Exception as e:
                print(f"Skipping unreadable page {n}: {e}")
                continue
            idx = len(page_meta)
            href = f"images/p{idx + 1:04d}.{ext}"
            rid = w.add_resource(href, data, mt, cover=(idx == 0), compress=False)
            d = os.path.dirname(n.replace("\\", "/"))
            if d != cur_dir:
                cur_dir = d
                ctitle_d = (os.path.basename(d) or title) if multi else title
                ctitle_d = re.sub(r"[_]+", " ", ctitle_d).strip() or f"Part {len(chap_meta) + 1}"
                chap_meta.append({"title": ctitle_d, "start": idx})
            body = f'<div class="page"><img src={_qa(href)} alt="Page {idx + 1}" width="{pw}" height="{ph}"/></div>'
            is_chap_start = chap_meta and chap_meta[-1]["start"] == idx
            w.add_xhtml(f"p{idx + 1:04d}.xhtml", chap_meta[-1]["title"] if is_chap_start else f"Page {idx + 1}", body,
                        in_toc=bool(is_chap_start), css="style.css",
                        head_extra=f'<meta name="viewport" content="width={pw}, height={ph}"/>')
            page_meta.append({"href": href, "w": pw, "h": ph, "chapter": len(chap_meta) - 1})
        if not page_meta:
            raise ImportProblem("None of the pages in this comic archive could be read.")
        w.add_extra_file(COMIC_MARKER, json.dumps(
            {"version": 1, "rtl": rtl, "pages": page_meta, "chapters": chap_meta}).encode("utf-8"))
        w.close()
    finally:
        rd.close()


# =============================================================================
# PDF (treated as a fixed-layout "comic EPUB" with rendered page images)
# =============================================================================
def convert_pdf(src, dest, progress=_noop):
    if fitz is None:
        raise ImportProblem("PyMuPDF is required to import PDF files.\nRun: pip install pymupdf")
    try:
        doc = fitz.open(src)
    except Exception as e:
        raise ImportProblem(f"Could not open this PDF file: {e}")

    try:
        if doc.is_encrypted:
            if not doc.authenticate(""):
                raise ImportProblem("This PDF is password-protected and cannot be imported.")

        total = len(doc)
        if total == 0:
            raise ImportProblem("The PDF file contains no pages.")

        stem = _stem(src)
        pdf_meta = doc.metadata or {}
        title = (pdf_meta.get("title") or "").strip() or stem
        author = (pdf_meta.get("author") or "").strip() or "Unknown Author"
        subject = (pdf_meta.get("subject") or "").strip()

        # Build TOC / chapters from PDF bookmarks if available
        raw_toc = []
        try:
            raw_toc = doc.get_toc()  # [[lvl, title, page_1_based], ...]
        except Exception:
            pass

        chap_meta = []
        if raw_toc:
            min_lvl = min(item[0] for item in raw_toc if isinstance(item[0], int))
            lvl_counts = Counter(item[0] for item in raw_toc if isinstance(item[0], int))
            target_lvls = {min_lvl}
            if lvl_counts.get(min_lvl, 0) <= 2 and lvl_counts.get(min_lvl + 1, 0) > 0:
                target_lvls.add(min_lvl + 1)

            seen_starts = set()
            for item in raw_toc:
                if len(item) >= 3 and item[0] in target_lvls:
                    c_title = str(item[1]).strip()
                    p_num = item[2]
                    if isinstance(p_num, int) and 1 <= p_num <= total:
                        p_start = p_num - 1  # 0-based
                        if p_start not in seen_starts:
                            seen_starts.add(p_start)
                            chap_meta.append({"title": c_title or f"Chapter {len(chap_meta) + 1}", "start": p_start})

            chap_meta.sort(key=lambda c: c["start"])

        # If no chapters from TOC, or first chapter does not start at page 0
        if not chap_meta:
            chap_meta = [{"title": title, "start": 0}]
        elif chap_meta[0]["start"] > 0:
            chap_meta.insert(0, {"title": title, "start": 0})

        w = EpubWriter(dest, title, author, "en", subject, rtl=False, fixed_layout=True,
                       extra_meta=[("xalibre:comic", "1"), ("xalibre:pdf", "1")])
        w.add_resource("style.css", b"html,body{margin:0;padding:0}img{display:block;margin:0 auto}", "text/css")

        page_meta = []
        cur_chap_idx = 0

        for i in range(total):
            progress(0.05 + 0.9 * i / total, f"Rendering PDF page {i + 1}/{total}…")
            page = doc[i]
            rect = page.rect
            rw, rh = max(1.0, rect.width), max(1.0, rect.height)

            # Target ~2000 px on longest side for sharp e-ink rendering and smart tiers
            zoom = min(3.0, max(1.5, 2000.0 / max(rw, rh)))
            if max(rw, rh) * zoom > COMIC_MAX_SIDE:
                zoom = COMIC_MAX_SIDE / max(rw, rh)

            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat, alpha=False)

            im = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            pix = None
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=90, optimize=True)
            data = buf.getvalue()
            pw, ph = im.size
            im = None
            ext, mt = "jpg", "image/jpeg"

            idx = len(page_meta)
            href = f"images/p{idx + 1:04d}.{ext}"
            w.add_resource(href, data, mt, cover=(idx == 0), compress=False)

            while cur_chap_idx + 1 < len(chap_meta) and idx >= chap_meta[cur_chap_idx + 1]["start"]:
                cur_chap_idx += 1

            is_chap_start = (idx == chap_meta[cur_chap_idx]["start"])
            chap_title = chap_meta[cur_chap_idx]["title"]

            body = f'<div class="page"><img src={_qa(href)} alt="Page {idx + 1}" width="{pw}" height="{ph}"/></div>'
            w.add_xhtml(f"p{idx + 1:04d}.xhtml", chap_title if is_chap_start else f"Page {idx + 1}", body,
                        in_toc=bool(is_chap_start), css="style.css",
                        head_extra=f'<meta name="viewport" content="width={pw}, height={ph}"/>')
            page_meta.append({"href": href, "w": pw, "h": ph, "chapter": cur_chap_idx})

        if not page_meta:
            raise ImportProblem("None of the pages in this PDF could be rendered.")

        w.add_extra_file(COMIC_MARKER, json.dumps(
            {"version": 1, "rtl": False, "pages": page_meta, "chapters": chap_meta}).encode("utf-8"))
        w.close()
    finally:
        doc.close()


# =============================================================================
# DISPATCH
# =============================================================================
def convert_to_epub(src, work_dir, progress=None):
    """Convert `src` into <work_dir>/<name>.epub. Returns a metadata dict."""
    progress = progress or _noop
    os.makedirs(work_dir, exist_ok=True)
    low = _lower_name(src)
    dest = os.path.join(work_dir, _stem(src) + ".epub")
    if low.endswith(".epub"):
        shutil.copy2(src, dest)
    elif low.endswith(".txt"):
        convert_txt(src, dest, progress)
    elif low.endswith((".fb2", ".fbz", ".fb2.zip")):
        convert_fb2(src, dest, progress)
    elif low.endswith(KINDLE_EXTS):
        convert_kindle(src, dest, progress)
    elif low.endswith(COMIC_EXTS):
        convert_comic(src, dest, progress)
    elif low.endswith(PDF_EXTS):
        convert_pdf(src, dest, progress)
    else:
        raise ImportProblem(f"Unsupported file type: {os.path.splitext(low)[1]}")
    meta = read_epub_light(dest)
    meta["epub_path"] = dest
    meta["title"] = meta["title"] or _stem(src)
    meta["author"] = meta["author"] or "Unknown Author"
    return meta
