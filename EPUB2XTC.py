import os
import sys
import struct
import fitz  # PyMuPDF
from PIL import Image, ImageEnhance, ImageDraw, ImageFont, ImageOps, ImageFilter
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup, NavigableString
import pyphen
import base64
import customtkinter as ctk
from tkinter import filedialog, messagebox, simpledialog
import threading
import re
import json
import glob
import io
from urllib.parse import unquote
import time
import hashlib
import csv  # Added for AoA parsing
import tempfile

# --- OPTIONAL DEPENDENCIES ---
try:
    import wordfreq

    HAS_WORDFREQ = True
except ImportError:
    HAS_WORDFREQ = False

try:
    from openai import OpenAI

    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

# --- CONFIGURATION DEFAULTS ---
DEFAULT_SCREEN_WIDTH = 480
DEFAULT_SCREEN_HEIGHT = 800
DEFAULT_RENDER_SCALE = 3.0

# --- FACTORY DEFAULTS ---
FACTORY_DEFAULTS = {
    "font_size": 28,
    "font_weight": 400,
    "line_height": 1.4,
    "margin": 20,
    "top_padding": 15,
    "bottom_padding": 45,
    "orientation": "Portrait",
    "text_align": "justify",
    "font_name": "Default (System)",
    "preview_zoom": 440,
    "generate_toc": True,
    "show_footnotes": False,

    # --- SPECTRA AI ANNOTATIONS ---
    "spectra_enabled": False,
    "spectra_threshold": 3.8,  # Zipf score
    "spectra_aoa_threshold": 10.0,  # NEW: AoA Threshold (0 = disabled)
    "spectra_api_key": "",
    "spectra_base_url": "https://api.openai.com/v1",
    "spectra_model": "gpt-4o-mini",
    "spectra_language": "en",

    # --- ELEMENT VISIBILITY & POSITION ---
    "pos_title": "Footer",
    "pos_pagenum": "Footer",
    "pos_chap_page": "Hidden",
    "pos_percent": "Hidden",

    # --- ELEMENT ORDERING ---
    "order_pagenum": 1,
    "order_title": 2,
    "order_chap_page": 3,
    "order_percent": 4,

    # --- PROGRESS BAR POSITION ---
    "pos_progress": "Footer (Below Text)",

    # --- PROGRESS BAR STYLING ---
    "bar_height": 4,
    "bar_show_marker": True,
    "bar_marker_color": "Black",
    "bar_marker_radius": 5,
    "bar_show_ticks": True,
    "bar_tick_height": 6,

    # --- HEADER/FOOTER STYLING ---
    "header_font_size": 16,
    "header_align": "Center",
    "header_margin": 10,
    "footer_font_size": 16,
    "footer_align": "Center",
    "footer_margin": 10,

    # --- RENDER MODES ---
    "bit_depth": "1-bit (XTG)",
    "render_mode": "Threshold",
    "white_clip": 220,
    "contrast": 1.2,
    "text_threshold": 130,
    "text_blur": 1.0,
}


if getattr(sys, 'frozen', False):
    # Running as compiled PyInstaller executable
    EXTERNAL_DIR = os.path.dirname(sys.executable) # Outside the .exe (User can see/edit)
    INTERNAL_DIR = sys._MEIPASS                    # Inside the .exe (Hidden, read-only)
else:
    # Running as a standard Python script
    EXTERNAL_DIR = os.path.dirname(os.path.abspath(__file__))
    INTERNAL_DIR = EXTERNAL_DIR

# --- EXTERNAL FILES (User can edit these or drop files here) ---
SETTINGS_FILE = os.path.join(EXTERNAL_DIR, "default_settings.json")
PRESETS_DIR = os.path.join(EXTERNAL_DIR, "presets")

# --- INTERNAL FILES (Bundled inside the .exe so you don't have to distribute them) ---
AOA_FILE = os.path.join(INTERNAL_DIR, "AoA_51715_words.csv")

# Global AoA Database
AOA_DB = {}

def load_aoa_database():
    """Loads the Kuperman AoA CSV into a global dict."""
    global AOA_DB
    if not os.path.exists(AOA_FILE):
        print(f"AoA file not found: {AOA_FILE}")
        return

    try:
        with open(AOA_FILE, 'r', encoding='utf-8', errors='ignore') as f:
            reader = csv.DictReader(f)

            # Check required columns
            # Note: We now look for the lemmatized column as a fallback
            if 'Word' not in reader.fieldnames:
                print("AoA CSV missing 'Word' column.")
                return

            count = 0
            for row in reader:
                word = row['Word'].strip().lower()

                # 1. Try exact word AoA first
                val = row.get('AoA_Kup')

                # 2. If empty, try Lemmatized AoA (Fallback for plurals like 'dinners')
                if not val or val.lower() == 'na':
                    val = row.get('AoA_Kup_lem')

                try:
                    if val and val.lower() != 'na':
                        AOA_DB[word] = float(val)
                        count += 1
                except ValueError:
                    continue

            print(f"Loaded {count} words from AoA database.")
    except Exception as e:
        print(f"Error loading AoA CSV: {e}")


# --- SPECTRA ANNOTATOR CLASS (PHRASE AWARE) ---
class SpectraAnnotator:
    def __init__(self, api_key, base_url, model, threshold=4.0, aoa_threshold=0.0, language='en',
                 target_lang='English'):
        self.enabled = HAS_WORDFREQ and HAS_OPENAI and bool(api_key)
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.threshold = threshold  # Zipf Threshold (Upper bound)
        self.aoa_threshold = aoa_threshold  # AoA Threshold (Lower bound)
        self.language = language
        self.target_lang = target_lang
        self.client = None
        # Cache Key: "word|context_hash" -> Value: "Definition"
        self.master_cache = {}

        if self.enabled:
            try:
                self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)
            except Exception as e:
                print(f"Spectra Init Error: {e}")
                self.enabled = False

    def get_difficulty(self, word):
        if not HAS_WORDFREQ: return 10.0
        return wordfreq.zipf_frequency(word, self.language)

    def get_aoa(self, word):
        """Returns Age of Acquisition or 0 if not found."""
        return AOA_DB.get(word, 0.0)

    def fetch_definitions_batch(self, word_context_map, force=False):
        if not word_context_map or not self.client: return {}

        # Determine what to fetch (all or only missing)
        if force:
            to_fetch = word_context_map
        else:
            to_fetch = {k: ctx for k, ctx in word_context_map.items() if k not in self.master_cache}

        if not to_fetch: return {}

        # 1. BUILD THE BATCH LIST
        items_str = ""
        for unique_key, context in to_fetch.items():
            target_word = unique_key.split('|')[0]
            clean_context = context.replace('"', "'").replace('\n', ' ').strip()[:300]
            items_str += (
                f"ID: {unique_key}\n"
                f"WORD: {target_word}\n"
                f"CONTEXT: {clean_context}\n"
                f"---\n"
            )

        # 2. DEFINE THE OUTPUT SCHEMA (JSON)
        json_schema = {
            "name": "dictionary_response",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "entries": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "definition": {"type": "string"}
                            },
                            "required": ["id", "definition"],
                            "additionalProperties": False
                        }
                    }
                },
                "required": ["entries"],
                "additionalProperties": False
            }
        }
        # Map your UI dropdown names to standard EPUB 2-letter codes
        lang_map = {
            "English": "en", "Spanish": "es", "French": "fr",
            "German": "de", "Italian": "it", "Polish": "pl",
            "Portuguese": "pt", "Russian": "ru", "Chinese": "zh", "Japanese": "ja"
        }

        # Get the 2-letter code for the selected target (fallback to 'en' if not found)
        target_code = lang_map.get(self.target_lang, "en")

        # Get the 2-letter code of the book itself (slicing [:2] handles "en-US" or "en-GB")
        book_code = self.language.lower()[:2]

        # It's a translation if the codes don't match!
        is_translation = (target_code != book_code)

        if is_translation:
            task_instruction = f"TASK: TRANSLATE 'WORD' accurately into {self.target_lang} based on the 'CONTEXT'."
            # Added back the anti-generalization rule to prevent "crossbow" -> "bow"
            rule_1 = "1. Provide the exact, literal translation. Do NOT simplify or generalize physical objects."
            # Define grammar_rule here so Python doesn't crash!
            grammar_rule = "4. Output the word in its base dictionary form (nominative/infinitive)."
        else:
            task_instruction = f"TASK: Provide a direct, simpler SYNONYM for 'WORD' in {self.target_lang} that fits the 'CONTEXT'."
            rule_1 = "1. The synonym must be easier to understand but MUST preserve the exact nuance."
            # Define grammar_rule here for English-to-English replacements
            grammar_rule = "4. Match the grammatical form (plural, past tense, -ing, etc.) of the original word perfectly so it can directly replace it in the sentence."

        prompt = (
            f"{task_instruction}\n"
            f"RULES:\n"
            f"{rule_1}\n"
            f"2. Do NOT simply describe what is happening; output the direct replacement word itself.\n"
            f"3. Keep responses extremely short (1-2 words maximum).\n"
            f"{grammar_rule}\n"
            f"5. Output valid JSON.\n"
            f"\n"
            f"INPUT LIST:\n"
            f"{items_str}"
        )

        try:
            # 4. SEND TO API
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": f"You are a dictionary. Output JSON only. You answer only in {self.target_lang}, regardless of the input language."},
                    {"role": "user", "content": prompt}
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": json_schema
                },
                temperature=0.2
            )

            # 5. PROCESS RESPONSE
            content = response.choices[0].message.content.strip()

            data = {}
            try:
                # Try standard JSON parsing first
                clean = re.sub(r'^```json\s*', '', content)
                clean = re.sub(r'\s*```$', '', clean)
                data = json.loads(clean)
            except json.JSONDecodeError:
                # --- ROBUST FALLBACK FOR GEMMA/SMALL MODELS ---

                pattern = r'"id":\s*"([^"]+)",\s*"definition":\s*"([^"]+)"'
                matches = re.findall(pattern, content)
                if matches:
                    data = {"entries": [{"id": m[0], "definition": m[1]} for m in matches]}

            if "entries" in data and isinstance(data["entries"], list):
                for entry in data["entries"]:
                    key = entry.get("id")
                    val = entry.get("definition")
                    if key and val and key in to_fetch:

                        # --- NEW: Sanitize the AI output ---
                        # 1. Strip whitespace and remove trailing punctuation
                        clean_val = val.strip().rstrip('.,;!')

                        # 2. Preserve capitalization for German nouns, lowercase everything else
                        if self.target_lang != "German":
                            clean_val = clean_val.lower()

                        self.master_cache[key] = clean_val

        except Exception as e:
            print(f"Spectra API Error: {e}")

    def analyze_chapters(self, chapters_list, selected_indices, progress_callback=None, force=False):
        if not self.enabled: return

        word_pattern = re.compile(r'\b[a-zA-Z]{3,}\b')
        split_pattern = r'(?<!\bMr)(?<!\bMrs)(?<!\bDr)(?<!\bMs)(?<!\bSt)(?<!\bProf)(?<!\bCapt)(?<!\bGen)(?<!\bSen)(?<!\bRev)(?<=[.!?])\s+'

        # We accumulate all unique instances here
        batch_items = {}

        total = len(selected_indices)
        for i, idx in enumerate(selected_indices):
            if progress_callback: progress_callback(0.1 + (i / total) * 0.4)

            soup = chapters_list[idx]['soup']
            full_text = soup.get_text(" ", strip=True)
            sentences = re.split(split_pattern, full_text)

            for sentence in sentences:
                sentence = sentence.strip()
                if not sentence: continue

                # Generate a short hash of the sentence context
                ctx_hash = hashlib.md5(sentence.encode('utf-8')).hexdigest()[:6]

                for match in word_pattern.finditer(sentence):
                    word_val = match.group()
                    start_index = match.start()

                    # --- DEFINITION MUST BE HERE (Before the filter) ---
                    w_lower = word_val.lower()
                    # ---------------------------------------------------

                    # --- SMART PROPER NOUN FILTER ---
                    if word_val[0].isupper():
                        # 1. If in middle of sentence, it's a Name/Place -> Skip
                        if start_index > 0:
                            continue

                        # 2. If at start, check if it's a known common word (using AoA DB)
                        # Priority 1: Check your CSV database
                        is_known = False
                        if AOA_DB and w_lower in AOA_DB:
                            is_known = True
                        # Priority 2: Fallback to WordFreq if CSV missing
                        elif not AOA_DB and HAS_WORDFREQ:
                            if wordfreq.zipf_frequency(w_lower, self.language) > 1.0:
                                is_known = True

                        # If it's a start-of-sentence Capital but NOT a known word -> Skip
                        if not is_known:
                            continue
                    # --------------------------------

                    # --- DUAL SLIDER LOGIC ---
                    zipf_score = self.get_difficulty(w_lower)

                    # 1. Zipf Check (Must be RARER than threshold)
                    is_candidate = (1.5 < zipf_score < self.threshold)

                    # 2. AoA Check (Only if enabled)
                    if is_candidate and self.aoa_threshold > 0.5 and self.language == 'en':
                        aoa_val = self.get_aoa(w_lower)
                        if aoa_val > 0:
                            # If word has AoA data, it must be OLDER than the slider
                            if aoa_val < self.aoa_threshold:
                                is_candidate = False

                    if is_candidate:
                        unique_key = f"{w_lower}|{ctx_hash}"
                        if force or unique_key not in self.master_cache:
                            batch_items[unique_key] = sentence

        # Process Batch
        if batch_items:
            items = list(batch_items.items())

            # Dynamically adjust limits based on the Base URL
            is_local = "localhost" in self.base_url or "127.0.0.1" in self.base_url

            batch_size = 15 if is_local else 40
            sleep_time = 0.1 if is_local else 4.5

            total_batches = (len(items) + batch_size - 1) // batch_size

            for b_idx, i in enumerate(range(0, len(items), batch_size)):
                if progress_callback:
                    pct = 0.5 + (b_idx / total_batches) * 0.5
                    progress_callback(pct)

                batch = dict(items[i:i + batch_size])
                self.fetch_definitions_batch(batch, force=force)

                time.sleep(sleep_time)

    def get_ordered_annotations(self, soup):
        if not self.master_cache: return {}
        ordered_defs = {}
        full_text = soup.get_text(" ", strip=True)
        split_pattern = r'(?<!\bMr)(?<!\bMrs)(?<!\bDr)(?<!\bMs)(?<!\bSt)(?<!\bProf)(?<!\bCapt)(?<!\bGen)(?<!\bSen)(?<!\bRev)(?<=[.!?])\s+'
        sentences = re.split(split_pattern, full_text)
        word_pattern = re.compile(r'\b[a-zA-Z]{3,}\b')

        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence: continue
            ctx_hash = hashlib.md5(sentence.encode('utf-8')).hexdigest()[:6]

            matches = word_pattern.findall(sentence)
            for w in matches:
                w_lower = w.lower()
                unique_key = f"{w_lower}|{ctx_hash}"
                if unique_key in self.master_cache:
                    if w_lower not in ordered_defs:
                        ordered_defs[w_lower] = []
                    ordered_defs[w_lower].append(self.master_cache[unique_key])
        return ordered_defs


# --- UTILITY FUNCTIONS ---
def fix_css_font_paths(css_text, target_font_family="'CustomFont'"):
    if target_font_family is None:
        return css_text
    css_text = re.sub(r'font-family\s*:\s*[^;!]+', f'font-family: {target_font_family}', css_text)
    return css_text


def get_font_variants(font_path):
    if not font_path or not os.path.exists(font_path):
        return {}
    directory = os.path.dirname(font_path)
    try:
        all_files = [f for f in os.listdir(directory) if f.lower().endswith((".ttf", ".otf"))]
    except:
        return {}
    candidates = {"regular": [], "italic": [], "bold": [], "bold_italic": []}

    for f in all_files:
        full_path = os.path.join(directory, f).replace("\\", "/")
        name_lower = f.lower()
        has_bold = any(x in name_lower for x in ["bold", "bd"])
        has_italic = any(x in name_lower for x in ["italic", "oblique", "obl"])
        if has_bold and has_italic:
            candidates["bold_italic"].append(full_path)
            continue
        if has_bold and not has_italic:
            candidates["bold"].append(full_path)
            continue
        if has_italic and not has_bold:
            candidates["italic"].append(full_path)
            continue
        candidates["regular"].append(full_path)

    def pick_best(file_list):
        if not file_list: return None
        return sorted(file_list, key=lambda p: len(os.path.basename(p)))[0]

    return {
        "regular": font_path.replace("\\", "/"),
        "italic": pick_best(candidates["italic"]),
        "bold": pick_best(candidates["bold"]),
        "bold_italic": pick_best(candidates["bold_italic"])
    }


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

    def add_entry(href, title):
        if '#' in href:
            href_clean, anchor = href.split('#', 1)
        else:
            href_clean, anchor = href, None
        filename = os.path.basename(href_clean)
        if filename not in mapping:
            mapping[filename] = []
        mapping[filename].append((anchor, title))

    def process_toc_item(item):
        if isinstance(item, tuple):
            if len(item) > 1 and isinstance(item[1], list):
                for sub in item[1]: process_toc_item(sub)
        elif isinstance(item, epub.Link):
            add_entry(item.href, item.title)

    for item in book.toc:
        process_toc_item(item)

    if not mapping:
        nav_item = next((item for item in book.get_items() if item.get_type() == ebooklib.ITEM_NAVIGATION), None)
        if nav_item:
            try:
                soup = BeautifulSoup(nav_item.get_content(), 'html.parser')
                nav_element = soup.find('nav', attrs={'epub:type': 'toc'}) or soup.find('nav')
                if nav_element:
                    for link in nav_element.find_all('a', href=True):
                        add_entry(link['href'], link.get_text().strip())
            except:
                pass
    return mapping


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
        if text_node.parent.name in ['script', 'style', 'head', 'title', 'meta', 'rt']: continue
        if not text_node.strip(): continue
        original_text = str(text_node)
        clean_text = original_text.replace('\u00A0', ' ')

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
    font_map = {}
    if os.path.exists(fonts_dir):
        for item in os.listdir(fonts_dir):
            item_path = os.path.join(fonts_dir, item)
            if os.path.isdir(item_path):
                files = [f for f in os.listdir(item_path) if f.lower().endswith((".ttf", ".otf"))]
                if not files: continue
                candidates = [f for f in files if "bold" not in f.lower() and "italic" not in f.lower()]
                main_file = candidates[0] if candidates else files[0]
                font_map[item] = os.path.abspath(os.path.join(item_path, main_file))
        for item in os.listdir(fonts_dir):
            item_path = os.path.join(fonts_dir, item)
            if os.path.isfile(item_path) and item.lower().endswith((".ttf", ".otf")):
                font_map[item] = os.path.abspath(item_path)
    return font_map


# --- DIALOG CLASSES ---
class CoverExportDialog(ctk.CTkToplevel):
    def __init__(self, parent, callback):
        super().__init__(parent)
        self.callback = callback
        self.title("Export Cover Settings")
        self.geometry("400x350")
        self.transient(parent)
        self.grab_set()
        ctk.CTkLabel(self, text="Export Cover as BMP", font=("Arial", 16, "bold")).pack(pady=15)
        self.frm_dim = ctk.CTkFrame(self, fg_color="transparent")
        self.frm_dim.pack(pady=10)
        ctk.CTkLabel(self.frm_dim, text="Width:").grid(row=0, column=0, padx=5)
        self.entry_w = ctk.CTkEntry(self.frm_dim, width=60)
        self.entry_w.insert(0, "480")
        self.entry_w.grid(row=0, column=1, padx=5)
        ctk.CTkLabel(self.frm_dim, text="Height:").grid(row=0, column=2, padx=5)
        self.entry_h = ctk.CTkEntry(self.frm_dim, width=60)
        self.entry_h.insert(0, "800")
        self.entry_h.grid(row=0, column=3, padx=5)
        ctk.CTkLabel(self, text="Scaling Mode:").pack(pady=(15, 5))
        self.mode_var = ctk.StringVar(value="Crop to Fill (Best)")
        modes = ["Crop to Fill (Best)", "Fit (Add White Bars)", "Stretch (Distort)"]
        ctk.CTkOptionMenu(self, variable=self.mode_var, values=modes).pack(pady=5)
        ctk.CTkLabel(self,
                     text="Crop to Fill: Fills the screen, cuts off edges if ratio differs.\nFit: Keeps entire image, adds white bars.\nStretch: Forces image to size, may look squashed.",
                     font=("Arial", 12), text_color="gray").pack(pady=10)
        ctk.CTkButton(self, text="Export", command=self.confirm).pack(pady=20)

    def confirm(self):
        try:
            w = int(self.entry_w.get())
            h = int(self.entry_h.get())
        except ValueError:
            messagebox.showerror("Error", "Width and Height must be numbers.")
            return
        mode = self.mode_var.get()
        self.callback(w, h, mode)
        self.grab_release()
        self.destroy()


class ChapterSelectionDialog(ctk.CTkToplevel):
    def __init__(self, parent, chapters_list, callback):
        super().__init__(parent)
        self.callback = callback
        self.chapters_list = chapters_list
        self.title("Select Chapters for TOC")
        self.geometry("500x600")
        self.transient(parent)
        self.grab_set()
        self.lbl_info = ctk.CTkLabel(self,
                                     text="Uncheck chapters to hide them from the TOC and Progress Bar.\n(They will still be readable in the book)",
                                     wraplength=450)
        self.lbl_info.pack(pady=10)
        self.btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.btn_frame.pack(fill="x", padx=20)
        ctk.CTkButton(self.btn_frame, text="Select All", width=100, command=self.select_all).pack(side="left", padx=5)
        ctk.CTkButton(self.btn_frame, text="Select None", width=100, command=self.select_none).pack(side="left", padx=5)
        self.scroll_frame = ctk.CTkScrollableFrame(self)
        self.scroll_frame.pack(fill="both", expand=True, padx=20, pady=10)
        self.check_vars = []
        for i, chap in enumerate(chapters_list):
            is_generic = re.match(r"^Section \d+$", chap['title'])
            var = ctk.BooleanVar(value=not is_generic)
            self.check_vars.append(var)
            chk = ctk.CTkCheckBox(self.scroll_frame, text=f"{i + 1}. {chap['title']}", variable=var)
            chk.pack(anchor="w", pady=2, padx=5)
        self.btn_confirm = ctk.CTkButton(self, text="Confirm Selection", command=self.confirm)
        self.btn_confirm.pack(pady=20)

    def select_all(self):
        for var in self.check_vars: var.set(True)

    def select_none(self):
        for var in self.check_vars: var.set(False)

    def confirm(self):
        selected_indices = [i for i, var in enumerate(self.check_vars) if var.get()]
        if not selected_indices:
            if not messagebox.askyesno("Warning",
                                       "No chapters selected for TOC. The book will have no navigation. Continue?"): return
        self.grab_release()
        self.destroy()
        self.callback(selected_indices)


# --- EPUB PROCESSOR ---
class EpubProcessor:
    def __init__(self):
        self.input_file = ""
        self.raw_chapters = []
        self.book_css = ""
        self.book_images = {}
        self.book_lang = 'en'
        self.cover_image_obj = None
        self.font_path = ""
        self.font_size = FACTORY_DEFAULTS["font_size"]
        self.margin = FACTORY_DEFAULTS["margin"]
        self.line_height = FACTORY_DEFAULTS["line_height"]
        self.font_weight = FACTORY_DEFAULTS["font_weight"]
        self.bottom_padding = FACTORY_DEFAULTS["bottom_padding"]
        self.top_padding = FACTORY_DEFAULTS["top_padding"]
        self.text_align = FACTORY_DEFAULTS["text_align"]
        self.screen_width = DEFAULT_SCREEN_WIDTH
        self.screen_height = DEFAULT_SCREEN_HEIGHT
        self.layout_settings = {}
        self.fitz_docs = []
        self.toc_data_final = []
        self.toc_pages_images = []
        self.page_map = []
        self.total_pages = 0
        self.toc_items_per_page = 18
        self.is_ready = False
        self.global_id_map = {}
        self.annotator = None  # Instance of SpectraAnnotator

    def _split_html_by_toc(self, soup, toc_entries):
        chunks = []
        if len(toc_entries) == 1 and not toc_entries[0][0]:
            return [{'title': toc_entries[0][1], 'soup': soup}]
        split_points = []
        for anchor, title in toc_entries:
            target = None
            if anchor: target = soup.find(id=anchor)
            if target or not anchor: split_points.append({'node': target, 'title': title})
        if not split_points: return [{'title': toc_entries[0][1], 'soup': soup}]

        current_idx = 0
        current_soup = BeautifulSoup("<body></body>", 'html.parser')
        body_children = list(soup.body.children) if soup.body else []
        for child in body_children:
            if isinstance(child, NavigableString) and not child.strip():
                if current_soup.body: current_soup.body.append(child.extract() if hasattr(child, 'extract') else child)
                continue
            if current_idx + 1 < len(split_points):
                next_node = split_points[current_idx + 1]['node']
                is_nested_target = False
                if hasattr(child, 'find_all'):
                    if next_node in child.find_all(): is_nested_target = True
                if next_node and (child == next_node or is_nested_target):
                    chunks.append({'title': split_points[current_idx]['title'], 'soup': current_soup})
                    current_idx += 1
                    current_soup = BeautifulSoup("<body></body>", 'html.parser')
            if current_soup.body: current_soup.body.append(child)
        chunks.append({'title': split_points[current_idx]['title'], 'soup': current_soup})
        return chunks

    def _smart_extract_content(self, elem):
        if elem.name == 'a':
            parent = elem.parent
            if parent and parent.name not in ['body', 'html', 'section']: return parent
            return elem
        if elem.name in ['aside', 'li', 'dd', 'div']: return elem
        text = elem.get_text(strip=True)
        if len(text) > 1: return elem
        parent = elem.parent
        if parent:
            if parent.name in ['body', 'html', 'section']: return elem
            return parent
        return elem

    def _build_global_id_map(self, book):
        id_map = {}
        for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
            try:
                soup = BeautifulSoup(item.get_content(), 'html.parser')
                filename = os.path.basename(item.get_name())
                for elem in soup.find_all(id=True):
                    target_node = self._smart_extract_content(elem)
                    import copy
                    content_node = copy.copy(target_node)
                    original_raw_html = content_node.decode_contents().strip()
                    for a in content_node.find_all('a'):
                        if a.get('role') in ['doc-backlink', 'doc-noteref']:
                            a.decompose()
                            continue
                        text = a.get_text(strip=True)
                        if any(x in text for x in ['↑', 'site', 'back', 'return', '↩']):
                            a.decompose()
                            continue
                        if len(text) < 5 and re.match(r'^[\s\[\(]*\d+[\.\)\]]*$', text):
                            a.decompose()
                            continue
                    final_html = content_node.decode_contents().strip()
                    if not final_html and original_raw_html: final_html = original_raw_html
                    if final_html: id_map[f"{filename}#{elem['id']}"] = final_html
            except Exception:
                pass
        return id_map

    def _inject_inline_footnotes(self, soup, current_filename):
        if not self.global_id_map: return soup
        links = soup.find_all('a', href=True)
        for link in reversed(list(links)):
            raw_href = link['href']
            href = unquote(raw_href)
            text = link.get_text(strip=True)
            if not text and not link.find('sup'): continue
            parent_classes = []
            for parent in link.parents:
                if parent.get('class'): parent_classes.extend(parent.get('class'))
            if any(x in [c.lower() for c in parent_classes] for x in
                   ['footnote', 'endnote', 'reflist', 'bibliography']): continue
            is_footnote = False
            if 'noteref' in link.get('epub:type', '') or link.get('role') == 'doc-noteref': is_footnote = True
            css = link.get('class', [])
            if isinstance(css, list): css = " ".join(css)
            if any(x in css.lower() for x in ['footnote', 'noteref', 'ref']): is_footnote = True
            if not is_footnote and text:
                clean_t = text.strip()
                if re.match(r'^[\(\[]?\d+[\)\]]?$', clean_t) or clean_t == '*':
                    is_footnote = True
                elif re.match(r'^[\(\[]?[ivx]+[\)\]]?$', clean_t.lower()):
                    is_footnote = True
            if not is_footnote: continue
            content = None
            if '#' in href:
                parts = href.rsplit('#', 1)
                href_path = parts[0]
                href_id = parts[1]
                f_name = os.path.basename(href_path) if href_path else current_filename
                key = f"{f_name}#{href_id}"
                content = self.global_id_map.get(key)
                if not content:
                    suffix = f"#{href_id}"
                    for k, v in self.global_id_map.items():
                        if k.endswith(suffix):
                            content = v
                            break
            if content:
                new_marker = soup.new_tag("sup")
                new_marker.string = text if text else "*"
                new_marker['class'] = "fn-marker"
                link.replace_with(new_marker)
                note_box = soup.new_tag("div")
                note_box['class'] = "inline-footnote-box"
                header = soup.new_tag("strong")
                header.string = f"{text}: "
                note_box.append(header)
                content_soup = BeautifulSoup(content, 'html.parser')
                note_box.append(content_soup)
                parent_block = new_marker.find_parent(['p', 'div', 'li', 'h1', 'h2', 'blockquote'])
                if parent_block:
                    parent_block.insert_after(note_box)
                else:
                    new_marker.insert_after(note_box)
        return soup

    def _find_cover_image(self, book):
        try:
            cover_data = book.get_metadata('OPF', 'cover')
            if cover_data:
                cover_id = cover_data[0][1]
                item = book.get_item_with_id(cover_id)
                if item: return Image.open(io.BytesIO(item.get_content()))
        except:
            pass
        for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
            name = item.get_name().lower()
            if 'cover' in name: return Image.open(io.BytesIO(item.get_content()))
        for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
            return Image.open(io.BytesIO(item.get_content()))
        return None

    def parse_book_structure(self, input_path):
        self.input_file = input_path
        self.raw_chapters = []
        self.cover_image_obj = None

        try:
            book = epub.read_epub(self.input_file)
        except Exception as e:
            print(f"Error reading EPUB: {e}")
            return False

        # --- CAPTURE METADATA FOR XTC EXPORT ---
        try:
            # Extract Title
            titles = book.get_metadata('DC', 'title')
            self.title_metadata = titles[0][0] if titles else "Unknown Title"

            # Extract Author
            authors = book.get_metadata('DC', 'creator')
            self.author_metadata = authors[0][0] if authors else "Unknown Author"

            # Extract Language
            langs = book.get_metadata('DC', 'language')
            self.book_lang = langs[0][0] if langs else 'en'
        except Exception as e:
            print(f"Metadata extraction error: {e}")
            self.title_metadata = "Unknown Title"
            self.author_metadata = "Unknown Author"
            self.book_lang = 'en'
        self.book_images = extract_images_to_base64(book)
        self.book_css = extract_all_css(book)
        toc_mapping = get_official_toc_mapping(book)
        self.global_id_map = self._build_global_id_map(book)
        items = [book.get_item_with_id(item_ref[0]) for item_ref in book.spine if
                 isinstance(book.get_item_with_id(item_ref[0]), epub.EpubHtml)]
        for item in items:
            item_filename = os.path.basename(item.get_name())
            raw_html = item.get_content().decode('utf-8', errors='replace')
            soup = BeautifulSoup(raw_html, 'html.parser')
            has_image = bool(soup.find('img'))
            toc_entries = toc_mapping.get(item_filename)
            if toc_entries and len(toc_entries) > 1:
                split_chapters = self._split_html_by_toc(soup, toc_entries)
                for chunk in split_chapters:
                    self.raw_chapters.append(
                        {'title': chunk['title'], 'soup': chunk['soup'], 'has_image': bool(chunk['soup'].find('img')),
                         'filename': item_filename})
            else:
                chapter_title = toc_entries[0][1] if toc_entries else None
                if not chapter_title:
                    for tag in ['h1', 'h2', 'h3']:
                        header = soup.find(tag)
                        if header:
                            t = header.get_text().strip()
                            if t and len(t) < 150:
                                chapter_title = t
                                break
                    if not chapter_title: chapter_title = f"Section {len(self.raw_chapters) + 1}"
                self.raw_chapters.append(
                    {'title': chapter_title, 'soup': soup, 'has_image': has_image, 'filename': item_filename})
        return True

    def init_annotator(self, layout_settings):
        target_lang = layout_settings.get("spectra_target_lang", "English")
        new_api_key = layout_settings.get("spectra_api_key", "")
        new_base_url = layout_settings.get("spectra_base_url", "")
        new_model = layout_settings.get("spectra_model", "gpt-4o-mini")

        # Only re-init if needed, else update settings
        if not self.annotator:
            self.annotator = SpectraAnnotator(
                api_key=new_api_key,
                base_url=new_base_url,
                model=new_model,
                threshold=layout_settings.get("spectra_threshold", 4.0),
                aoa_threshold=layout_settings.get("spectra_aoa_threshold", 0.0),
                language=self.book_lang,
                target_lang=target_lang
            )
        else:
            # Update settings on existing instance
            self.annotator.api_key = new_api_key
            self.annotator.base_url = new_base_url
            self.annotator.model = new_model
            self.annotator.threshold = layout_settings.get("spectra_threshold", 4.0)
            self.annotator.aoa_threshold = layout_settings.get("spectra_aoa_threshold", 0.0)
            self.annotator.target_lang = target_lang

            # Re-initialize the OpenAI client with the NEW URL
            if self.annotator.api_key:
                try:
                    self.annotator.client = OpenAI(
                        api_key=self.annotator.api_key,
                        base_url=self.annotator.base_url
                    )
                    self.annotator.enabled = True
                except Exception as e:
                    print(f"Client Init Error: {e}")
                    self.annotator.enabled = False

    def render_chapters(self, selected_indices, font_path, font_size, margin, line_height, font_weight, bottom_padding,
                        top_padding, text_align="justify", orientation="Portrait", add_toc=True, show_footnotes=True,
                        layout_settings=None, progress_callback=None):
        self.font_path = font_path if font_path != "DEFAULT" else ""
        self.font_size = font_size
        self.margin = margin
        self.font_weight = font_weight
        self.bottom_padding = bottom_padding
        self.top_padding = top_padding
        self.text_align = text_align
        self.layout_settings = layout_settings if layout_settings else {}

        # Force comfortable line height if Spectra is enabled
        spectra_enabled = self.layout_settings.get("spectra_enabled", False)
        if spectra_enabled:
            self.line_height = max(line_height, 2.2)
        else:
            self.line_height = line_height

        # Orientation logic
        if orientation == "Landscape":
            self.screen_width = DEFAULT_SCREEN_HEIGHT
            self.screen_height = DEFAULT_SCREEN_WIDTH
        else:
            self.screen_width = DEFAULT_SCREEN_WIDTH
            self.screen_height = DEFAULT_SCREEN_HEIGHT

        # Reset docs and maps
        for entry in self.fitz_docs:
            entry[0].close()
        self.fitz_docs = []  # Stores (doc, has_image)
        self.page_map = []  # Maps global_index -> (doc_index, page_index)

        # NEW: Stores definitions specific to coordinates on a specific page
        # Key: (doc_index, page_index), Value: { "x_y": "Definition Text" }
        self.page_annotation_data = {}

        # Init Annotator (for retrieval)
        self.init_annotator(self.layout_settings)

        # CSS Generation
        if self.font_path:
            variants = get_font_variants(self.font_path)
            font_rules = []
            font_rules.append(
                f"""@font-face {{ font-family: "CustomFont"; src: url("{variants['regular']}"); font-weight: normal; font-style: normal; }}""")
            if variants['italic']: font_rules.append(
                f"""@font-face {{ font-family: "CustomFont"; src: url("{variants['italic']}"); font-weight: normal; font-style: italic; }}""")
            if variants['bold']: font_rules.append(
                f"""@font-face {{ font-family: "CustomFont"; src: url("{variants['bold']}"); font-weight: bold; font-style: normal; }}""")
            if variants['bold_italic']: font_rules.append(
                f"""@font-face {{ font-family: "CustomFont"; src: url("{variants['bold_italic']}"); font-weight: bold; font-style: italic; }}""")
            font_face_rule = "\n".join(font_rules)
            font_family_val = '"CustomFont"'
        else:
            font_face_rule = ""
            font_family_val = "serif"

        patched_css = fix_css_font_paths(self.book_css, font_family_val)

        # Calculate the actual space available for text
        content_height = max(1, self.screen_height - self.top_padding - self.bottom_padding)

        custom_css = f"""
                        <style>
                            {font_face_rule}
                            @page {{ size: {self.screen_width}pt {content_height}pt; margin: 0; }}
                    body {{
                        font-family: {font_family_val} !important;
                        font-size: {self.font_size}pt !important;
                        font-weight: {self.font_weight} !important;
                        line-height: {self.line_height} !important;
                        text-align: {self.text_align} !important;
                        color: black !important;
                        margin: 0 !important;
                        padding: {self.margin}px !important;
                        background-color: white !important;
                        width: 100% !important; 
                        height: 100% !important;
                        overflow-wrap: break-word;
                    }}
                    p, div, li, blockquote, dd, dt {{
                        font-family: inherit !important;
                        font-size: inherit !important;
                        font-weight: inherit !important;
                        line-height: inherit !important;
                        text-align: {self.text_align} !important;
                        color: inherit !important;
                    }}
                    span {{
                        font-family: {font_family_val} !important;
                        font-size: inherit !important;
                        line-height: inherit !important;
                        color: inherit !important;
                    }}
                    img {{ max-width: 95% !important; height: auto !important; display: block; margin: 20px auto !important; }}
                    h1, h2, h3 {{ text-align: center !important; margin-top: 1em; font-weight: {min(900, self.font_weight + 200)} !important; }}
                    .fn-marker {{ font-weight: bold; font-size: 0.7em !important; vertical-align: super; color: solid black !important; }}
                    .inline-footnote-box {{
                        display: block; margin: 15px 0px; padding: 0px 15px;
                        border-left: 4px solid solid black;
                        font-size: {int(self.font_size * 0.85)}pt !important;
                        line-height: {self.line_height} !important;
                    }}
                    .inline-footnote-box p {{ margin: 0 !important; padding: 0 !important; font-size: inherit !important; display: inline; }}
                </style>
                """

        temp_chapter_starts = []
        running_page_count = 0
        render_dir = tempfile.gettempdir()
        temp_html_path = os.path.join(render_dir, f"render_temp_{int(time.time())}.html")
        final_toc_titles = []
        total_chaps = len(self.raw_chapters)
        selected_set = set(selected_indices)

        # Regex for queue building (MUST match the analyzer logic)
        split_pattern = r'(?<!\bMr)(?<!\bMrs)(?<!\bDr)(?<!\bMs)(?<!\bSt)(?<!\bProf)(?<!\bCapt)(?<!\bGen)(?<!\bSen)(?<!\bRev)(?<=[.!?])\s+'
        word_pattern = re.compile(r'\b[a-zA-Z]{3,}\b')

        for idx, chapter in enumerate(self.raw_chapters):
            if progress_callback: progress_callback((idx / total_chaps) * 0.9)

            import copy
            soup = copy.copy(chapter['soup'])

            if show_footnotes:
                soup = self._inject_inline_footnotes(soup, chapter.get('filename', ''))

            # --- BUILD ANNOTATION QUEUES (FIFO) ---
            # structure: { "bank": ["Definition 1 (river)", "Definition 2 (finance)"] }
            annotation_queues = {}

            if spectra_enabled and idx in selected_set and self.annotator and self.annotator.master_cache:
                full_text = soup.get_text(" ", strip=True)
                sentences = re.split(split_pattern, full_text)

                for sentence in sentences:
                    sentence = sentence.strip()
                    if not sentence: continue

                    # 1. Recreate the unique context hash
                    ctx_hash = hashlib.md5(sentence.encode('utf-8')).hexdigest()[:6]

                    # 2. Find words and check if they exist in cache with this hash
                    matches = word_pattern.findall(sentence)
                    for w in matches:
                        w_lower = w.lower()
                        unique_key = f"{w_lower}|{ctx_hash}"

                        if unique_key in self.annotator.master_cache:
                            if w_lower not in annotation_queues:
                                annotation_queues[w_lower] = []
                            annotation_queues[w_lower].append(self.annotator.master_cache[unique_key])

            # Process Images
            for img_tag in soup.find_all('img'):
                src = os.path.basename(img_tag.get('src', ''))
                if src in self.book_images: img_tag['src'] = self.book_images[src]

            soup = hyphenate_html_text(soup, self.book_lang)

            if idx in selected_set:
                temp_chapter_starts.append(running_page_count)
                final_toc_titles.append(chapter['title'])

            body_content = "".join([str(x) for x in soup.body.contents]) if soup.body else str(soup)
            final_html = f"<html lang='{self.book_lang}'><head><style>{patched_css}</style>{custom_css}</head><body>{body_content}</body></html>"

            with open(temp_html_path, "w", encoding="utf-8") as f:
                f.write(final_html)

            # --- RENDER PDF ---
            doc = fitz.open(temp_html_path)
            rect = fitz.Rect(0, 0, self.screen_width, content_height)
            doc.layout(rect=rect)

            # Save doc reference
            self.fitz_docs.append((doc, chapter['has_image']))
            current_doc_idx = len(self.fitz_docs) - 1

            # --- ASSIGN DEFINITIONS TO COORDINATES ---
            # Iterate through generated pages and "deal out" definitions from our queues
            for page_i, page in enumerate(doc):
                # This dict will hold mappings for THIS SPECIFIC page
                page_defs = {}

                # Extract words to match against our queue
                words_on_page = page.get_text("words")

                for *coords, text, block_n, line_n, word_n in words_on_page:
                    # Clean the word (remove punctuation, lower case) to match queue keys
                    clean_text = text.strip('.,!?;:"()[]{}“”').lower()

                    if clean_text in annotation_queues:
                        queue = annotation_queues[clean_text]
                        if queue:
                            # FIFO: Pop the next definition available for this word
                            definition = queue.pop(0)

                            # Create a unique key based on coordinates
                            # We use int() to avoid float micro-differences
                            coord_key = f"{int(coords[0])}_{int(coords[1])}"
                            page_defs[coord_key] = definition

                # Store the result for render_page to use later
                if page_defs:
                    self.page_annotation_data[(current_doc_idx, page_i)] = page_defs

                # Map global page index
                self.page_map.append((current_doc_idx, page_i))

            running_page_count += len(doc)

        if os.path.exists(temp_html_path): os.remove(temp_html_path)

        # TOC Generation
        if add_toc and final_toc_titles:
            toc_main_size = self.font_size
            toc_header_size = int(self.font_size * 1.2)
            toc_row_height = int(self.font_size * self.line_height * 1.2)
            toc_header_space = 100 + self.top_padding
            reserved_top = toc_header_space
            reserved_bottom = self.bottom_padding
            available_h = self.screen_height - reserved_top - reserved_bottom
            self.toc_items_per_page = max(1, int(available_h // toc_row_height))
            num_toc_pages = (len(final_toc_titles) + self.toc_items_per_page - 1) // self.toc_items_per_page
            self.toc_data_final = [(t, temp_chapter_starts[i] + num_toc_pages + 1) for i, t in
                                   enumerate(final_toc_titles)]
            self.toc_pages_images = self._render_toc_pages(self.toc_data_final, toc_row_height, toc_main_size,
                                                           toc_header_size)
        else:
            self.toc_data_final = [(t, temp_chapter_starts[i] + 1) for i, t in enumerate(final_toc_titles)]
            self.toc_pages_images = []

        self.total_pages = len(self.toc_pages_images) + len(self.page_map)
        if progress_callback: progress_callback(1.0)
        self.is_ready = True
        return True

    def _get_ui_font(self, size):
        if self.font_path: return get_pil_font(self.font_path, size)
        try:
            return ImageFont.truetype("georgia.ttf", size)
        except:
            return ImageFont.load_default()

    def _render_toc_pages(self, toc_entries, row_height, font_size, header_size):
        pages = []

        def get_dynamic_toc_font(size):
            if self.font_path: return get_pil_font(self.font_path, size)
            try:
                return ImageFont.truetype("georgia.ttf", size)
            except:
                return ImageFont.load_default()

        font_main = get_dynamic_toc_font(font_size)
        font_header = get_dynamic_toc_font(header_size)
        left_margin = 40;
        right_margin = 40;
        column_gap = 20;
        limit = self.toc_items_per_page
        for i in range(0, len(toc_entries), limit):
            chunk = toc_entries[i: i + limit]
            img = Image.new('1', (self.screen_width, self.screen_height), 1)
            draw = ImageDraw.Draw(img)
            header_text = "TABLE OF CONTENTS"
            header_w = font_header.getlength(header_text)
            header_y = 40 + self.top_padding
            draw.text(((self.screen_width - header_w) // 2, header_y), header_text, font=font_header, fill=0)
            line_y = header_y + int(header_size * 1.5)
            draw.line((left_margin, line_y, self.screen_width - right_margin, line_y), fill=0)
            y = line_y + int(font_size * 1.2)
            for title, pg_num in chunk:
                pg_str = str(pg_num)
                pg_w = font_main.getlength(pg_str)
                max_title_w = self.screen_width - left_margin - right_margin - pg_w - column_gap
                display_title = title
                if font_main.getlength(display_title) > max_title_w:
                    while font_main.getlength(display_title + "...") > max_title_w and len(
                            display_title) > 0: display_title = display_title[:-1]
                    display_title += "..."
                draw.text((left_margin, y), display_title, font=font_main, fill=0)
                title_end_x = left_margin + font_main.getlength(display_title) + 5
                dots_end_x = self.screen_width - right_margin - pg_w - 10
                if dots_end_x > title_end_x:
                    dots_text = "." * int((dots_end_x - title_end_x) / font_main.getlength("."))
                    draw.text((title_end_x, y), dots_text, font=font_main, fill=0)
                draw.text((self.screen_width - right_margin - pg_w, y), pg_str, font=font_main, fill=0)
                y += row_height
            pages.append(img)
        return pages

    def _draw_progress_bar(self, draw, y, height, global_page_index):
        if self.total_pages <= 0: return
        show_ticks = self.layout_settings.get("bar_show_ticks", True)
        tick_h = self.layout_settings.get("bar_tick_height", 6)
        show_marker = self.layout_settings.get("bar_show_marker", True)
        marker_r = self.layout_settings.get("bar_marker_radius", 5)
        marker_col_str = self.layout_settings.get("bar_marker_color", "Black")
        marker_fill = (255, 255, 255) if marker_col_str == "White" else (0, 0, 0)
        draw.rectangle([10, y, self.screen_width - 10, y + height], fill=(255, 255, 255), outline=(0, 0, 0))
        if show_ticks:
            bar_center_y = y + (height / 2)
            t_top = bar_center_y - (tick_h / 2)
            t_bot = bar_center_y + (tick_h / 2)
            chapter_pages = [item[1] for item in self.toc_data_final]
            for cp in chapter_pages:
                mx = int(((cp - 1) / self.total_pages) * (self.screen_width - 20)) + 10
                draw.line([mx, t_top, mx, t_bot], fill=(0, 0, 0), width=1)
        curr_page_disp = global_page_index + 1
        bar_width_px = self.screen_width - 20
        fill_width = int((curr_page_disp / self.total_pages) * bar_width_px)
        draw.rectangle([10, y, 10 + fill_width, y + height], fill=(0, 0, 0))
        if show_marker:
            cx = 10 + fill_width
            cy = y + (height / 2)
            draw.ellipse([cx - marker_r, cy - marker_r, cx + marker_r, cy + marker_r], fill=marker_fill,
                         outline=(0, 0, 0))

    def _get_page_text_elements(self, global_page_index):
        page_num_disp = global_page_index + 1
        percent = int((page_num_disp / self.total_pages) * 100)
        current_title = ""
        num_toc = len(self.toc_pages_images)
        if global_page_index < num_toc:
            current_title = "Table of Contents"
            chap_page_disp = f"{global_page_index + 1}/{num_toc}"
        else:
            for title, start_pg in reversed(self.toc_data_final):
                if page_num_disp >= start_pg:
                    current_title = title
                    break
            pm_idx = global_page_index - num_toc
            if 0 <= pm_idx < len(self.page_map):
                doc_idx, page_idx = self.page_map[pm_idx]
                doc_ref = self.fitz_docs[doc_idx][0]
                chap_total = len(doc_ref)
                chap_page_disp = f"{page_idx + 1}/{chap_total}"
            else:
                chap_page_disp = "1/1"
        return {'pagenum': f"{page_num_disp}/{self.total_pages}", 'title': current_title, 'chap_page': chap_page_disp,
                'percent': f"{percent}%"}

    def _draw_text_line(self, draw, y, font, elements_list, align):
        if not elements_list: return
        separator = "   |   "
        margin_x = 15
        if align == "Justify" and len(elements_list) > 1:
            left_item = elements_list[0]
            left_text = left_item[1]
            draw.text((margin_x, y), left_text, font=font, fill=(0, 0, 0))
            right_item = elements_list[-1]
            right_text = right_item[1]
            right_w = font.getlength(right_text)
            draw.text((self.screen_width - margin_x - right_w, y), right_text, font=font, fill=(0, 0, 0))
            if len(elements_list) > 2:
                mid_items = elements_list[1:-1]
                mid_text_parts = []
                title_in_middle = False
                title_original = ""
                for key, txt in mid_items:
                    if key == 'title':
                        title_in_middle = True
                        title_original = txt
                    mid_text_parts.append(txt)
                left_boundary = margin_x + font.getlength(left_text) + 20
                right_boundary = self.screen_width - margin_x - right_w - 20
                max_mid_w = right_boundary - left_boundary
                if max_mid_w < 50: return
                final_mid_text = separator.join(mid_text_parts)
                if title_in_middle and font.getlength(final_mid_text) > max_mid_w:
                    non_title_w = sum([font.getlength(p) for k, p in mid_items if k != 'title'])
                    sep_w_total = font.getlength(separator) * (len(mid_items) - 1) if len(mid_items) > 1 else 0
                    available_for_title = max_mid_w - non_title_w - sep_w_total
                    if available_for_title > 20:
                        trunc_title = title_original
                        while font.getlength(trunc_title + "...") > available_for_title and len(
                                trunc_title) > 0: trunc_title = trunc_title[:-1]
                        trunc_title += "..."
                        new_parts = []
                        for key, txt in mid_items:
                            if key == 'title':
                                new_parts.append(trunc_title)
                            else:
                                new_parts.append(txt)
                        final_mid_text = separator.join(new_parts)
                    else:
                        final_mid_text = separator.join([p for k, p in mid_items if k != 'title'])
                mid_w = font.getlength(final_mid_text)
                mid_x = (self.screen_width - mid_w) // 2
                if mid_x < left_boundary: mid_x = left_boundary
                draw.text((mid_x, y), final_mid_text, font=font, fill=(0, 0, 0))
        else:
            other_text = ""
            title_text = ""
            has_title = False
            non_title_parts = []
            for key, txt in elements_list:
                if key == 'title':
                    title_text = txt
                    has_title = True
                else:
                    non_title_parts.append(txt)
            sep_w = font.getlength(separator) * (len(elements_list) - 1) if len(elements_list) > 1 else 0
            others_w = sum([font.getlength(p) for p in non_title_parts])
            max_screen = self.screen_width - 30
            available_for_title = max_screen - others_w - sep_w
            if has_title:
                if font.getlength(title_text) > available_for_title:
                    while font.getlength(title_text + "...") > available_for_title and len(
                            title_text) > 0: title_text = title_text[:-1]
                    title_text += "..."
            final_parts = []
            for key, txt in elements_list:
                if key == 'title':
                    final_parts.append(title_text)
                else:
                    final_parts.append(txt)
            full_str = separator.join(final_parts)
            str_w = font.getlength(full_str)
            x_pos = 15
            if align == "Center":
                x_pos = (self.screen_width - str_w) // 2
            elif align == "Right":
                x_pos = self.screen_width - 15 - str_w
            draw.text((x_pos, y), full_str, font=font, fill=(0, 0, 0))

    def _get_active_elements(self, bar_role, text_data):
        s = self.layout_settings
        active = []
        for key in ['title', 'pagenum', 'chap_page', 'percent']:
            pos_val = s.get(f"pos_{key}", "Hidden")
            if pos_val == bar_role:
                order = int(s.get(f"order_{key}", 99))
                content = text_data.get(key, "")
                if content: active.append((order, key, content))
        active.sort(key=lambda x: x[0])
        return [(x[1], x[2]) for x in active]

    def _draw_header(self, draw, global_page_index):
        settings = self.layout_settings
        font_size = settings.get("header_font_size", 16)
        margin = settings.get("header_margin", 0)
        align = settings.get("header_align", "Center")
        bar_height = settings.get("bar_height", 4)
        pos_prog = settings.get("pos_progress", "Footer (Below Text)")
        text_data = self._get_page_text_elements(global_page_index)
        elements = self._get_active_elements("Header", text_data)
        has_text = len(elements) > 0
        has_bar = False
        bar_above_text = True
        if pos_prog == "Header (Above Text)":
            has_bar = True;
            bar_above_text = True
        elif pos_prog == "Header (Below Text)":
            has_bar = True;
            bar_above_text = False
        gap = 6
        font_ui = self._get_ui_font(font_size)
        current_y = margin
        if bar_above_text and has_bar:
            self._draw_progress_bar(draw, current_y, bar_height, global_page_index)
            current_y += bar_height + gap
        if has_text:
            self._draw_text_line(draw, current_y, font_ui, elements, align)
            current_y += font_size + gap
        if not bar_above_text and has_bar: self._draw_progress_bar(draw, current_y, bar_height, global_page_index)

    def _draw_footer(self, draw, global_page_index):
        settings = self.layout_settings
        font_size = settings.get("footer_font_size", 16)
        margin = settings.get("footer_margin", 0)
        align = settings.get("footer_align", "Center")
        bar_height = settings.get("bar_height", 4)
        pos_prog = settings.get("pos_progress", "Footer (Below Text)")
        text_data = self._get_page_text_elements(global_page_index)
        elements = self._get_active_elements("Footer", text_data)
        has_text = len(elements) > 0
        has_bar = False
        bar_above_text = True
        if pos_prog == "Footer (Above Text)":
            has_bar = True;
            bar_above_text = True
        elif pos_prog == "Footer (Below Text)":
            has_bar = True;
            bar_above_text = False
        gap = 6
        font_ui = self._get_ui_font(font_size)
        anchor_y = self.screen_height - margin
        text_h = font_size if has_text else 0
        bar_h = bar_height if has_bar else 0
        text_y = 0
        bar_y = 0
        if has_bar and has_text:
            if bar_above_text:
                text_y = anchor_y - text_h
                bar_y = text_y - gap - bar_h
            else:
                bar_y = anchor_y - bar_h
                text_y = bar_y - gap - text_h
        elif has_text:
            text_y = anchor_y - text_h
        elif has_bar:
            bar_y = anchor_y - bar_h
        if has_bar: self._draw_progress_bar(draw, bar_y, bar_height, global_page_index)
        if has_text: self._draw_text_line(draw, text_y, font_ui, elements, align)

        # --- INSIDE CLASS EpubProcessor ---

    def render_page(self, global_page_index):
        if not self.is_ready: return None

        # 1. Get current settings
        contrast = self.layout_settings.get("contrast", 1.2)
        white_clip = self.layout_settings.get("white_clip", 220)
        spectra_enabled = self.layout_settings.get("spectra_enabled", False)

        num_toc = len(self.toc_pages_images)
        footer_padding = max(0, self.bottom_padding)
        header_padding = max(0, self.top_padding)
        content_height = max(1, self.screen_height - footer_padding - header_padding)

        # 2. Prepare content layer
        if global_page_index < num_toc:
            img_content = self.toc_pages_images[global_page_index].copy().convert("L")
            is_toc, page = True, None
            # Dummy scaling vars for TOC (though not used)
            sx, sy = 1.0, 1.0
        else:
            is_toc = False
            doc_idx, page_idx = self.page_map[global_page_index - num_toc]
            doc, _ = self.fitz_docs[doc_idx]
            page = doc[page_idx]

            # --- CALCULATE SCALING FACTORS (Crucial for your snippet) ---
            sx = self.screen_width / page.rect.width
            sy = content_height / page.rect.height

            # Render PDF page to Grayscale
            mat = fitz.Matrix(sx, sy)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img_content = Image.frombytes("RGB", [pix.width, pix.height], pix.samples).convert("L")

        # 3. Assemble full page
        full_page = Image.new("L", (self.screen_width, self.screen_height), 255)

        # Define Paste Offsets
        paste_x = (self.screen_width - img_content.width) // 2
        paste_y = 0 if is_toc else header_padding

        full_page.paste(img_content, (paste_x, paste_y))

        # 4. Enhance Grayscale
        # 4. Enhance Grayscale & SIMULATE PREVIEW
        if not is_toc:
            if contrast != 1.0:
                full_page = ImageEnhance.Contrast(full_page).enhance(contrast)
            if white_clip < 255:
                full_page = full_page.point(lambda p: 255 if p > white_clip else p)

            # --- RESTORED STEP B: PREVIEW SIMULATION ---
            render_mode = self.layout_settings.get("render_mode", "Threshold")
            bit_depth = self.layout_settings.get("bit_depth", "1-bit (XTG)")

            # 1. Apply Sharpness (Definition slider) to BOTH 1-bit and 2-bit Threshold modes
            text_blur = self.layout_settings.get("text_blur", 1.0)
            if render_mode == "Threshold" and text_blur > 0:
                full_page = ImageEnhance.Sharpness(full_page).enhance(1.0 + (text_blur * 0.5))

            if "2-bit" in bit_depth:
                if render_mode == "Dither":
                    # FIX 1: Prevent white-on-black inversion by forcing an exact grayscale palette
                    pal = Image.new('P', (1, 1))
                    pal.putpalette([0, 0, 0, 85, 85, 85, 170, 170, 170, 255, 255, 255] + [0] * 756)
                    # Convert to RGB first, quantize, then back to L to guarantee correct colors
                    full_page = full_page.convert("RGB").quantize(palette=pal,
                                                                  dither=Image.Dither.FLOYDSTEINBERG).convert("L")
                else:
                    # FIX 2: Make the Threshold slider work in 2-bit mode
                    thresh = self.layout_settings.get("text_threshold", 130)

                    # Shift pixel brightness based on slider (128 is neutral).
                    # High threshold = darker image (thicker text). Low threshold = brighter image.
                    brightness_shift = 128 - thresh
                    full_page = full_page.point(lambda p: max(0, min(255, p + brightness_shift)))

                    # Snap to 4 distinct hardware shades (0, 85, 170, 255)
                    full_page = full_page.point(lambda p: (p // 64) * 85).convert("L")
            else:
                if render_mode == "Dither":
                    full_page = full_page.convert("1", dither=Image.Dither.FLOYDSTEINBERG).convert("L")
                else:
                    thresh = self.layout_settings.get("text_threshold", 130)
                    full_page = full_page.point(lambda p: 255 if p > thresh else 0).convert("L")

        # 5. UI Overlay (Header/Footer/Spectra)
        img_final = full_page.convert("RGB")
        draw = ImageDraw.Draw(img_final)

        if not is_toc:
            # Clear margin areas
            if header_padding > 0: draw.rectangle([0, 0, self.screen_width, header_padding], fill=(255, 255, 255))
            if footer_padding > 0: draw.rectangle(
                [0, self.screen_height - footer_padding, self.screen_width, self.screen_height], fill=(255, 255, 255))

            self._draw_header(draw, global_page_index)
            self._draw_footer(draw, global_page_index)

            # --- YOUR IMPROVED SPECTRA RENDERING LOGIC ---
            page_annotations = self.page_annotation_data.get((doc_idx, page_idx), {})

            if page_annotations and spectra_enabled and page:
                annot_font_size = max(9, int(self.font_size * 0.65))
                annot_font = self._get_ui_font(annot_font_size)

                drawn_items = []

                # Get all words on page to match coordinates
                page_words = page.get_text("words")

                for x0, y0, x1, y1, text, block, line, word_idx in page_words:
                    # 1. Clean punctuation
                    clean_text = text.strip('.,!?;:"()[]{}“”')

                    # --- RESTORED PROTECTION ---
                    # Note: This will hide annotations for words at the start of sentences
                    # (e.g., "Suddenly" -> "Fast") because "Suddenly" is not lower case.
                    if not clean_text.islower():
                        continue
                    # ---------------------------

                    coord_key = f"{int(x0)}_{int(y0)}"

                    if coord_key not in page_annotations:
                        continue

                    defi = page_annotations[coord_key]

                    # We check for both character length and word count to be safe - protection against hallucination.

                    if not defi or len(defi) > 35 or len(defi.split()) > 5:
                        continue

                    # --- RENDER LOGIC ---
                    is_duplicate = False

                    # Convert PDF coordinates to Image Pixel coordinates
                    px_x = x0 * sx
                    px_y = y0 * sy
                    px_w = (x1 - x0) * sx

                    # Check vertical collision (prevent overlapping definitions)
                    for prev_y, prev_def in drawn_items:
                        if prev_def == defi:
                            # If same definition is closer than 2.5 lines, skip it
                            if abs(px_y - prev_y) < (self.font_size * 1.5):
                                is_duplicate = True
                                break

                    if is_duplicate: continue

                    # Calculate Draw Position
                    text_len = annot_font.getlength(defi)

                    # Center text over the word
                    draw_x = paste_x + px_x + (px_w - text_len) / 2

                    # Screen Boundary Checks (Left/Right)
                    if draw_x < 2: draw_x = 2
                    if draw_x + text_len > self.screen_width - 2:
                        draw_x = self.screen_width - text_len - 2

                    # Vertical Position (Above the word)
                    draw_y = paste_y + px_y - annot_font_size + 2

                    # Header Boundary Check
                    if draw_y < header_padding: draw_y = header_padding

                    # Draw
                    draw.text((draw_x, draw_y), defi, font=annot_font, fill=(0, 0, 0))
                    drawn_items.append((px_y, defi))

        return img_final

    def _pack_metadata(self, title, author, lang, chapter_count):
        """Packs metadata into a 256-byte fixed-size block."""
        # Fields: Title(128), Author(64), Publisher(32), Language(16),
        # CreateTime(4), CoverPage(2), ChapterCount(2), Reserved(8)
        blob = bytearray(256)
        struct.pack_into("<128s", blob, 0x00, title.encode('utf-8')[:127])
        struct.pack_into("<64s", blob, 0x80, author.encode('utf-8')[:63])
        struct.pack_into("<32s", blob, 0xC0, b"EPUB2XTC")  # Publisher
        struct.pack_into("<16s", blob, 0xE0, lang.encode('utf-8')[:15])
        struct.pack_into("<I", blob, 0xF0, int(time.time()))
        struct.pack_into("<H", blob, 0xF4, 0)  # Cover Page (usually 0)
        struct.pack_into("<H", blob, 0xF6, chapter_count)
        return bytes(blob)

    def _pack_chapter(self, name, start_pg, end_pg):
        """Packs a single chapter into a 96-byte fixed-size block."""
        # Fields: Name(80), Start(2), End(2), Reserved1-3(4+4+4)
        blob = bytearray(96)
        struct.pack_into("<80s", blob, 0x00, name.encode('utf-8')[:79])
        struct.pack_into("<H", blob, 0x50, start_pg)
        struct.pack_into("<H", blob, 0x52, end_pg)
        return bytes(blob)

    def save_xtc(self, out_name, progress_callback=None):
        if not self.is_ready: return

        # 1. SETUP PARAMETERS & DEPTH
        bit_depth = self.layout_settings.get("bit_depth", "1-bit (XTG)")
        render_mode = self.layout_settings.get("render_mode", "Threshold")
        is_2bit = "2-bit" in bit_depth
        file_id = 0x00485458 if is_2bit else 0x00475458

        # Determine how many TOC pages exist (to sync chapter start pages)
        num_toc_pages = len(self.toc_pages_images)

        # 2. CALCULATE FILE OFFSETS
        metadata_off = 56
        chapter_off = metadata_off + 256
        num_chaps = len(self.toc_data_final)
        index_off = chapter_off + (num_chaps * 96)
        data_off = index_off + (self.total_pages * 16)

        # 3. PACK METADATA
        book_title = getattr(self, 'title_metadata', "Unknown Title")
        book_author = getattr(self, 'author_metadata', "Unknown Author")
        metadata_block = self._pack_metadata(book_title, book_author, self.book_lang, num_chaps)

        # 4. PACK CHAPTERS (Synchronized with Visual TOC)
        chapter_block = bytearray()
        for i, (title, start_pg_from_renderer) in enumerate(self.toc_data_final):
            # start_pg_from_renderer is already 1-based and includes TOC offset
            # XTC internal structure expects 0-based index
            start_idx = start_pg_from_renderer - 1

            # Calculate end index (inclusive)
            if i + 1 < num_chaps:
                next_chap_start = self.toc_data_final[i + 1][1]
                end_idx = next_chap_start - 2  # Page before next chapter starts
            else:
                end_idx = self.total_pages - 1

            chapter_block.extend(self._pack_chapter(title, start_idx, end_idx))

        # 5. PROCESS ALL PAGES (TOC + CONTENT)
        blob_data, index_table = bytearray(), bytearray()

        for i in range(self.total_pages):
            if progress_callback: progress_callback((i + 1) / self.total_pages)

            img_rgb = self.render_page(i)
            img_gray = img_rgb.convert("L")
            w, h = img_gray.size

            if is_2bit:
                # --- XTH 2-BIT VERTICAL SCAN ORDER ---
                # Quantize/Posterize to 4 levels
                if render_mode == "Dither":
                    pal = Image.new('P', (1, 1))
                    # Palette: White, Light Gray, Dark Gray, Black
                    pal.putpalette([255, 255, 255, 170, 170, 170, 85, 85, 85, 0, 0, 0] + [0] * 756)
                    quant = img_gray.quantize(palette=pal, dither=Image.DITHER.FLOYDSTEINBERG)
                else:
                    # 255 (White) // 64 = 3 -> (3-3) = 0 (White)
                    # 0 (Black) // 64 = 0 -> (3-0) = 3 (Black)
                    quant = img_gray.point(lambda p: (3 - (p // 64)))

                pix = quant.load()
                bytes_per_col = (h + 7) // 8
                p1, p2 = bytearray(bytes_per_col * w), bytearray(bytes_per_col * w)

                # Scan: Right to Left -> Top to Bottom
                for x_idx, x in enumerate(range(w - 1, -1, -1)):
                    for y in range(h):
                        val = pix[x, y] & 0x03
                        bit1 = (val >> 1) & 0x01
                        bit2 = val & 0x01

                        target_byte = (x_idx * bytes_per_col) + (y // 8)
                        bit_pos = 7 - (y % 8)  # MSB is top

                        if bit1: p1[target_byte] |= (1 << bit_pos)
                        if bit2: p2[target_byte] |= (1 << bit_pos)

                bitmap_data = bytes(p1) + bytes(p2)
                data_size = len(bitmap_data)
            else:
                # --- XTG 1-BIT ROW-MAJOR ---
                if render_mode == "Dither":
                    img_final = img_gray.convert("1", dither=Image.Dither.FLOYDSTEINBERG)
                else:
                    thresh = self.layout_settings.get("text_threshold", 130)
                    img_final = img_gray.point(lambda p: 255 if p > thresh else 0).convert("1")

                bitmap_data = img_final.tobytes()
                data_size = ((w + 7) // 8) * h

            # Page Blob = 22-byte XTG/XTH Header + Bitmap
            xt_header = struct.pack("<IHHBBIQ", file_id, w, h, 0, 0, data_size, 0)
            page_blob = xt_header + bitmap_data

            # Index entry (absolute offset from file start)
            index_table.extend(struct.pack("<QIHH", data_off + len(blob_data), len(page_blob), w, h))
            blob_data.extend(page_blob)

        # 6. FINAL CONTAINER HEADER
        header = struct.pack("<IHHBBBBIQQQQQ",
                             0x00435458,  # Identifier
                             0x0100,  # Version (Must be here at Offset 0x04)
                             self.total_pages,  # Page Count (Must be here at Offset 0x06)
                             0, 1, 0, 1, 1,  # Flags
                             metadata_off, index_off, data_off, 0, chapter_off
                             )
        # Note: The format of struct.pack above must match the 56-byte definition exactly.
        # Ensure the number of 'Q' (uint64) and 'I' (uint32) matches your spec.

        with open(out_name, "wb") as f:
            f.write(header)
            f.write(metadata_block)
            f.write(chapter_block)
            f.write(index_table)
            f.write(blob_data)

# --- UI COLORS ---
COLOR_BG = "#111111"
COLOR_TOOLBAR = "#1a1a1a"
COLOR_CARD = "#2B2B2B"
COLOR_ACCENT = "#3498DB"
COLOR_SUCCESS = "#2ECC71"
COLOR_WARNING = "#E67E22"
COLOR_DANGER = "#C0392B"
COLOR_TEXT_GRAY = "#AAAAAA"


class SettingsCard(ctk.CTkFrame):
    def __init__(self, parent, title, expanded=False):
        super().__init__(parent, fg_color=COLOR_CARD, corner_radius=10, border_width=1, border_color="#333")
        self.pack(fill="x", pady=5, padx=10)

        self.title_text = title
        self.is_expanded = expanded

        # 1. HEADER: Use a Button instead of a Label to make it clickable
        # We use transparent fg_color so it looks like a header but reacts to hover
        self.btn_header = ctk.CTkButton(
            self,
            text=f"{'▼' if expanded else '▶'} {title}",
            command=self.toggle,
            fg_color="transparent",
            hover_color="#404040",
            anchor="w",
            font=("Arial", 13, "bold"),
            text_color="#FFF",
            height=35
        )
        self.btn_header.pack(fill="x", padx=2, pady=2)

        # 2. CONTENT FRAME: Created immediately so widgets can be added to it,
        # but only packed (shown) if expanded is True.
        self.content = ctk.CTkFrame(self, fg_color="transparent")

        if self.is_expanded:
            self.content.pack(fill="x", padx=10, pady=(0, 10))

    def toggle(self):
        """Show/Hide the content frame"""
        self.is_expanded = not self.is_expanded

        if self.is_expanded:
            self.btn_header.configure(text=f"▼ {self.title_text}")
            self.content.pack(fill="x", padx=10, pady=(0, 10))
        else:
            self.btn_header.configure(text=f"▶ {self.title_text}")
            self.content.pack_forget()


class ModernApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        # Load AoA on startup
        load_aoa_database()

        self.processor = EpubProcessor()
        self.current_page_index = 0
        self.debounce_timer = None
        self.is_processing = False
        self.pending_rerun = False
        self.selected_chapter_indices = []
        self.title("EPUB2XTC Converter")
        self.geometry("1400x950")
        self.configure(fg_color=COLOR_BG)
        if not os.path.exists(PRESETS_DIR): os.makedirs(PRESETS_DIR)
        self.startup_settings = FACTORY_DEFAULTS.copy()
        self.load_startup_defaults()
        self._build_toolbar()
        self.main_container = ctk.CTkFrame(self, fg_color=COLOR_BG)
        self.main_container.pack(fill="both", expand=True)
        self._build_sidebar()
        self._build_preview_area()
        self.refresh_presets_list()
        self.apply_settings_dict(self.startup_settings)

    def _build_toolbar(self):
        tb = ctk.CTkFrame(self, height=70, fg_color=COLOR_TOOLBAR, corner_radius=0)
        tb.pack(fill="x", side="top")
        logo_f = ctk.CTkFrame(tb, fg_color="transparent")
        logo_f.pack(side="left", padx=(20, 30))
        ctk.CTkLabel(logo_f, text="EPUB2XTC", font=("Arial", 20, "bold"), text_color=COLOR_ACCENT).pack(anchor="w")
        self.lbl_file = ctk.CTkLabel(logo_f, text="No file selected", font=("Arial", 12), text_color="gray", anchor="w")
        self.lbl_file.pack(anchor="w")
        self._create_icon_btn(tb, "＋ Import EPUB", COLOR_SUCCESS, self.select_file)
        self._create_divider(tb)
        self.btn_chapters = self._create_icon_btn(tb, "☰ Edit TOC", COLOR_WARNING, self.open_chapter_dialog, "disabled")
        self.btn_export = self._create_icon_btn(tb, "⚡ Save .XTC", COLOR_ACCENT, self.export_file, "disabled")
        self.btn_cover = self._create_icon_btn(tb, "🖼 Export Cover", "#8E44AD", self.open_cover_export, "disabled")
        self._create_divider(tb)
        self._create_icon_btn(tb, "⟲ Reset Settings", "#555", self.reset_to_factory)
        right_f = ctk.CTkFrame(tb, fg_color="transparent")
        right_f.pack(side="right", padx=20, fill="y")
        self.progress_label = ctk.CTkLabel(right_f, text="Ready", font=("Arial", 12), text_color="gray", anchor="e")
        self.progress_label.pack(side="top", pady=(15, 0), anchor="e")
        self.progress_bar = ctk.CTkProgressBar(right_f, width=200, height=8, progress_color=COLOR_ACCENT)
        self.progress_bar.set(0)
        self.progress_bar.pack(side="bottom", pady=(0, 15))

    def refresh_all_slider_labels(self):
        """Forces all labels to match the current slider values (used after loading presets)."""
        # List of all your slider attributes
        sliders = [
            ("lbl_size", "slider_font_size"),
            ("lbl_weight", "slider_font_weight"),
            ("lbl_line", "slider_line_height"),
            ("lbl_margin", "slider_margin"),
            ("lbl_top_padding", "slider_top_padding"),
            ("lbl_padding", "slider_bottom_padding"),
            ("lbl_spectra_thresh", "slider_spectra_threshold"),
            ("lbl_spectra_aoa", "slider_spectra_aoa_threshold"),
            ("lbl_white_clip", "slider_white_clip"),
            ("lbl_contrast", "slider_contrast"),
            ("lbl_threshold", "slider_text_threshold"),
            ("lbl_blur", "slider_text_blur"),
            ("lbl_header_size", "slider_header_font_size"),
            ("lbl_header_margin", "slider_header_margin"),
            ("lbl_footer_size", "slider_footer_font_size"),
            ("lbl_footer_margin", "slider_footer_margin"),
            ("lbl_preview_zoom", "slider_preview_zoom"),
            ("lbl_marker_size", "slider_bar_marker_radius"),
            ("lbl_tick_height", "slider_bar_tick_height"),
            ("lbl_bar_thick", "slider_bar_height"),
        ]

        for lbl_attr, sld_attr in sliders:
            if hasattr(self, lbl_attr) and hasattr(self, sld_attr):
                label_widget = getattr(self, lbl_attr)
                slider_widget = getattr(self, sld_attr)
                formatter = getattr(self, f"fmt_{sld_attr}")
                label_widget.configure(text=formatter(slider_widget.get()))

    def _build_sidebar(self):
        self.sidebar = ctk.CTkScrollableFrame(self.main_container, width=400, fg_color="transparent")
        self.sidebar.pack(side="left", fill="y", padx=10, pady=10)

        # --- PRESETS ---
        c_pre = SettingsCard(self.sidebar, "PRESETS")
        row_pre = ctk.CTkFrame(c_pre.content, fg_color="transparent")
        row_pre.pack(fill="x")
        self.preset_var = ctk.StringVar(value="Select Preset...")
        self.preset_dropdown = ctk.CTkOptionMenu(row_pre, variable=self.preset_var, values=[],
                                                 command=self.load_selected_preset, fg_color="#444",
                                                 button_color="#555", height=22, font=("Arial", 12))
        self.preset_dropdown.pack(side="left", fill="x", expand=True, padx=(0, 5))
        ctk.CTkButton(row_pre, text="💾", width=30, height=22, command=self.save_new_preset,
                      fg_color=COLOR_SUCCESS).pack(side="left")
        ctk.CTkButton(c_pre.content, text="Set Current as Default", command=self.save_current_as_default,
                      fg_color="#444", hover_color=COLOR_ACCENT, height=24).pack(fill="x", pady=(5, 0))

        # --- RENDER ENGINE ---
        c_ren = SettingsCard(self.sidebar, "RENDER ENGINE")

        # 1. Selection: Bit Depth / Format
        r_depth = ctk.CTkFrame(c_ren.content, fg_color="transparent")
        r_depth.pack(fill="x", pady=2)
        ctk.CTkLabel(r_depth, text="Target Format:", font=("Arial", 12), width=130, anchor="w").pack(side="left")
        self.bit_depth_var = ctk.StringVar(value=self.startup_settings.get("bit_depth", "1-bit (XTG)"))
        ctk.CTkOptionMenu(r_depth, values=["1-bit (XTG)", "2-bit (XTH)"], variable=self.bit_depth_var,
                          command=self.schedule_update, height=22).pack(side="right", fill="x", expand=True)

        # 2. Selection: Conversion Algorithm
        r_mode = ctk.CTkFrame(c_ren.content, fg_color="transparent")
        r_mode.pack(fill="x", pady=2)
        ctk.CTkLabel(r_mode, text="Conversion:", font=("Arial", 12), width=130, anchor="w").pack(side="left")
        self.render_mode_var = ctk.StringVar(value=self.startup_settings.get("render_mode", "Threshold"))
        ctk.CTkOptionMenu(r_mode, values=["Threshold", "Dither"], variable=self.render_mode_var,
                          command=self.toggle_render_controls, height=22).pack(side="right", fill="x", expand=True)

        self.frm_render_dynamic = ctk.CTkFrame(c_ren.content, fg_color="transparent")
        self.frm_render_dynamic.pack(fill="x", pady=5)

        # 3. Sliders: Dither Path
        self.frm_dither = ctk.CTkFrame(self.frm_render_dynamic, fg_color="transparent")
        self.sld_white = self._create_slider(self.frm_dither, "lbl_white_clip", "White Clip", "slider_white_clip", 150,
                                             255)
        self.sld_contrast = self._create_slider(self.frm_dither, "lbl_contrast", "Contrast", "slider_contrast", 0.5,
                                                2.0, is_float=True)

        # 4. Sliders: Threshold Path
        self.frm_thresh = ctk.CTkFrame(self.frm_render_dynamic, fg_color="transparent")
        self.sld_thresh = self._create_slider(self.frm_thresh, "lbl_threshold", "Threshold", "slider_text_threshold",
                                              50, 200)
        self.sld_blur = self._create_slider(self.frm_thresh, "lbl_blur", "Definition", "slider_text_blur", 0.0, 3.0,
                                            is_float=True)

        self.toggle_render_controls()

        # --- TYPOGRAPHY ---
        c_type = SettingsCard(self.sidebar, "TYPOGRAPHY")
        r_font = ctk.CTkFrame(c_type.content, fg_color="transparent")
        r_font.pack(fill="x", pady=2)
        ctk.CTkLabel(r_font, text="Font Family:", font=("Arial", 12), width=130, anchor="w").pack(side="left")
        self.available_fonts = get_local_fonts()
        self.font_options = ["Default (System)"] + sorted(list(self.available_fonts.keys()))
        self.font_map = self.available_fonts.copy()
        self.font_map["Default (System)"] = "DEFAULT"
        self.font_dropdown = ctk.CTkOptionMenu(r_font, values=self.font_options, command=self.on_font_change, height=22,
                                               font=("Arial", 12))
        self.font_dropdown.set(self.startup_settings.get("font_name", "Default (System)"))
        self.font_dropdown.pack(side="right", fill="x", expand=True)
        r_align = ctk.CTkFrame(c_type.content, fg_color="transparent")
        r_align.pack(fill="x", pady=2)
        ctk.CTkLabel(r_align, text="Alignment:", font=("Arial", 12), width=130, anchor="w").pack(side="left")
        self.align_dropdown = ctk.CTkOptionMenu(r_align, values=["justify", "left"], command=self.schedule_update,
                                                height=22, font=("Arial", 12))
        self.align_dropdown.set(self.startup_settings["text_align"])
        self.align_dropdown.pack(side="right", fill="x", expand=True)
        self._create_slider(c_type.content, "lbl_size", "Font Size", "slider_font_size", 12, 48)
        self._create_slider(c_type.content, "lbl_weight", "Font Weight", "slider_font_weight", 100, 900)
        self._create_slider(c_type.content, "lbl_line", "Line Height", "slider_line_height", 1.0, 2.5, is_float=True)

        # --- LAYOUT ---
        c_lay = SettingsCard(self.sidebar, "PAGE LAYOUT")
        r_ori = ctk.CTkFrame(c_lay.content, fg_color="transparent")
        r_ori.pack(fill="x", pady=2)
        ctk.CTkLabel(r_ori, text="Orientation:", font=("Arial", 12), width=130, anchor="w").pack(side="left")
        self.orientation_var = ctk.StringVar(value=self.startup_settings["orientation"])
        ctk.CTkOptionMenu(r_ori, values=["Portrait", "Landscape"], variable=self.orientation_var,
                          command=self.schedule_update, height=22, font=("Arial", 12)).pack(side="right", fill="x",
                                                                                            expand=True)
        r_tog = ctk.CTkFrame(c_lay.content, fg_color="transparent")
        r_tog.pack(fill="x", pady=5)
        self.var_toc = ctk.BooleanVar(value=self.startup_settings["generate_toc"])
        ctk.CTkCheckBox(r_tog, text="Generate TOC", variable=self.var_toc, command=self.schedule_update,
                        font=("Arial", 12)).pack(side="left")
        self.var_footnotes = ctk.BooleanVar(value=self.startup_settings.get("show_footnotes", True))
        ctk.CTkCheckBox(r_tog, text="Inline Footnotes", variable=self.var_footnotes, command=self.schedule_update,
                        font=("Arial", 12)).pack(side="right")
        self._create_slider(c_lay.content, "lbl_margin", "Side Margin", "slider_margin", 0, 100)
        self._create_slider(c_lay.content, "lbl_top_padding", "Top Padding", "slider_top_padding", 0, 150)
        self._create_slider(c_lay.content, "lbl_padding", "Bottom Padding", "slider_bottom_padding", 0, 150)


        # --- HEADER & FOOTER ---
        c_hf = SettingsCard(self.sidebar, "HEADER & FOOTER")
        grid_hf = ctk.CTkFrame(c_hf.content, fg_color="transparent")
        grid_hf.pack(fill="x")

        # --- SPECTRA AI ANNOTATIONS (ISSUE 4 FIX: SEPARATE GEN FROM VIEW) ---
        c_spectra = SettingsCard(self.sidebar, "SPECTRA AI ANNOTATIONS")

        if not HAS_WORDFREQ or not HAS_OPENAI:
            ctk.CTkLabel(c_spectra.content, text="Missing libraries:\npip install wordfreq openai",
                         text_color=COLOR_DANGER).pack(pady=5)
            self.var_spectra_enabled = ctk.BooleanVar(value=False)
        else:

            self.var_spectra_enabled = ctk.BooleanVar(value=self.startup_settings.get("spectra_enabled", False))
            ctk.CTkCheckBox(c_spectra.content, text="Show Definitions Overlay", variable=self.var_spectra_enabled,
                            command=self.schedule_update).pack(anchor="w", pady=5)

            # --- NEW LANGUAGE DROPDOWN ---
            ctk.CTkLabel(c_spectra.content, text="Target Language:", font=("Arial", 12), anchor="w").pack(anchor="w")
            self.var_spectra_lang = ctk.StringVar(value=self.startup_settings.get("spectra_target_lang", "English"))
            languages = ["English", "Spanish", "French", "German", "Italian", "Polish", "Portuguese", "Russian",
                         "Chinese", "Japanese"]
            ctk.CTkOptionMenu(c_spectra.content, variable=self.var_spectra_lang, values=languages).pack(fill="x",
                                                                                                        pady=(0, 5))

            # Manual Trigger for API calls
            self.btn_spectra_gen = ctk.CTkButton(c_spectra.content, text="⚡ Analyze & Generate Definitions",
                                                 command=self.run_spectra_analysis, fg_color="#E67E22",
                                                 hover_color="#D35400")
            self.btn_spectra_gen.pack(fill="x", pady=5)

            def set_level(choice):
                if choice == "A2 (Beginner)":
                    self.slider_spectra_threshold.set(5.5)  # Common words
                    self.slider_spectra_aoa_threshold.set(4.0)  # Child words okay
                elif choice == "B1 (Intermediate)":
                    self.slider_spectra_threshold.set(4.5)
                    self.slider_spectra_aoa_threshold.set(8.0)
                elif choice == "B2 (Upper Intermediate)":
                    self.slider_spectra_threshold.set(3.8)  # Rarer words
                    self.slider_spectra_aoa_threshold.set(10.0)  # No child words
                elif choice == "C1 (Advanced)":
                    self.slider_spectra_threshold.set(3.2)  # Very rare words
                    self.slider_spectra_aoa_threshold.set(13.0)  # Academic only

                # Update label text for sliders
                self.lbl_spectra_thresh.configure(
                    text=f"Zipf Difficulty (Max): {self.slider_spectra_threshold.get():.1f}")
                self.lbl_spectra_aoa.configure(
                    text=f"Min. Age of Acquisition: {self.slider_spectra_aoa_threshold.get():.1f}")

                self.schedule_update()

            ctk.CTkLabel(c_spectra.content, text="Quick Level Select:", font=("Arial", 12, "bold"), anchor="w").pack(
                anchor="w", pady=(5, 0))
            self.level_var = ctk.StringVar(value="Select Level...")
            self.level_dropdown = ctk.CTkOptionMenu(
                c_spectra.content,
                variable=self.level_var,
                values=["A2 (Beginner)", "B1 (Intermediate)", "B2 (Upper Intermediate)", "C1 (Advanced)"],
                command=set_level
            )
            self.level_dropdown.pack(fill="x", pady=5)

            # Inside _build_sidebar -> Spectra Section
            self._create_slider(c_spectra.content, "lbl_spectra_thresh", "Zipf Difficulty (Max)",
                                "slider_spectra_threshold",
                                1.0, 7.0, is_float=True, trigger_auto_update=False)

            self._create_slider(c_spectra.content, "lbl_spectra_aoa", "Min. Age of Acquisition",
                                "slider_spectra_aoa_threshold",
                                0.0, 25.0, is_float=True, trigger_auto_update=False)

            # API Key Input
            ctk.CTkLabel(c_spectra.content, text="API Key:", font=("Arial", 12), anchor="w").pack(anchor="w",
                                                                                                  pady=(5, 0))
            self.entry_spectra_key = ctk.CTkEntry(c_spectra.content, show="*")
            self.entry_spectra_key.insert(0, self.startup_settings.get("spectra_api_key", ""))
            self.entry_spectra_key.pack(fill="x", pady=(0, 5))
            # Base URL
            ctk.CTkLabel(c_spectra.content, text="Base URL:", font=("Arial", 12), anchor="w").pack(anchor="w")
            self.entry_spectra_url = ctk.CTkEntry(c_spectra.content)
            self.entry_spectra_url.insert(0, self.startup_settings.get("spectra_base_url", "https://api.openai.com/v1"))
            self.entry_spectra_url.pack(fill="x", pady=(0, 5))
            # Model
            ctk.CTkLabel(c_spectra.content, text="Model:", font=("Arial", 12), anchor="w").pack(anchor="w")
            self.entry_spectra_model = ctk.CTkEntry(c_spectra.content)
            self.entry_spectra_model.insert(0, self.startup_settings.get("spectra_model", "gpt-4o-mini"))
            self.entry_spectra_model.pack(fill="x", pady=(0, 5))

        def add_elem_row(txt, var_pos_name, var_ord_name):
            r = ctk.CTkFrame(grid_hf, fg_color="transparent")
            r.pack(fill="x", pady=2)
            ctk.CTkLabel(r, text=txt, font=("Arial", 12), width=110, anchor="w").pack(side="left")
            var_p = ctk.StringVar(value=self.startup_settings.get(var_pos_name, "Hidden"))
            setattr(self, f"var_{var_pos_name}", var_p)
            ctk.CTkOptionMenu(r, variable=var_p, values=["Header", "Footer", "Hidden"], width=90, height=22,
                              font=("Arial", 12), command=self.schedule_update).pack(side="left", padx=5)
            var_o = ctk.StringVar(value=str(self.startup_settings.get(var_ord_name, 1)))
            var_o.trace_add("write", lambda *args: self.schedule_update())
            setattr(self, f"var_{var_ord_name}", var_o)
            ctk.CTkEntry(r, textvariable=var_o, width=35, height=22).pack(side="right")

        add_elem_row("Chapter Title", "pos_title", "order_title")
        add_elem_row("Page Number", "pos_pagenum", "order_pagenum")
        add_elem_row("Chapter Page", "pos_chap_page", "order_chap_page")
        add_elem_row("Reading %", "pos_percent", "order_percent")
        self._create_divider_horizontal(c_hf.content)
        ctk.CTkLabel(c_hf.content, text="Progress Bar", font=("Arial", 12, "bold"), text_color=COLOR_TEXT_GRAY).pack(
            anchor="w", pady=(10, 2))
        row_progress = ctk.CTkFrame(c_hf.content, fg_color="transparent")
        row_progress.pack(fill="x", pady=2)
        ctk.CTkLabel(row_progress, text="Position:", font=("Arial", 12), width=130, anchor="w").pack(side="left")
        self.var_pos_progress = ctk.StringVar(value=self.startup_settings.get("pos_progress", "Footer (Below Text)"))
        ctk.CTkOptionMenu(row_progress, variable=self.var_pos_progress,
                          values=["Header (Above Text)", "Header (Below Text)", "Footer (Above Text)",
                                  "Footer (Below Text)", "Hidden"], command=self.schedule_update, height=22,
                          font=("Arial", 12)).pack(side="right", fill="x", expand=True)
        row_chk = ctk.CTkFrame(c_hf.content, fg_color="transparent")
        row_chk.pack(fill="x", pady=5)
        self.var_bar_ticks = ctk.BooleanVar(value=self.startup_settings.get("bar_show_ticks", True))
        ctk.CTkCheckBox(row_chk, text="Show Ticks", variable=self.var_bar_ticks, command=self.schedule_update,
                        font=("Arial", 12)).pack(side="left")
        self.var_bar_marker = ctk.BooleanVar(value=self.startup_settings.get("bar_show_marker", True))
        ctk.CTkCheckBox(row_chk, text="Show Marker", variable=self.var_bar_marker, command=self.schedule_update,
                        font=("Arial", 12)).pack(side="right")
        row_mc = ctk.CTkFrame(c_hf.content, fg_color="transparent")
        row_mc.pack(fill="x", pady=2)
        ctk.CTkLabel(row_mc, text="Marker Color:", font=("Arial", 12), width=130, anchor="w").pack(side="left")
        self.var_marker_color = ctk.StringVar(value=self.startup_settings.get("bar_marker_color", "Black"))
        ctk.CTkOptionMenu(row_mc, variable=self.var_marker_color, values=["Black", "White"], width=90, height=22,
                          font=("Arial", 12), command=self.schedule_update).pack(side="left", fill="x", expand=True)
        self._create_slider(c_hf.content, "lbl_marker_size", "Marker Radius", "slider_bar_marker_radius", 2, 10)
        self._create_slider(c_hf.content, "lbl_tick_height", "Tick Height", "slider_bar_tick_height", 2, 20)
        self._create_slider(c_hf.content, "lbl_bar_thick", "Bar Thickness", "slider_bar_height", 1, 10)
        self._create_divider_horizontal(c_hf.content)
        ctk.CTkLabel(c_hf.content, text="Specific Styles", font=("Arial", 12, "bold"), text_color=COLOR_TEXT_GRAY).pack(
            anchor="w", pady=(10, 2))
        f_adv = ctk.CTkFrame(c_hf.content, fg_color="transparent")
        f_adv.pack(fill="x")
        align_options = ["Left", "Center", "Right", "Justify"]
        r_h_align = ctk.CTkFrame(f_adv, fg_color="transparent")
        r_h_align.pack(fill="x", pady=2)
        ctk.CTkLabel(r_h_align, text="Header Align:", font=("Arial", 12), width=130, anchor="w").pack(side="left")
        self.var_header_align = ctk.StringVar(value=self.startup_settings.get("header_align", "Center"))
        ctk.CTkOptionMenu(r_h_align, variable=self.var_header_align, values=align_options, command=self.schedule_update,
                          height=22, font=("Arial", 12)).pack(side="right", fill="x", expand=True)
        self._create_slider(f_adv, "lbl_header_size", "Header Font Size", "slider_header_font_size", 8, 30)
        self._create_slider(f_adv, "lbl_header_margin", "Header Y-Offset", "slider_header_margin", 0, 80)
        self._create_divider_horizontal(f_adv)
        r_f_align = ctk.CTkFrame(f_adv, fg_color="transparent")
        r_f_align.pack(fill="x", pady=2)
        ctk.CTkLabel(r_f_align, text="Footer Align:", font=("Arial", 12), width=130, anchor="w").pack(side="left")
        self.var_footer_align = ctk.StringVar(value=self.startup_settings.get("footer_align", "Center"))
        ctk.CTkOptionMenu(r_f_align, variable=self.var_footer_align, values=align_options, command=self.schedule_update,
                          height=22, font=("Arial", 12)).pack(side="right", fill="x", expand=True)
        self._create_slider(f_adv, "lbl_footer_size", "Footer Font Size", "slider_footer_font_size", 8, 30)
        self._create_slider(f_adv, "lbl_footer_margin", "Footer Y-Offset", "slider_footer_margin", 0, 80)

    def _build_preview_area(self):
        self.preview_frame = ctk.CTkFrame(self.main_container, fg_color="#181818")
        self.preview_frame.pack(side="left", fill="both", expand=True, padx=(0, 10), pady=10)

        # Instantiate it ONCE with the new colors
        self.preview_scroll = ctk.CTkScrollableFrame(
            self.preview_frame,
            fg_color="transparent",
            scrollbar_button_color="#2B2B2B",  # Matches your card color
            scrollbar_button_hover_color="#404040"
        )

        # CRITICAL: Put the pack() method back so it actually shows up on screen!
        self.preview_scroll.pack(fill="both", expand=True)

        self.preview_scroll.grid_columnconfigure(0, weight=1)
        self.preview_scroll.grid_rowconfigure(0, weight=1)

        self.img_label = ctk.CTkLabel(self.preview_scroll, text="Open an EPUB to begin", font=("Arial", 16, "bold"),
                                      text_color="#333")
        self.img_label.grid(row=0, column=0, pady=20, padx=20)

        def pass_scroll_event(event):
            # Depending on the OS, delta is handled differently
            if event.num == 4 or event.delta > 0:
                self.preview_scroll._parent_canvas.yview_scroll(-1, "units")
            elif event.num == 5 or event.delta < 0:
                self.preview_scroll._parent_canvas.yview_scroll(1, "units")

        # Bind for Windows / MacOS
        self.img_label.bind("<MouseWheel>", pass_scroll_event)
        # Bind for Linux
        self.img_label.bind("<Button-4>", pass_scroll_event)
        self.img_label.bind("<Button-5>", pass_scroll_event)

        ctrl_bar = ctk.CTkFrame(self.preview_frame, height=50, fg_color=COLOR_TOOLBAR, corner_radius=15)
        ctrl_bar.pack(side="bottom", fill="x", padx=20, pady=20)
        f_nav = ctk.CTkFrame(ctrl_bar, fg_color="transparent")
        f_nav.place(relx=0.5, rely=0.5, anchor="center")
        ctk.CTkButton(f_nav, text="◀", width=40, command=self.prev_page, fg_color="#333").pack(side="left", padx=5)
        f_page_stack = ctk.CTkFrame(f_nav, fg_color="transparent")
        f_page_stack.pack(side="left", padx=10)
        self.lbl_page = ctk.CTkLabel(f_page_stack, text="0 / 0", font=("Arial", 14, "bold"), width=80)
        self.lbl_page.pack(side="top")
        self.entry_page = ctk.CTkEntry(f_page_stack, width=50, height=20, placeholder_text="#", justify="center",
                                       font=("Arial", 10))
        self.entry_page.pack(side="top", pady=(2, 0))
        self.entry_page.bind('<Return>', lambda e: self.go_to_page())
        ctk.CTkButton(f_nav, text="▶", width=40, command=self.next_page, fg_color="#333").pack(side="left", padx=5)
        f_zoom = ctk.CTkFrame(ctrl_bar, fg_color="transparent")
        f_zoom.pack(side="right", padx=20, pady=10)
        self._create_slider(f_zoom, "lbl_preview_zoom", "Zoom", "slider_preview_zoom", 200, 800, width=150)

    def _create_icon_btn(self, parent, text, hover_col, cmd, state="normal"):
        b = ctk.CTkButton(parent, text=text, command=cmd, state=state, width=110, height=35, corner_radius=8,
                          font=("Arial", 12, "bold"), fg_color=COLOR_CARD, hover_color=hover_col)
        b.pack(side="left", padx=5)
        return b

    def _create_divider(self, parent):
        ctk.CTkFrame(parent, width=2, height=30, fg_color="#333").pack(side="left", padx=10)

    def _create_divider_horizontal(self, parent):
        ctk.CTkFrame(parent, height=2, fg_color="#333").pack(fill="x", pady=10)

    def _create_slider(self, parent, label_attr, text, slider_attr, min_v, max_v, is_float=False, width=None, trigger_auto_update=True):
        f = ctk.CTkFrame(parent, fg_color="transparent")
        f.pack(fill="x", pady=2)

        setting_key = slider_attr.replace('slider_', '')
        default_val = self.startup_settings.get(setting_key, min_v)

        # Helper to format the label text
        def get_label_text(val):
            v_num = float(val) if is_float else int(val)
            fmt = f"{v_num:.1f}" if is_float else f"{v_num}"
            return f"{text}: {fmt}"

        lbl = ctk.CTkLabel(f, text=get_label_text(default_val), font=("Arial", 12), anchor="w", width=130)
        lbl.pack(side="left")
        setattr(self, label_attr, lbl)

        # This logic handles the "suppress update" for Spectra sliders
        def on_slide(val):
            lbl.configure(text=get_label_text(val))
            if trigger_auto_update:
                self.schedule_update()

        sld = ctk.CTkSlider(f, from_=min_v, to=max_v, command=on_slide, height=16)
        if width: sld.configure(width=width)
        sld.set(default_val)
        sld.pack(side="left", fill="x", expand=True, padx=(5, 0))

        # Store the formatter and slider for the refresh_all_slider_labels method
        setattr(self, f"fmt_{slider_attr}", get_label_text)
        setattr(self, slider_attr, sld)
        return sld

    def toggle_render_controls(self, _=None):
        mode = self.render_mode_var.get()

        # Always hide both first
        self.frm_dither.pack_forget()
        self.frm_thresh.pack_forget()

        # If Dither is selected, show Contrast/White Clip for cleaning gradients
        if mode == "Dither":
            self.frm_dither.pack(fill="x")
        # If Threshold is selected, show Threshold/Blur for text sharpening
        elif mode == "Threshold":
            self.frm_thresh.pack(fill="x")

        self.schedule_update()

    def select_file(self):
        path = filedialog.askopenfilename(filetypes=[("EPUB", "*.epub")])
        if path:
            self.processor.input_file = path
            self.current_page_index = 0
            self.lbl_file.configure(text=os.path.basename(path))
            self.progress_label.configure(text="Parsing...")
            threading.Thread(target=self._task_parse_structure).start()

    def _task_parse_structure(self):
        success = self.processor.parse_book_structure(self.processor.input_file)
        self.after(0, lambda: self._on_structure_parsed(success))

    def _on_structure_parsed(self, success):
        if not success:
            messagebox.showerror("Error", "Failed to parse EPUB.")
            return
        self.btn_chapters.configure(state="normal")
        self.open_chapter_dialog()

    def open_chapter_dialog(self):
        if not self.selected_chapter_indices: self.selected_chapter_indices = list(
            range(len(self.processor.raw_chapters)))
        ChapterSelectionDialog(self, self.processor.raw_chapters, self._on_chapters_selected)

    def _on_chapters_selected(self, selected_indices):
        self.selected_chapter_indices = selected_indices
        self.run_processing()

    def run_spectra_analysis(self):
        """Manually trigger API Analysis with Regenerate Option"""
        if not self.processor.input_file or not self.selected_chapter_indices: return
        if not self.entry_spectra_key.get():
            messagebox.showerror("Missing Key", "Please enter an OpenAI API Key.")
            return

        # NEW LOGIC: Ask user if they want to force regenerate
        force_regen = False

        # Check if we already have some definitions in memory
        has_cache = hasattr(self.processor.annotator, 'master_cache') and bool(self.processor.annotator.master_cache)

        if has_cache:
            # Ask the user
            ans = messagebox.askyesnocancel("Regenerate Annotations?",
                                            "Definitions already exist.\n\n"
                                            "Yes = Force Regenerate ALL (Overwrites existing, uses more API credits)\n"
                                            "No = Only Scan for Missing words (Cheaper)\n"
                                            "Cancel = Stop")
            if ans is None:
                return  # Cancelled
            if ans:
                force_regen = True

        self.is_processing = True
        self.progress_label.configure(text="Scanning Words...")
        settings = self.gather_current_ui_settings()

        # Pass the force_regen flag to the task
        threading.Thread(target=lambda: self._task_analyze(settings, force_regen)).start()

    def _task_analyze(self, layout_settings, force=False):
        # Init annotator with current settings
        self.processor.init_annotator(layout_settings)

        # Run scanning/fetching with FORCE flag
        self.processor.annotator.analyze_chapters(
            self.processor.raw_chapters,
            self.selected_chapter_indices,
            progress_callback=lambda v: self.update_progress_ui(v, "Analyzing"),
            force=force  # <--- Passed here
        )

        # After analysis, re-render the layout to apply new cache
        self.after(0, self.run_processing)

    def run_processing(self):
        if not self.processor.input_file: return

        self.is_processing = True
        self.pending_rerun = False  # We are starting the latest job now

        self.progress_label.configure(text="Rendering Layout...")

        # Gather settings NOW (so we get the very latest slider values)
        settings = self.gather_current_ui_settings()

        threading.Thread(target=lambda: self._task_render(settings)).start()

    def _task_render(self, layout_settings):
        success = self.processor.render_chapters(
            self.selected_chapter_indices,
            self.processor.font_path,
            int(self.slider_font_size.get()),
            int(self.slider_margin.get()),
            float(self.slider_line_height.get()),
            int(self.slider_font_weight.get()),
            int(self.slider_bottom_padding.get()),
            int(self.slider_top_padding.get()),
            text_align=self.align_dropdown.get(),
            orientation=self.orientation_var.get(),
            add_toc=self.var_toc.get(),
            show_footnotes=self.var_footnotes.get(),
            layout_settings=layout_settings,
            progress_callback=lambda v: self.update_progress_ui(v, "Layout")
        )
        self.after(0, lambda: self._done(success))

    def _done(self, success):
        self.is_processing = False

        # CHECK IF A NEW CHANGE CAME IN WHILE WE WERE WORKING
        if self.pending_rerun:
            # Recursive restart: Run again immediately with the new settings
            self.after(10, self.run_processing)
            return

        # Normal finish
        self.progress_label.configure(text="Ready")
        self.progress_bar.set(0)

        if success:
            self.btn_export.configure(state="normal")
            self.btn_cover.configure(state="normal")
            self.show_page(self.current_page_index)
        else:
            messagebox.showerror("Error", "Processing failed.")

    def update_progress_ui(self, val, stage_text="Processing"):
        try:
            # Check if the widget still exists before trying to update it
            if not self.winfo_exists(): return

            self.after(0, lambda: [
                self.progress_bar.set(val),
                self.progress_label.configure(text=f"{stage_text} {int(val * 100)}%")
            ])
        except:
            pass

    def show_page(self, idx):
        if not self.processor.is_ready: return
        self.current_page_index = idx
        img = self.processor.render_page(idx)
        base_size = int(self.slider_preview_zoom.get())
        if img.width > img.height:
            target_h = base_size
            aspect_ratio = img.width / img.height
            target_w = int(target_h * aspect_ratio)
        else:
            target_w = base_size
            aspect_ratio = img.height / img.width
            target_h = int(target_w * aspect_ratio)
        ctk_img = ctk.CTkImage(light_image=img, size=(target_w, target_h))
        self.img_label.configure(image=ctk_img, text="")
        self.lbl_page.configure(text=f"{idx + 1} / {self.processor.total_pages}")
        self.preview_scroll.update_idletasks()

    def prev_page(self):
        self.show_page(max(0, self.current_page_index - 1))

    def next_page(self):
        self.show_page(min(self.processor.total_pages - 1, self.current_page_index + 1))

    def go_to_page(self):
        try:
            target = int(self.entry_page.get())
            self.show_page(max(0, min(target - 1, self.processor.total_pages - 1)))
        except:
            pass
        self.entry_page.delete(0, 'end')

    def export_file(self):
        path = filedialog.asksaveasfilename(defaultextension=".xtc")
        if path: threading.Thread(target=lambda: self._run_export(path)).start()

    def _run_export(self, path):
        self.processor.save_xtc(path, progress_callback=lambda v: self.update_progress_ui(v, "Exporting"))
        self.after(0, lambda: messagebox.showinfo("Success", "XTC file saved."))

    def open_cover_export(self):
        if self.processor.cover_image_obj:
            CoverExportDialog(self, self._process_cover_export)
        else:
            messagebox.showinfo("Info", "No cover found.")

    def _process_cover_export(self, w, h, mode):
        path = filedialog.asksaveasfilename(defaultextension=".bmp", filetypes=[("Bitmap", "*.bmp")])
        if path: threading.Thread(target=lambda: self._run_cover_export(path, w, h, mode)).start()

    def _run_cover_export(self, path, w, h, mode):
        try:
            self.update_progress_ui(0.5, "Exporting Cover")
            img = self.processor.cover_image_obj.convert("RGB")
            if "Stretch" in mode:
                img = img.resize((w, h), Image.Resampling.LANCZOS)
            elif "Fit" in mode:
                img = ImageOps.pad(img, (w, h), color="white", centering=(0.5, 0.5))
            else:
                img = ImageOps.fit(img, (w, h), centering=(0.5, 0.5))
            img = img.convert("L")
            img = ImageEnhance.Contrast(img).enhance(1.3)
            img = ImageEnhance.Brightness(img).enhance(1.05)
            img = img.convert("1", dither=Image.Dither.FLOYDSTEINBERG)
            img.save(path, format="BMP")
            self.update_progress_ui(1.0, "Done")
            self.after(0, lambda: messagebox.showinfo("Success", "Cover saved."))
        except Exception as e:
            self.after(0, lambda: messagebox.showerror("Error", str(e)))

    def schedule_update(self, _=None):
        if not self.processor.input_file: return

        # Immediate visual feedback
        self.progress_label.configure(text="Pending changes...")

        # Cancel previous timer if it exists (standard debounce)
        if self.debounce_timer:
            try:
                self.after_cancel(self.debounce_timer)
            except:
                pass

        # Wait 500ms (increased from 200ms for stability)
        self.debounce_timer = self.after(500, self.trigger_processing)

    def trigger_processing(self):
        if self.is_processing:
            # If busy, mark that we need to run again as soon as we finish
            self.pending_rerun = True
            return

        self.run_processing()

    def gather_current_ui_settings(self):
        def get_order(var_name):
            try:
                return int(getattr(self, var_name).get())
            except:
                return 99

        spectra_en = self.var_spectra_enabled.get() if hasattr(self, 'var_spectra_enabled') else False
        spectra_key = self.entry_spectra_key.get() if hasattr(self, 'entry_spectra_key') else ""
        spectra_url = self.entry_spectra_url.get() if hasattr(self, 'entry_spectra_url') else ""
        spectra_model = self.entry_spectra_model.get() if hasattr(self, 'entry_spectra_model') else ""
        spectra_thresh = float(self.slider_spectra_threshold.get()) if hasattr(self, 'slider_spectra_threshold') else 4.0
        # NEW AoA
        spectra_aoa = float(self.slider_spectra_aoa_threshold.get()) if hasattr(self, 'slider_spectra_aoa_threshold') else 0.0
        spectra_lang = self.var_spectra_lang.get() if hasattr(self, 'var_spectra_lang') else "English"

        return {
            "font_size": int(self.slider_font_size.get()),
            "font_weight": int(self.slider_font_weight.get()),
            "line_height": float(self.slider_line_height.get()),
            "margin": int(self.slider_margin.get()),
            "top_padding": int(self.slider_top_padding.get()),
            "bottom_padding": int(self.slider_bottom_padding.get()),
            "orientation": self.orientation_var.get(),
            "text_align": self.align_dropdown.get(),
            "font_name": self.font_dropdown.get(),
            "preview_zoom": int(self.slider_preview_zoom.get()),
            "generate_toc": self.var_toc.get(),
            "show_footnotes": self.var_footnotes.get(),
            "bar_height": int(self.slider_bar_height.get()),
            "pos_title": self.var_pos_title.get(),
            "pos_pagenum": self.var_pos_pagenum.get(),
            "pos_chap_page": self.var_pos_chap_page.get(),
            "pos_percent": self.var_pos_percent.get(),
            "pos_progress": self.var_pos_progress.get(),
            "order_title": get_order("var_order_title"),
            "order_pagenum": get_order("var_order_pagenum"),
            "order_chap_page": get_order("var_order_chap_page"),
            "order_percent": get_order("var_order_percent"),
            "bar_show_ticks": self.var_bar_ticks.get(),
            "bar_show_marker": self.var_bar_marker.get(),
            "bar_marker_color": self.var_marker_color.get(),
            "bar_marker_radius": int(self.slider_bar_marker_radius.get()),
            "bar_tick_height": int(self.slider_bar_tick_height.get()),
            "header_align": self.var_header_align.get(),
            "header_font_size": int(self.slider_header_font_size.get()),
            "header_margin": int(self.slider_header_margin.get()),
            "footer_align": self.var_footer_align.get(),
            "footer_font_size": int(self.slider_footer_font_size.get()),
            "footer_margin": int(self.slider_footer_margin.get()),
            "render_mode": self.render_mode_var.get(),
            "bit_depth": self.bit_depth_var.get(),  # Critical: 1-bit vs 2-bit
            "white_clip": int(self.slider_white_clip.get()),
            "contrast": float(self.slider_contrast.get()),
            "text_threshold": int(self.slider_text_threshold.get()),
            "text_blur": float(self.slider_text_blur.get()),

            # Spectra
            "spectra_enabled": spectra_en,
            "spectra_api_key": spectra_key,
            "spectra_base_url": spectra_url,
            "spectra_model": spectra_model,
            "spectra_threshold": spectra_thresh,
            "spectra_aoa_threshold": spectra_aoa,  # NEW
            "spectra_target_lang": spectra_lang,
        }

    def load_startup_defaults(self):
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r") as f:
                    self.startup_settings.update(json.load(f))
            except:
                pass

    def refresh_presets_list(self):
        files = glob.glob(os.path.join(PRESETS_DIR, "*.json"))
        names = [os.path.basename(f).replace(".json", "") for f in files]
        names.sort()
        self.preset_dropdown.configure(values=names if names else ["No Presets"])

    def load_selected_preset(self, choice):
        if choice in ["No Presets", "Select Preset..."]: return
        path = os.path.join(PRESETS_DIR, f"{choice}.json")
        if os.path.exists(path):
            with open(path, "r") as f: self.apply_settings_dict(json.load(f))

    def save_new_preset(self):
        name = simpledialog.askstring("New Preset", "Name:")
        if name:
            safe = "".join(x for x in name if x.isalnum() or x in " -_")
            with open(os.path.join(PRESETS_DIR, f"{safe}.json"), "w") as f:
                json.dump(self.gather_current_ui_settings(), f, indent=4)
            self.refresh_presets_list()
            self.preset_var.set(safe)

    def save_current_as_default(self):
        with open(SETTINGS_FILE, "w") as f: json.dump(self.gather_current_ui_settings(), f, indent=4)
        messagebox.showinfo("Saved", "Settings saved as default.")

    def reset_to_factory(self):
        if messagebox.askyesno("Reset", "Reset to factory settings?"): self.apply_settings_dict(FACTORY_DEFAULTS)

    def apply_settings_dict(self, s):
        defaults = FACTORY_DEFAULTS.copy()
        defaults.update(s)
        s = defaults

        # --- Standard Sliders & Dropdowns ---
        self.bit_depth_var.set(s.get("bit_depth", "1-bit (XTG)"))
        self.slider_font_size.set(s['font_size'])
        self.slider_font_weight.set(s['font_weight'])
        self.slider_line_height.set(s['line_height'])
        self.slider_margin.set(s['margin'])
        self.slider_top_padding.set(s['top_padding'])
        self.slider_bottom_padding.set(s['bottom_padding'])
        self.orientation_var.set(s['orientation'])
        self.align_dropdown.set(s['text_align'])
        self.slider_preview_zoom.set(s['preview_zoom'])
        self.var_toc.set(s['generate_toc'])
        self.var_footnotes.set(s.get('show_footnotes', True))
        self.slider_bar_height.set(s['bar_height'])

        # --- Header/Footer/Progress ---
        self.var_pos_title.set(s['pos_title'])
        self.var_pos_pagenum.set(s['pos_pagenum'])
        self.var_pos_chap_page.set(s['pos_chap_page'])
        self.var_pos_percent.set(s['pos_percent'])
        self.var_pos_progress.set(s['pos_progress'])
        self.var_order_title.set(str(s['order_title']))
        self.var_order_pagenum.set(str(s['order_pagenum']))
        self.var_order_chap_page.set(str(s['order_chap_page']))
        self.var_order_percent.set(str(s['order_percent']))
        self.var_bar_ticks.set(s['bar_show_ticks'])
        self.var_bar_marker.set(s['bar_show_marker'])
        self.var_marker_color.set(s['bar_marker_color'])
        self.slider_bar_marker_radius.set(s['bar_marker_radius'])
        self.slider_bar_tick_height.set(s['bar_tick_height'])
        self.var_header_align.set(s['header_align'])
        self.slider_header_font_size.set(s['header_font_size'])
        self.slider_header_margin.set(s['header_margin'])
        self.var_footer_align.set(s['footer_align'])
        self.slider_footer_font_size.set(s['footer_font_size'])
        self.slider_footer_margin.set(s['footer_margin'])

        # --- Rendering ---
        self.render_mode_var.set(s.get("render_mode", "Threshold"))
        self.slider_white_clip.set(s.get("white_clip", 220))
        self.slider_contrast.set(s.get("contrast", 1.2))
        self.slider_text_threshold.set(s.get("text_threshold", 130))
        self.slider_text_blur.set(s.get("text_blur", 1.0))

        # --- SPECTRA AI SETTINGS (FIXED) ---
        if hasattr(self, 'var_spectra_enabled'):
            self.var_spectra_enabled.set(s.get('spectra_enabled', False))

        if hasattr(self, 'slider_spectra_threshold'):
            self.slider_spectra_threshold.set(s.get('spectra_threshold', 4.0))

        if hasattr(self, 'slider_spectra_aoa_threshold'):
            self.slider_spectra_aoa_threshold.set(s.get('spectra_aoa_threshold', 0.0))

        # FIX: Explicitly update the text entry fields
        if hasattr(self, 'entry_spectra_key'):
            self.entry_spectra_key.delete(0, 'end')
            self.entry_spectra_key.insert(0, s.get('spectra_api_key', ""))

        if hasattr(self, 'entry_spectra_url'):
            self.entry_spectra_url.delete(0, 'end')
            self.entry_spectra_url.insert(0, s.get('spectra_base_url', "https://api.openai.com/v1"))

        if hasattr(self, 'entry_spectra_model'):
            self.entry_spectra_model.delete(0, 'end')
            self.entry_spectra_model.insert(0, s.get('spectra_model', "gpt-4o-mini"))

        if hasattr(self, 'var_spectra_lang'):
            self.var_spectra_lang.set(s.get('spectra_target_lang', "English"))

        # --- Final UI Refresh ---
        self.refresh_all_slider_labels()

        # Handle Font Change
        if s["font_name"] in self.font_options:
            self.font_dropdown.set(s["font_name"])
            self.processor.font_path = self.font_map[s["font_name"]]
        else:
            self.font_dropdown.set("Default (System)")
            self.processor.font_path = self.font_map["Default (System)"]

        self.toggle_render_controls()
        self.schedule_update()

    def on_font_change(self, choice):
        self.processor.font_path = self.font_map[choice]
        self.schedule_update()


if __name__ == "__main__":
    app = ModernApp()
    app.mainloop()