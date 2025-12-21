import os
import sys
import struct
import fitz  # PyMuPDF
from PIL import Image, ImageEnhance, ImageDraw, ImageFont
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup, NavigableString
import pyphen  # <--- Essential for PyMuPDF
import base64
import customtkinter as ctk
from tkinter import filedialog, messagebox
import threading
import re

# --- CONFIGURATION DEFAULTS ---
DEFAULT_SCREEN_WIDTH = 480
DEFAULT_SCREEN_HEIGHT = 800
DEFAULT_RENDER_SCALE = 3.0
DEFAULT_FONT_SIZE = 22
DEFAULT_MARGIN = 20
DEFAULT_LINE_HEIGHT = 1.4
DEFAULT_FONT_WEIGHT = 400
DEFAULT_BOTTOM_PADDING = 15
DEFAULT_TOP_PADDING = 15


# --- UTILITY FUNCTIONS ---

def fix_css_font_paths(css_text, target_font_family="'CustomFont'"):
    if target_font_family is None:
        return css_text
    css_text = re.sub(r'font-family\s*:\s*[^;!]+', f'font-family: {target_font_family}', css_text)
    return css_text


def get_pil_font(font_path, size):
    try:
        if font_path and os.path.exists(font_path):
            return ImageFont.truetype(font_path, size)
        return ImageFont.load_default()
    except:
        return ImageFont.load_default()


def extract_all_css(book):
    css_rules = []
    for item in book.get_items_of_type(ebooklib.ITEM_STYLE):
        try:
            css_rules.append(item.get_content().decode('utf-8', errors='ignore'))
        except:
            pass
    return "\n".join(css_rules)


def extract_images_to_base64(book):
    image_map = {}
    for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
        try:
            filename = os.path.basename(item.get_name())
            b64_data = base64.b64encode(item.get_content()).decode('utf-8')
            image_map[filename] = f"data:{item.media_type};base64,{b64_data}"
        except:
            pass
    return image_map


def get_official_toc_mapping(book):
    mapping = {}

    def process_toc_item(item):
        if isinstance(item, tuple):
            if len(item) > 1 and isinstance(item[1], list):
                for sub in item[1]: process_toc_item(sub)
        elif isinstance(item, epub.Link):
            clean_href = item.href.split('#')[0]
            mapping[clean_href] = item.title

    for item in book.toc: process_toc_item(item)
    return mapping


# --- IMPROVED HYPHENATION FUNCTION ---
def hyphenate_html_text(soup, language_code):
    try:
        dic = pyphen.Pyphen(lang=language_code)
    except:
        try:
            dic = pyphen.Pyphen(lang='en')
        except:
            return soup

    word_pattern = re.compile(r'\w+', re.UNICODE)

    for text_node in soup.find_all(string=True):
        if text_node.parent.name in ['script', 'style', 'head', 'title', 'meta']:
            continue
        if not text_node.strip():
            continue

        original_text = str(text_node)

        # --- FIX: FORCE NORMAL SPACES ---
        # 1. Replace Non-Breaking Space (\u00A0) with a standard space.
        # This allows the "Justify" alignment to stretch this space evenly with the others.
        clean_text = original_text.replace('\u00A0', ' ')

        # --------------------------------

        def replace_match(match):
            word = match.group(0)
            if len(word) < 6: return word
            return dic.inserted(word, hyphen='\u00AD')

        new_text = word_pattern.sub(replace_match, clean_text)

        if new_text != original_text:
            text_node.replace_with(NavigableString(new_text))

    return soup


def get_local_fonts():
    if getattr(sys, 'frozen', False):
        base_path = os.path.dirname(sys.executable)
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))

    fonts_dir = os.path.join(base_path, "fonts")
    if not os.path.exists(fonts_dir):
        try:
            os.makedirs(fonts_dir)
        except OSError:
            pass

    fonts = []
    if os.path.exists(fonts_dir):
        for f in os.listdir(fonts_dir):
            if f.lower().endswith((".ttf", ".otf")):
                fonts.append(os.path.abspath(os.path.join(fonts_dir, f)))
    return sorted(fonts)


# --- PROCESSING ENGINE ---

class EpubProcessor:
    def __init__(self):
        self.input_file = ""
        self.font_path = ""
        self.font_size = DEFAULT_FONT_SIZE
        self.margin = DEFAULT_MARGIN
        self.line_height = DEFAULT_LINE_HEIGHT
        self.font_weight = DEFAULT_FONT_WEIGHT
        self.bottom_padding = DEFAULT_BOTTOM_PADDING
        self.top_padding = DEFAULT_TOP_PADDING
        self.text_align = "justify"
        self.screen_width = DEFAULT_SCREEN_WIDTH
        self.screen_height = DEFAULT_SCREEN_HEIGHT
        self.fitz_docs = []
        self.toc_data_final = []
        self.toc_pages_images = []
        self.page_map = []
        self.total_pages = 0
        self.toc_items_per_page = 18
        self.is_ready = False

    def load_and_layout(self, input_path, font_path, font_size, margin, line_height, font_weight,
                        bottom_padding, top_padding, text_align="justify", add_toc=True, progress_callback=None):
        self.input_file = input_path
        self.font_path = font_path if font_path != "DEFAULT" else ""
        self.font_size = font_size
        self.margin = margin
        self.line_height = line_height
        self.font_weight = font_weight
        self.bottom_padding = bottom_padding
        self.top_padding = top_padding
        self.text_align = text_align

        for doc, _ in self.fitz_docs: doc.close()
        self.fitz_docs, self.page_map, self.toc_data = [], [], []

        try:
            book = epub.read_epub(self.input_file)
        except Exception as e:
            print(f"Error reading EPUB: {e}")
            return False

        if self.font_path:
            css_font_path = self.font_path.replace("\\", "/")
            font_face_rule = f'@font-face {{ font-family: "CustomFont"; src: url("{css_font_path}"); }}'
            font_family_val = '"CustomFont"'
        else:
            font_face_rule = ""
            font_family_val = "serif"

        # CSS: We keep 'hyphens: auto' as a fallback, but we rely on Python injection
        custom_css = f"""
        <style>
            {font_face_rule}
            @page {{ margin: 0; }}

            body, p, div, span, li, blockquote, dd, dt {{
                font-family: {font_family_val} !important;
                font-size: {self.font_size}pt !important;
                font-weight: {self.font_weight} !important;
                line-height: {self.line_height} !important;
                text-align: {self.text_align} !important;
                color: black !important;
                overflow-wrap: break-word;
            }}

            body {{
                margin: 0 !important;
                padding: {self.margin}px !important;
                background-color: white !important;
            }}

            img {{ max-width: 95% !important; height: auto !important; display: block; margin: 50px auto !important; }}

            h1, h2, h3 {{ 
                text-align: center !important; 
                margin-top: 1em; 
                font-weight: {min(900, self.font_weight + 200)} !important; 
            }}
        </style>
        """

        try:
            book_lang = book.get_metadata('DC', 'language')[0][0]
        except:
            book_lang = 'en'

        image_map = extract_images_to_base64(book)
        original_css = fix_css_font_paths(extract_all_css(book), font_family_val)
        toc_mapping = get_official_toc_mapping(book)

        items = [book.get_item_with_id(item_ref[0]) for item_ref in book.spine
                 if isinstance(book.get_item_with_id(item_ref[0]), epub.EpubHtml)]

        temp_chapter_starts = []
        running_page_count = 0

        render_dir = os.path.dirname(input_path)
        temp_html_path = os.path.join(render_dir, "render_temp.html")

        self.toc_data = []
        for idx, item in enumerate(items):
            if progress_callback: progress_callback((idx / len(items)) * 0.9)

            item_name = item.get_name()
            raw_html = item.get_content().decode('utf-8', errors='replace')
            soup = BeautifulSoup(raw_html, 'html.parser')

            has_image = bool(soup.find('img'))
            text_content = soup.get_text().strip()

            if item_name not in toc_mapping and len(text_content) < 50 and not has_image: continue

            temp_chapter_starts.append(running_page_count)
            chapter_title = toc_mapping.get(item_name) or (soup.find(['h1', 'h2']).get_text().strip() if soup.find(
                ['h1', 'h2']) else f"Section {len(self.toc_data) + 1}")
            self.toc_data.append(chapter_title)

            for img_tag in soup.find_all('img'):
                src = os.path.basename(img_tag.get('src', ''))
                if src in image_map: img_tag['src'] = image_map[src]

            # --- KEY FIX: Smart Hyphenation ---
            # We call the improved function here.
            soup = hyphenate_html_text(soup, book_lang)

            body_content = "".join([str(x) for x in soup.body.contents]) if soup.body else str(soup)
            final_html = f"<html lang='{book_lang}'><head><style>{original_css}</style>{custom_css}</head><body>{body_content}</body></html>"

            with open(temp_html_path, "w", encoding="utf-8") as f:
                f.write(final_html)

            doc = fitz.open(temp_html_path)
            self.fitz_docs.append((doc, has_image))
            for i in range(len(doc)): self.page_map.append((len(self.fitz_docs) - 1, i))
            running_page_count += len(doc)

        if os.path.exists(temp_html_path): os.remove(temp_html_path)

        if add_toc:
            toc_header_space = 100 + self.top_padding
            toc_row_height = 35
            available_h = self.screen_height - self.bottom_padding - toc_header_space

            self.toc_items_per_page = max(1, int(available_h // toc_row_height))
            num_toc_pages = (len(self.toc_data) + self.toc_items_per_page - 1) // self.toc_items_per_page

            self.toc_data_final = [(t, temp_chapter_starts[i] + num_toc_pages + 1) for i, t in enumerate(self.toc_data)]
            self.toc_pages_images = self._render_toc_pages(self.toc_data_final)
        else:
            self.toc_data_final = [(t, temp_chapter_starts[i] + 1) for i, t in enumerate(self.toc_data)]
            self.toc_pages_images = []

        self.total_pages = len(self.toc_pages_images) + len(self.page_map)
        if progress_callback: progress_callback(1.0)
        self.is_ready = True
        return True

    def _get_ui_font(self, size):
        if self.font_path:
            return get_pil_font(self.font_path, size)
        try:
            return ImageFont.truetype("georgia.ttf", size)
        except:
            return ImageFont.load_default()

    def _render_toc_pages(self, toc_entries):
        pages = []

        def get_dynamic_toc_font(size):
            if self.font_path: return get_pil_font(self.font_path, size)
            try:
                return ImageFont.truetype("georgia.ttf", size)
            except:
                return ImageFont.load_default()

        font_main = get_dynamic_toc_font(20)
        font_header = get_dynamic_toc_font(24)
        left_margin, right_margin, column_gap = 40, 40, 20

        limit = self.toc_items_per_page

        for i in range(0, len(toc_entries), limit):
            chunk = toc_entries[i: i + limit]
            img = Image.new('1', (self.screen_width, self.screen_height), 1)
            draw = ImageDraw.Draw(img)

            header_text = "TABLE OF CONTENTS"
            header_w = font_header.getlength(header_text)
            header_y = 40 + self.top_padding
            draw.text(((self.screen_width - header_w) // 2, header_y), header_text, font=font_header, fill=0)

            line_y = header_y + 35
            draw.line((left_margin, line_y, self.screen_width - right_margin, line_y), fill=0)

            y = line_y + 25
            for title, pg_num in chunk:
                pg_str = str(pg_num)
                pg_w = font_main.getlength(pg_str)
                max_title_w = self.screen_width - left_margin - right_margin - pg_w - column_gap
                display_title = title
                if font_main.getlength(display_title) > max_title_w:
                    while font_main.getlength(display_title + "...") > max_title_w and len(display_title) > 0:
                        display_title = display_title[:-1]
                    display_title += "..."
                draw.text((left_margin, y), display_title, font=font_main, fill=0)
                title_end_x = left_margin + font_main.getlength(display_title) + 5
                dots_end_x = self.screen_width - right_margin - pg_w - 10
                if dots_end_x > title_end_x:
                    dots_text = "." * int((dots_end_x - title_end_x) / font_main.getlength("."))
                    draw.text((title_end_x, y), dots_text, font=font_main, fill=0)
                draw.text((self.screen_width - right_margin - pg_w, y), pg_str, font=font_main, fill=0)
                y += 35
            pages.append(img)
        return pages

    def render_page(self, global_page_index):
        if not self.is_ready: return None
        num_toc = len(self.toc_pages_images)

        footer_height = max(0, self.bottom_padding)
        header_height = max(0, self.top_padding)
        content_height = self.screen_height - footer_height - header_height

        if global_page_index < num_toc:
            img = self.toc_pages_images[global_page_index].copy().convert("RGB")
        else:
            doc_idx, page_idx = self.page_map[global_page_index - num_toc]
            doc, has_image = self.fitz_docs[doc_idx]
            page = doc[page_idx]
            mat = fitz.Matrix(DEFAULT_RENDER_SCALE, DEFAULT_RENDER_SCALE)
            pix = page.get_pixmap(matrix=mat, alpha=False)

            img_content = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            img_content = img_content.resize((self.screen_width, content_height), Image.Resampling.LANCZOS).convert("L")

            img = Image.new("RGB", (self.screen_width, self.screen_height), (255, 255, 255))
            img.paste(img_content, (0, header_height))

            if has_image:
                img = img.convert("L")
                img = ImageEnhance.Contrast(ImageEnhance.Brightness(img).enhance(1.15)).enhance(1.4)
                img = img.convert("1", dither=Image.Dither.FLOYDSTEINBERG)
            else:
                img = img.convert("L")
                img = ImageEnhance.Contrast(img).enhance(2.0).point(lambda p: 255 if p > 140 else 0, mode='1')
            img = img.convert("RGB")

        draw = ImageDraw.Draw(img)
        font_ui = self._get_ui_font(16)

        page_num_disp = global_page_index + 1
        current_title = ""
        chapter_pages = [item[1] for item in self.toc_data_final]
        for title, start_pg in reversed(self.toc_data_final):
            if page_num_disp >= start_pg:
                current_title = title
                break

        bar_height = 4
        bar_y_top = self.screen_height - 20
        footer_y = self.screen_height - 45

        draw.rectangle([10, bar_y_top, self.screen_width - 10, bar_y_top + bar_height], fill=(255, 255, 255),
                       outline=(0, 0, 0))

        for cp in chapter_pages:
            if self.total_pages > 0:
                mx = int(((cp - 1) / self.total_pages) * (self.screen_width - 20)) + 10
                draw.line([mx, bar_y_top - 4, mx, bar_y_top], fill=(0, 0, 0), width=1)

        if self.total_pages > 0:
            bw = int((page_num_disp / self.total_pages) * (self.screen_width - 20))
            draw.rectangle([10, bar_y_top, 10 + bw, bar_y_top + bar_height], fill=(0, 0, 0))

        draw.text((15, footer_y), f"{page_num_disp}/{self.total_pages}", font=font_ui, fill=(0, 0, 0))
        if current_title:
            draw.text((100, footer_y), f"| {current_title}"[:35], font=font_ui, fill=(0, 0, 0))

        return img

    def save_xtc(self, out_name, progress_callback=None):
        if not self.is_ready: return
        blob, idx = bytearray(), bytearray()
        data_off = 56 + (16 * self.total_pages)
        for i in range(self.total_pages):
            if progress_callback: progress_callback((i + 1) / self.total_pages)
            img = self.render_page(i).convert("L").point(lambda p: 255 if p > 128 else 0, mode='1')
            w, h = img.size
            xtg = struct.pack("<IHHBBIQ", 0x00475458, w, h, 0, 0, ((w + 7) // 8) * h, 0) + img.tobytes()
            idx.extend(struct.pack("<QIHH", data_off + len(blob), len(xtg), w, h))
            blob.extend(xtg)
        header = struct.pack("<IHHBBBBIQQQQQ", 0x00435458, 0x0100, self.total_pages, 0, 0, 0, 0, 0, 0, 56, data_off, 0,
                             0)
        with open(out_name, "wb") as f:
            f.write(header + idx + blob)


# --- GUI APPLICATION ---

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.processor = EpubProcessor()
        self.current_page_index = 0
        self.title("EPUB to XTC Converter")
        self.geometry("1100x950")
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.sidebar = ctk.CTkScrollableFrame(self, width=300, corner_radius=0)
        self.sidebar.grid(row=0, column=0, sticky="nsew")

        ctk.CTkButton(self.sidebar, text="Select EPUB", command=self.select_file).pack(padx=20, pady=(20, 5), fill="x")
        self.lbl_file = ctk.CTkLabel(self.sidebar, text="No file", text_color="gray")
        self.lbl_file.pack()

        self.var_toc = ctk.BooleanVar(value=True)
        self.check_toc = ctk.CTkCheckBox(self.sidebar, text="Generate TOC Pages", variable=self.var_toc)
        self.check_toc.pack(padx=20, pady=10, anchor="w")

        ctk.CTkLabel(self.sidebar, text="Text Alignment:").pack(pady=(10, 0))
        self.align_dropdown = ctk.CTkOptionMenu(self.sidebar, values=["justify", "left"])
        self.align_dropdown.set("justify")
        self.align_dropdown.pack(padx=20, pady=5, fill="x")

        ctk.CTkLabel(self.sidebar, text="Select Font:").pack(pady=(10, 0))
        self.available_fonts = get_local_fonts()
        self.font_options = ["Default (System)"] + [os.path.basename(f) for f in self.available_fonts]
        self.font_map = {os.path.basename(f): f for f in self.available_fonts}
        self.font_map["Default (System)"] = "DEFAULT"
        self.font_dropdown = ctk.CTkOptionMenu(self.sidebar, values=self.font_options, command=self.on_font_change)
        self.font_dropdown.pack(padx=20, pady=5, fill="x")

        self.lbl_size = ctk.CTkLabel(self.sidebar, text=f"Font Size: {DEFAULT_FONT_SIZE}pt")
        self.lbl_size.pack()
        self.slider_size = ctk.CTkSlider(self.sidebar, from_=12, to=36, command=self.update_size_label)
        self.slider_size.set(DEFAULT_FONT_SIZE)
        self.slider_size.pack(padx=20, pady=5, fill="x")

        self.lbl_weight = ctk.CTkLabel(self.sidebar, text=f"Font Weight: {DEFAULT_FONT_WEIGHT}")
        self.lbl_weight.pack()
        self.slider_weight = ctk.CTkSlider(self.sidebar, from_=100, to=900, number_of_steps=8,
                                           command=self.update_weight_label)
        self.slider_weight.set(DEFAULT_FONT_WEIGHT)
        self.slider_weight.pack(padx=20, pady=5, fill="x")

        self.lbl_line = ctk.CTkLabel(self.sidebar, text=f"Line Height: {DEFAULT_LINE_HEIGHT}")
        self.lbl_line.pack()
        self.slider_line = ctk.CTkSlider(self.sidebar, from_=1.0, to=2.5, command=self.update_line_label)
        self.slider_line.set(DEFAULT_LINE_HEIGHT)
        self.slider_line.pack(padx=20, pady=5, fill="x")

        self.lbl_margin = ctk.CTkLabel(self.sidebar, text=f"Margin: {DEFAULT_MARGIN}px")
        self.lbl_margin.pack()
        self.slider_margin = ctk.CTkSlider(self.sidebar, from_=0, to=100, command=self.update_margin_label)
        self.slider_margin.set(DEFAULT_MARGIN)
        self.slider_margin.pack(padx=20, pady=5, fill="x")

        self.lbl_top_padding = ctk.CTkLabel(self.sidebar, text=f"Top Padding: {DEFAULT_TOP_PADDING}px")
        self.lbl_top_padding.pack()
        self.slider_top_padding = ctk.CTkSlider(self.sidebar, from_=0, to=100, command=self.update_top_padding_label)
        self.slider_top_padding.set(DEFAULT_TOP_PADDING)
        self.slider_top_padding.pack(padx=20, pady=5, fill="x")

        self.lbl_padding = ctk.CTkLabel(self.sidebar, text=f"Bottom Padding: {DEFAULT_BOTTOM_PADDING}px")
        self.lbl_padding.pack()
        self.slider_padding = ctk.CTkSlider(self.sidebar, from_=0, to=100, command=self.update_padding_label)
        self.slider_padding.set(DEFAULT_BOTTOM_PADDING)
        self.slider_padding.pack(padx=20, pady=5, fill="x")

        self.btn_run = ctk.CTkButton(self.sidebar, text="Process / Update Preview", fg_color="green",
                                     command=self.run_processing)
        self.btn_run.pack(padx=20, pady=20, fill="x")

        self.btn_export = ctk.CTkButton(self.sidebar, text="Export XTC", state="disabled", command=self.export_file)
        self.btn_export.pack(padx=20, pady=5, fill="x")

        self.progress_bar = ctk.CTkProgressBar(self.sidebar)
        self.progress_bar.set(0)
        self.progress_bar.pack(padx=20, pady=10, fill="x")
        self.progress_label = ctk.CTkLabel(self.sidebar, text="Progress: Ready")
        self.progress_label.pack()

        self.preview_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.preview_frame.grid(row=0, column=1, sticky="nsew", padx=20, pady=20)
        self.img_label = ctk.CTkLabel(self.preview_frame, text="Load EPUB to Preview")
        self.img_label.pack(expand=True, fill="both")

        self.nav = ctk.CTkFrame(self.preview_frame, fg_color="transparent")
        self.nav.pack(side="bottom", fill="x", pady=10)
        ctk.CTkButton(self.nav, text="< Previous", width=100, command=self.prev_page).pack(side="left", padx=20)
        self.lbl_page = ctk.CTkLabel(self.nav, text="Page 0/0", font=("Arial", 16))
        self.lbl_page.pack(side="left", expand=True)
        ctk.CTkButton(self.nav, text="Next >", width=100, command=self.next_page).pack(side="right", padx=20)

    def select_file(self):
        path = filedialog.askopenfilename(filetypes=[("EPUB", "*.epub")])
        if path:
            self.processor.input_file = path
            self.lbl_file.configure(text=os.path.basename(path))

    def on_font_change(self, choice):
        self.processor.font_path = self.font_map[choice]

    def update_size_label(self, value):
        self.lbl_size.configure(text=f"Font Size: {int(value)}pt")

    def update_weight_label(self, value):
        self.lbl_weight.configure(text=f"Font Weight: {int(value)}")

    def update_line_label(self, value):
        self.lbl_line.configure(text=f"Line Height: {value:.1f}")

    def update_margin_label(self, value):
        self.lbl_margin.configure(text=f"Margin: {int(value)}px")

    def update_padding_label(self, value):
        self.lbl_padding.configure(text=f"Bottom Padding: {int(value)}px")

    def update_top_padding_label(self, value):
        self.lbl_top_padding.configure(text=f"Top Padding: {int(value)}px")

    def update_progress_ui(self, val, stage_text="Processing"):
        self.after(0, lambda: self.progress_bar.set(val))
        self.after(0, lambda: self.progress_label.configure(text=f"{stage_text}: {int(val * 100)}%"))

    def run_processing(self):
        if not self.processor.input_file:
            messagebox.showwarning("Warning", "Please select an EPUB file first.")
            return
        self.btn_run.configure(state="disabled", text="Processing...")
        threading.Thread(target=self._task).start()

    def _task(self):
        success = self.processor.load_and_layout(
            self.processor.input_file,
            self.processor.font_path,
            int(self.slider_size.get()),
            int(self.slider_margin.get()),
            float(self.slider_line.get()),
            int(self.slider_weight.get()),
            int(self.slider_padding.get()),
            int(self.slider_top_padding.get()),
            text_align=self.align_dropdown.get(),
            add_toc=self.var_toc.get(),
            progress_callback=lambda v: self.update_progress_ui(v, "Layout")
        )
        self.after(0, lambda: self._done(success))

    def _done(self, success):
        self.btn_run.configure(state="normal", text="Process / Update Preview")
        if success:
            self.btn_export.configure(state="normal")
            self.show_page(0)
        else:
            messagebox.showerror("Error", "Processing failed.")

    def show_page(self, idx):
        if not self.processor.is_ready: return
        self.current_page_index = idx
        img = self.processor.render_page(idx)
        available_h = max(100, self.preview_frame.winfo_height() - 100)
        w = int(img.width * (available_h / img.height))
        ctk_img = ctk.CTkImage(light_image=img, size=(w, available_h))
        self.img_label.configure(image=ctk_img, text="")
        self.lbl_page.configure(text=f"Page {idx + 1} / {self.processor.total_pages}")

    def prev_page(self):
        self.show_page(max(0, self.current_page_index - 1))

    def next_page(self):
        self.show_page(min(self.processor.total_pages - 1, self.current_page_index + 1))

    def export_file(self):
        path = filedialog.asksaveasfilename(defaultextension=".xtc")
        if path: threading.Thread(target=lambda: self._run_export(path)).start()

    def _run_export(self, path):
        self.processor.save_xtc(path, progress_callback=lambda v: self.update_progress_ui(v, "Exporting"))
        self.after(0, lambda: messagebox.showinfo("Success", "XTC file saved."))


if __name__ == "__main__":
    app = App()
    app.mainloop()