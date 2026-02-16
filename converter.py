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
    "toc_insert_page": 1,
    "font_size": 28,
    "font_weight": 400,
    "line_height": 1.4,
    "margin": 20,
    "top_padding": 15,
    "bottom_padding": 32,
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

            #print(f"Loaded {count} words from AoA database.")
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

        # Filter what needs fetching
        if force:
            to_fetch = word_context_map
        else:
            to_fetch = {k: ctx for k, ctx in word_context_map.items() if k not in self.master_cache}

        if not to_fetch: return {}

        # --- 1. PREPARE INPUT & RECOVERY MAPS ---
        items_str = ""

        # RECOVERY MAPS
        reverse_lookup = {}  # "word" -> ["word|hash1", "word|hash2"]
        hash_lookup = {}  # "hash" -> "word|hash"
        ordered_keys = []  # ["word|hash", "word|hash"] (for index recovery 1, 2, 3...)

        for i, (unique_key, context) in enumerate(to_fetch.items()):
            target_word, ctx_hash = unique_key.split('|')

            # Map 1: Simple Word Recovery
            if target_word not in reverse_lookup:
                reverse_lookup[target_word] = []
            reverse_lookup[target_word].append(unique_key)

            # Map 2: Hash Recovery (Fixes "Orphaned Response: '3e8cdd'")
            hash_lookup[ctx_hash] = unique_key

            # Map 3: Index Recovery (Fixes "Orphaned Response: '1'")
            ordered_keys.append(unique_key)

            clean_context = context.replace('"', "'").replace('\n', ' ').strip()[:300]

            # We use a 1-based index in the prompt to match likely AI numbering
            items_str += (
                f"Item {i + 1}:\n"
                f"ID: {unique_key}\n"
                f"WORD: {target_word}\n"
                f"CONTEXT: {clean_context}\n"
                f"---\n"
            )

        # --- LANGUAGE & PROMPT SETUP (Kept largely the same, just tightened) ---
        lang_map = {
            "English": "en", "Spanish": "es", "French": "fr",
            "German": "de", "Italian": "it", "Polish": "pl",
            "Portuguese": "pt", "Russian": "ru", "Chinese": "zh", "Japanese": "ja"
        }
        target_code = lang_map.get(self.target_lang, "en")
        raw_lang = self.language.lower().strip()

        # ... (Language Code Logic omitted for brevity, it was fine) ...
        # Assume is_translation logic is here
        is_translation = (
                    target_code != 'en' and self.language.startswith('en') == False)  # Simplified check for snippet

        if self.target_lang != "English" and not self.language.lower().startswith(target_code):
            is_translation = True

        # Determine Mode
        if is_translation:
            field_name = "translation"
            system_role = f"You are a professional translator. Output JSON only. Answer in {self.target_lang}."
            task_instruction = f"TASK: TRANSLATE 'WORD' accurately into {self.target_lang} based on 'CONTEXT'."
        else:
            field_name = "synonym"
            system_role = f"You are a Thesaurus. Output JSON only. Answer in {self.target_lang}."
            task_instruction = f"TASK: Provide a simpler SYNONYM or SHORT PHRASE for 'WORD' in {self.target_lang}."

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
                                field_name: {"type": "string"}
                            },
                            "required": ["id", field_name],
                            "additionalProperties": False
                        }
                    }
                },
                "required": ["entries"],
                "additionalProperties": False
            }
        }

        prompt = (
            f"{task_instruction}\n"
            f"RULES:\n"
            f"1. Return the EXACT ID provided in the input.\n"
            f"2. Keep definitions short (1-3 words).\n"
            f"3. Output valid JSON.\n"
            f"\n"
            f"INPUT LIST:\n"
            f"{items_str}"
        )

        try:
            # 4. SEND TO API
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_role},
                    {"role": "user", "content": prompt}
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": json_schema
                },
                temperature=0.2
            )

            content = response.choices[0].message.content.strip()
            entries_found = []

            try:
                # 4. SEND TO API
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_role},
                        {"role": "user", "content": prompt}
                    ],
                    response_format={
                        "type": "json_schema",
                        "json_schema": json_schema
                    },
                    temperature=0.2
                )

                content = response.choices[0].message.content.strip()
                entries_found = []

                # A. Try Standard JSON
                try:
                    clean = re.sub(r'^```json\s*', '', content)
                    clean = re.sub(r'\s*```$', '', clean)
                    json_data = json.loads(clean)
                    if "entries" in json_data:
                        entries_found = json_data["entries"]
                except json.JSONDecodeError:
                    pass

                # B. Regex Fallback (if JSON fails)
                if not entries_found:
                    # Matches: "id": "ANYTHING", "synonym": "ANYTHING"
                    pattern = r'"id"\s*:\s*"([^"]+)"\s*,\s*"(?:synonym|definition|translation|' + field_name + r')"\s*:\s*"([^"]+)"'
                    matches = re.findall(pattern, content, flags=re.IGNORECASE)
                    entries_found = [{"id": m[0], field_name: m[1]} for m in matches]

                for entry in entries_found:
                    raw_id = str(entry.get("id", "")).strip()

                    # --- RECOVERY LOGIC START ---

                    # 1. Normalize ID (Remove "ID:", "Item", and extra spaces)
                    # This fixes "ITEM 1" -> "1"
                    clean_id_str = re.sub(r'^(ID:|Item)\s*', '', raw_id, flags=re.IGNORECASE).strip()

                    # 2. Junk Filter (Instruction Echo Fix)
                    if len(clean_id_str) > 40 and " " in clean_id_str:
                        # print(f"[Spectra DEBUG] 🗑️ Discarding junk ID: '{clean_id_str[:30]}...'")
                        continue

                    final_key = None

                    # --- STRATEGY A: Exact Match ---
                    if raw_id in to_fetch:
                        final_key = raw_id

                    # --- STRATEGY B: Number/Index Match ---
                    # Fixes "Item 1", "ITEM 1", "1"
                    elif clean_id_str.isdigit():
                        idx = int(clean_id_str) - 1
                        if 0 <= idx < len(ordered_keys):
                            final_key = ordered_keys[idx]

                    # --- STRATEGY C: Hash Extraction ---
                    # Fixes "word | hash" (spaces) or "gratfully|hash" (typos)
                    elif '|' in raw_id:
                        possible_hash = raw_id.split('|')[-1].strip()
                        if possible_hash in hash_lookup:
                            final_key = hash_lookup[possible_hash]

                    # --- STRATEGY D: Bare Hash Match ---
                    # Fixes "3e8cdd"
                    elif raw_id in hash_lookup:
                        final_key = hash_lookup[raw_id]

                    # --- STRATEGY E: Word Fallback ---
                    # Fixes cases where hash is missing but word is correct
                    elif raw_id in reverse_lookup:
                        for complex_key in reverse_lookup[raw_id]:
                            val = entry.get(field_name) or entry.get("definition") or entry.get("synonym") or entry.get(
                                "translation")
                            if val:
                                clean_val = val.strip().rstrip('.,;!')
                                if self.target_lang != "German": clean_val = clean_val.lower()
                                if clean_val.lower() != raw_id.lower():
                                    self.master_cache[complex_key] = clean_val
                        continue

                        # --- ASSIGNMENT ---
                    if final_key:
                        val = entry.get(field_name) or entry.get("definition") or entry.get("synonym") or entry.get(
                            "translation")
                        if val:
                            clean_val = val.strip().rstrip('.,;!')
                            if self.target_lang != "German": clean_val = clean_val.lower()

                            # Prevent circular definitions
                            original_word = final_key.split('|')[0]
                            if clean_val.lower() != original_word.lower():
                                self.master_cache[final_key] = clean_val

                    # Only log genuine errors now
                    elif len(raw_id) < 20:
                        print(f"[Spectra DEBUG] ❌ Orphaned Response: '{raw_id}'")

            except Exception as e:
                print(f"Spectra API Error: {e}")



        except Exception as e:
            print(f"Spectra API Error: {e}")

    def analyze_chapters(self, chapters_list, selected_indices, progress_callback=None, force=False):
        if not self.enabled: return

        word_pattern = re.compile(r'\b[a-zA-Z]{3,}\b')
        split_pattern = r'(?<!\bMr)(?<!\bMrs)(?<!\bDr)(?<!\bMs)(?<!\bSt)(?<!\bProf)(?<!\bCapt)(?<!\bGen)(?<!\bSen)(?<!\bRev)(?<=[.!?])\s+'

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

                # Create a hash of the specific sentence context
                ctx_hash = hashlib.md5(sentence.encode('utf-8')).hexdigest()[:6]

                for match in word_pattern.finditer(sentence):
                    word_val = match.group()
                    start_index = match.start()
                    w_lower = word_val.lower()

                    # --- SMART PROPER NOUN FILTER ---
                    if word_val[0].isupper():
                        # 1. If in middle of sentence, it's a Name/Place -> Skip
                        if start_index > 0:
                            continue

                        # 2. If at start, check if it's a Name using Frequency Comparison
                        # "London" (high freq) vs "london" (low freq) -> Name -> SKIP
                        # "Apple" (low freq) vs "apple" (high freq) -> Noun -> KEEP
                        if HAS_WORDFREQ:
                            freq_cap = wordfreq.zipf_frequency(word_val, self.language)
                            freq_low = wordfreq.zipf_frequency(w_lower, self.language)

                            # If the Capitalized version is more common or equal, it's likely a Name
                            if freq_cap >= freq_low:
                                continue

                            # Extra safety: If lowercase word essentially doesn't exist
                            if freq_low == 0:
                                continue
                    # --------------------------------

                    zipf_score = self.get_difficulty(w_lower)

                    # 1. Zipf Check
                    is_candidate = (1.5 < zipf_score < self.threshold)

                    # 2. AoA Check
                    if is_candidate and self.aoa_threshold > 0.5 and self.language == 'en':
                        aoa_val = self.get_aoa(w_lower)
                        if aoa_val > 0 and aoa_val < self.aoa_threshold:
                            is_candidate = False

                    if is_candidate:
                        unique_key = f"{w_lower}|{ctx_hash}"
                        if force or unique_key not in self.master_cache:
                            batch_items[unique_key] = sentence


        if batch_items:
            items = list(batch_items.items())
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
    # 1. Remove all @font-face blocks to prevent MuPDF errors about missing relative files
    # We use re.DOTALL so the dot (.) matches newlines, catching multi-line blocks
    css_text = re.sub(r'@font-face\s*\{[^}]+\}', '', css_text, flags=re.IGNORECASE | re.DOTALL)

    if target_font_family is None:
        return css_text

    # 2. Force the custom font family on all elements
    # We stop at ; or } or ! to avoid breaking the CSS syntax
    css_text = re.sub(r'font-family\s*:\s*[^;!}]+', f'font-family: {target_font_family}', css_text, flags=re.IGNORECASE)

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
    # Iterate over ALL items to find images, not just those marked specifically as ITEM_IMAGE
    # (Some EPUBs mislabel the cover as a generic document)
    for item in book.get_items():
        try:
            # Check if it looks like an image based on media type or extension
            media_type = item.media_type.lower()
            name = item.get_name()

            if "image" in media_type or name.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp')):
                # 1. Get the content
                content = item.get_content()

                # 2. Convert to Base64
                b64_data = base64.b64encode(content).decode('utf-8')
                data_uri = f"data:{media_type};base64,{b64_data}"

                # 3. Store using the BASENAME (filename only)
                # We decode URL chars (e.g., "my%20image.jpg" -> "my image.jpg")
                clean_name = unquote(os.path.basename(name))

                # Store exact match
                image_map[clean_name] = data_uri

                # Store lowercase match for loose matching
                image_map[clean_name.lower()] = data_uri
        except Exception as e:
            print(f"Failed to extract image {item.get_name()}: {e}")
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
    user_home = os.path.expanduser("~")
    fonts_dir = os.path.join(user_home, "Xlibre", "Fonts")

    if not os.path.exists(fonts_dir):
        os.makedirs(fonts_dir, exist_ok=True)

    font_map = {}
    for item in os.listdir(fonts_dir):
        item_path = os.path.join(fonts_dir, item)

        if os.path.isdir(item_path):
            # Scan inside the family folder for .ttf or .otf
            files = [f for f in os.listdir(item_path) if f.lower().endswith((".ttf", ".otf"))]
            if not files: continue

            # Identify the "Main" font file (not Bold/Italic) to use as the base
            candidates = [f for f in files if "bold" not in f.lower() and "italic" not in f.lower()]
            main_file = candidates[0] if candidates else files[0]

            # Store the absolute path for the renderer
            font_map[item] = os.path.abspath(os.path.join(item_path, main_file))

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
    def __init__(self, parent, chapters_list, initial_selection, callback):
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

        # Create a set for fast lookup if we have a selection
        selected_set = set(initial_selection) if initial_selection is not None else set()

        for i, chap in enumerate(chapters_list):

            # --- LOGIC FIX START ---
            if initial_selection is None:
                # FIRST RUN: Use your Regex logic to hide "Section X"
                is_generic = re.match(r"^Section \d+$", chap['title'])
                should_check = not is_generic
            else:
                # SUBSEQUENT RUNS: Restore exactly what the user had last time
                should_check = i in selected_set
            # --- LOGIC FIX END ---

            var = ctk.BooleanVar(value=should_check)
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

        # Allow empty selection (no TOC) but warn user
        if not selected_indices:
            if not messagebox.askyesno("Warning",
                                       "No chapters selected. The TOC will be empty. Continue?"):
                return

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
        # 1. Collect EVERY item in the EPUB regardless of what ebooklib thinks it is
        all_items = list(book.get_items())

        # Potential candidates list (we'll rank them by probability)
        candidates = []

        for item in all_items:
            name = item.get_name().lower()
            content = item.get_content()

            # Skip empty items
            if not content:
                continue

            # A: Check if the filename looks like a cover
            # We look for 'cover', 'front', 'okladka' (Polish), or 'thumbnail'
            is_cover_named = any(x in name for x in ['cover', 'front', 'okladka', 'thumb', 'jacket'])

            # B: Check for image extensions
            is_image_ext = any(name.endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.webp'])

            if is_cover_named or is_image_ext:
                try:
                    # Try to force-open the bytes as an image
                    img = Image.open(io.BytesIO(content))

                    # Filter out tiny icons (like social media logos or dividers)
                    if img.width > 250 and img.height > 250:
                        # Give priority to things actually named "cover"
                        score = 100 if is_cover_named else 0
                        # Give priority to larger images (covers are usually the biggest)
                        score += (img.width * img.height) // 10000

                        candidates.append((score, img, name))
                except:
                    continue

        # 2. Sort by score (Highest first) and return the winner
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            winner_score, winner_img, winner_name = candidates[0]
            print(f"DEBUG: Selected Cover -> {winner_name} (Score: {winner_score})")
            return winner_img

        # 3. Final desperation: if metadata has a 'cover' tag, try to find THAT filename
        try:
            meta_id = book.get_metadata('OPF', 'cover')[0][1]
            item = book.get_item_with_id(meta_id)
            if item:
                return Image.open(io.BytesIO(item.get_content()))
        except:
            pass

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
        self.cover_image_obj = self._find_cover_image(book)

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

        # 1. Force comfortable line height if Spectra is enabled
        spectra_enabled = self.layout_settings.get("spectra_enabled", False)
        if spectra_enabled:
            self.line_height = max(line_height, 2.2)
        else:
            self.line_height = line_height

        # 2. Orientation logic
        if orientation == "Landscape":
            self.screen_width = DEFAULT_SCREEN_HEIGHT
            self.screen_height = DEFAULT_SCREEN_WIDTH
        else:
            self.screen_width = DEFAULT_SCREEN_WIDTH
            self.screen_height = DEFAULT_SCREEN_HEIGHT

        # 3. Reset docs and maps
        for entry in self.fitz_docs:
            entry[0].close()
        self.fitz_docs = []
        self.page_map = []
        self.page_annotation_data = {}

        self.init_annotator(self.layout_settings)

        # 4. CSS Font Generation
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
        content_height = max(1, self.screen_height - self.top_padding - self.bottom_padding)

        # --- 5. GLOBAL CSS (The Hybrid Approach) ---
        # --- 5. GLOBAL CSS (Optimized for EM Scaling) ---
        # --- 5. GLOBAL CSS (Optimized for EM Scaling & Calibre Override) ---
        custom_css = f"""
                    <style>
                        {font_face_rule}
                        @page {{ size: {self.screen_width}pt {content_height}pt; margin: 0; }}

                        /* 1. RESET EVERYTHING */
                        html, body {{
                            height: 100%;
                            width: 100%;
                            margin: 0 !important;
                            padding: 0 !important;
                        }}

                        /* 2. BODY CONTAINER CONTROLS */
                        body {{
                            font-family: {font_family_val} !important;
                            font-size: {self.font_size}pt !important;
                            font-weight: {self.font_weight} !important;
                            line-height: {self.line_height} !important;
                            text-align: {self.text_align} !important;
                            color: black !important;
                            background-color: white !important;
                            /* This padding applies the SLIDER margin to the whole page */
                            padding: {self.margin}px !important; 
                            box-sizing: border-box !important;
                        }}

                        /* 3. OVERRIDE CALIBRE/GENERIC TAGS */
                        /* We target broad tags to kill specific margins/padding defined by the book */
                        p, div, li, dd, dt, span {{
                            font-family: inherit !important;
                            line-height: inherit !important;
                            color: inherit !important;
                            
                            /* REMOVED margin-left/right: 0 here so we can override it for blockquotes below */
                            padding-left: 0;
                            padding-right: 0;
                            margin-top: 0.5em;
                            margin-bottom: 0.5em;
                            text-indent: 1.5em;
                            max-width: 100% !important;
                            
                        }}
                        
                        /* ADD THIS NEW BLOCK */
                        blockquote {{
                            margin-left: 1.5em !important;  /* Give visual indication of quote */
                            margin-right: 1.5em !important;
                            font-style: italic;             /* Optional: helps distinguish quotes */
                        }}
                        
                        /* Ensure centered text doesn't have an indent (Helper class if needed) */
                        .center-text {{
                            text-align: center !important;
                            text-indent: 0 !important;
                        }}
                                                
                        /* Conservative scale: Subtle enough to save space, distinct enough to read */
                        h1 {{ font-size: 1.35em !important; margin-top: 0.8em; margin-bottom: 0.4em; }}
                        h2 {{ font-size: 1.25em !important; margin-top: 0.7em; margin-bottom: 0.3em; }}
                        h3 {{ font-size: 1.15em !important; margin-top: 0.6em; margin-bottom: 0.2em; }}
                        
                        /* H4-H6 are now the same size as body text, but distinguished by style */
                        h4, h5, h6 {{ 
                            font-size: 1.0em !important; 
                            font-style: italic; 
                            text-transform: uppercase; 
                            letter-spacing: 0.05em; 
                            margin-top: 0.5em;
                        }}
                        
                        h1, h2, h3, h4, h5, h6 {{ 
                            text-indent: 0 !important;
                            font-weight: bold !important;
                            line-height: 1.1 !important; /* Tighter line height for headers looks cleaner */
                            page-break-after: avoid;
                        }}

                        /* Fix Image containers being restricted */
                        .svg-wrapper-replaced, img {{
                            max-width: 100% !important;
                            height: auto !important;
                        }}

                        /* ... Footnote styling remains same ... */
                        .inline-footnote-box {{
                            display: block; 
                            margin: 15px 0px; 
                            padding: 5px 0px 5px 15px; 
                            border-left: 4px solid black !important;
                            font-size: {int(self.font_size * 0.85)}pt !important;
                            line-height: {self.line_height} !important;
                            text-indent: 0 !important;
                        }}
                        .inline-footnote-box strong, 
                        .inline-footnote-box p, 
                        .inline-footnote-box div, 
                        .inline-footnote-box span {{
                            display: inline !important; 
                            margin: 0 !important; 
                            padding: 0 !important; 
                            text-indent: 0 !important;
                        }}
                        .inline-footnote-box strong {{ margin-right: 4px !important; }}
                    </style>
                """

        temp_chapter_starts = []
        running_page_count = 0
        render_dir = tempfile.gettempdir()

        final_toc_titles = []
        total_chaps = len(self.raw_chapters)
        selected_set = set(selected_indices)

        split_pattern = r'(?<!\bMr)(?<!\bMrs)(?<!\bDr)(?<!\bMs)(?<!\bSt)(?<!\bProf)(?<!\bCapt)(?<!\bGen)(?<!\bSen)(?<!\bRev)(?<=[.!?])\s+'
        word_pattern = re.compile(r'\b[a-zA-Z]{3,}\b')

        for idx, chapter in enumerate(self.raw_chapters):
            if progress_callback: progress_callback((idx / total_chaps) * 0.9)
            temp_html_path = os.path.join(render_dir, f"render_temp_{int(time.time())}_{idx}.html")
            import copy
            soup = copy.copy(chapter['soup'])

            # Spectra logic
            annotation_queues = {}
            if spectra_enabled and idx in selected_set and self.annotator and self.annotator.master_cache:
                full_text = soup.get_text(" ", strip=True)
                sentences = re.split(split_pattern, full_text)
                for sentence in sentences:
                    sentence = sentence.strip()
                    if not sentence: continue
                    ctx_hash = hashlib.md5(sentence.encode('utf-8')).hexdigest()[:6]
                    matches = word_pattern.findall(sentence)
                    for w in matches:
                        w_lower = w.lower()
                        unique_key = f"{w_lower}|{ctx_hash}"
                        if unique_key in self.annotator.master_cache:
                            if w_lower not in annotation_queues:
                                annotation_queues[w_lower] = []
                            annotation_queues[w_lower].append(self.annotator.master_cache[unique_key])

            # --- APPLY FORMATTING PROTECTION (2-arg version) ---
            soup = self._protect_formatting(soup, self.book_css)

            if show_footnotes:
                soup = self._inject_inline_footnotes(soup, chapter.get('filename', ''))

            soup = hyphenate_html_text(soup, self.book_lang)

            # --- SVG WRAPPER FIX ---
            for svg in soup.find_all('svg'):
                image_node = svg.find('image') or svg.find('image', recursive=True)
                if image_node:
                    href = image_node.get('xlink:href') or image_node.get('href')
                    if href:
                        new_img = soup.new_tag('img', src=href)
                        new_img['class'] = "svg-wrapper-replaced"  # <--- The tag we look for
                        # Force 100% width here
                        new_img[
                            'style'] = "width: 100% !important; height: auto !important; display: block !important; margin: 0 auto !important;"
                        svg.replace_with(new_img)

            # --- IMAGE PROCESSING (SIZE DETECTION) ---

            # --- IMAGE PROCESSING (SIZE DETECTION) ---
            for img_tag in soup.find_all('img'):
                raw_src = img_tag.get('src', '')
                if not raw_src: continue
                src_filename = unquote(os.path.basename(raw_src))

                # Try to find the image data in our extracted dictionary
                found_data = self.book_images.get(src_filename) or self.book_images.get(
                    src_filename.lower())

                if found_data:
                    # 1. CRITICAL: Set the Base64 data FIRST
                    img_tag['src'] = found_data

                    # 2. Check if this is the SVG cover we tagged earlier
                    classes = img_tag.get('class', [])
                    if isinstance(classes, str): classes = classes.split()

                    if 'svg-wrapper-replaced' in classes:
                        # It is the cover. We already set width: 100% in the SVG block.
                        # Stop here so we don't overwrite it with "width: auto".
                        continue

                    # 3. Standard sizing logic for all other images
                    is_big_photo = False
                    try:
                        b64_str = found_data.split(',', 1)[1] if ',' in found_data else found_data
                        image_bytes = base64.b64decode(b64_str)
                        with Image.open(io.BytesIO(image_bytes)) as tmp_img:
                            w, h = tmp_img.size
                            if w > 150 or h > 150:
                                is_big_photo = True
                    except:
                        pass

                    if is_big_photo:
                        img_tag['style'] = (img_tag.get('style', '') +
                                            "; display: block !important; margin: 10px auto !important; max-width: 100% !important; width: auto !important; height: auto !important; clear: both;"
                                            ).strip()

                        parent = img_tag.parent
                        if parent and parent.name in ['div', 'figure', 'p']:
                            parent['style'] = (parent.get('style', '') +
                                               "; text-align: center !important; width: auto !important; max-width: 100% !important; margin-left: auto !important; margin-right: auto !important;"
                                               ).strip()
                    else:
                        img_tag['style'] = (img_tag.get('style', '') +
                                            "; display: inline !important; vertical-align: middle !important; margin: 0 5px !important;"
                                            ).strip()
                else:
                    img_tag['style'] = "display: none !important;"

            cover_node = soup.find('img', class_="svg-wrapper-replaced")

            if cover_node and soup.body:
                # A. Extract the image tag to keep it safe
                cover_node = cover_node.extract()

                # B. NUKE the body content (Removes all text, divs, and invisible newlines)
                soup.body.clear()

                # C. Put the image back as the ONLY element
                soup.body.append(cover_node)

                # D. Inject CSS to kill the global padding for THIS chapter only
                # This ensures 100% Height + 0 Padding = Fits exactly on one page.
                style_tag = soup.new_tag("style")
                style_tag.string = """
                                body { 
                                    padding: 0 !important; 
                                    margin: 0 !important; 
                                    height: 100% !important; 
                                    overflow: hidden !important; 
                                }
                                img.svg-wrapper-replaced {
                                    width: 100% !important; 
                                    height: 100% !important; 
                                    object-fit: contain !important; 
                                    margin: 0 !important; 
                                    display: block !important;
                                }
                            """
                if soup.head:
                    soup.head.append(style_tag)
                else:
                    head = soup.new_tag("head")
                    head.append(style_tag)
                    soup.insert(0, head)
            # =========================================================

            if idx in selected_set:
                temp_chapter_starts.append(running_page_count)
                final_toc_titles.append(chapter['title'])

            body_content = "".join([str(x) for x in soup.body.contents]) if soup.body else str(soup)
            final_html = f"<html lang='{self.book_lang}'><head><style>{patched_css}</style>{custom_css}</head><body>{body_content}</body></html>"

            with open(temp_html_path, "w", encoding="utf-8") as f:
                f.write(final_html)

            doc = fitz.open(temp_html_path)
            rect = fitz.Rect(0, 0, self.screen_width, content_height)
            doc.layout(rect=rect)
            self.fitz_docs.append((doc, chapter['has_image']))
            current_doc_idx = len(self.fitz_docs) - 1

            for page_i, page in enumerate(doc):
                page_defs = {}
                words_on_page = page.get_text("words")
                for *coords, text, block_n, line_n, word_n in words_on_page:
                    clean_text = text.replace('\u00ad', '').strip('.,!?;:"()[]{}“”‘’—–-').lower()
                    if clean_text in annotation_queues:
                        queue = annotation_queues[clean_text]
                        if queue:
                            definition = queue.pop(0)
                            coord_key = f"{int(coords[0])}_{int(coords[1])}"
                            page_defs[coord_key] = definition
                if page_defs:
                    self.page_annotation_data[(current_doc_idx, page_i)] = page_defs
                self.page_map.append((current_doc_idx, page_i))

            running_page_count += len(doc)

        # (TOC generation remains the same)
        num_toc_pages = 0
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

        requested_start = self.layout_settings.get("toc_insert_page", 1) - 1
        toc_insert_index = max(0, min(requested_start, len(self.page_map)))

        self.toc_data_final = []
        for i, title in enumerate(final_toc_titles):
            raw_start_index = temp_chapter_starts[i]
            display_page = raw_start_index + 1
            if raw_start_index >= toc_insert_index:
                display_page += num_toc_pages
            self.toc_data_final.append((title, display_page))

        if add_toc and final_toc_titles:
            self.toc_pages_images = self._render_toc_pages(self.toc_data_final, toc_row_height, toc_main_size,
                                                           toc_header_size)
        else:
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

    def _protect_formatting(self, soup, css_text):
        """
        Refined: Strips rigid layout locks but PRESERVES structural indentation (poems/letters)
        and meaningful scene breaks.
        """
        spacing_classes = set()

        # 1. HARD KILL PATTERN: Things that break the screen flow.
        # We REMOVE margin-left/padding-left/margin-top from this list to handle them intelligently below.
        nuclear_pattern = re.compile(
            r'(?:^|;)\s*(width|height|max-width|max-height|line-height|'
            r'background-color)\s*:[^;]+',
            flags=re.IGNORECASE
        )

        # 2. FONT KILL PATTERN: Kill absolute sizes, allow relatives if you want (or kill all).
        # Currently set to kill absolute units but keep em/%.
        absolute_font_pattern = re.compile(
            r'(?:^|;)\s*font-size\s*:\s*[\d\.]+(?:px|cm|mm|in|pc|pt)\s*(?:!important)?',
            flags=re.IGNORECASE
        )

        for tag in soup.find_all(True):
            # A. Attribute Cleaning (Keep basic table attributes if needed, but usually strip)
            if tag.name not in ['img', 'table', 'td', 'th']:
                if tag.has_attr('width'): del tag['width']
                if tag.has_attr('height'): del tag['height']

            # B. Style Cleaning
            style = tag.get('style')
            if style:
                # 1. Preserve Alignment
                alignment_match = re.search(r'text-align\s*:\s*(center|right|justify)', style, re.I)

                # 2. Preserve Indentation (Vital for poems/letters)
                # We look for margin-left or padding-left > 0
                indent_match = re.search(r'(margin-left|padding-left)\s*:\s*([\d\.]+(?:em|px|%|pt))', style, re.I)

                # 3. Preserve Scene Breaks (Large top margins)
                # If margin-top is significant (>1em or >20px), we might want to keep it or swap it for a <br>
                top_margin_match = re.search(r'margin-top\s*:\s*([\d\.]+(?:em|px|%|pt))', style, re.I)

                # 4. Run the Nuclear Kill List
                clean_style = nuclear_pattern.sub('', style)
                clean_style = absolute_font_pattern.sub('', clean_style)

                # 5. Re-inject Preserved Properties
                injections = []

                if alignment_match:
                    align_val = alignment_match.group(1).lower()
                    injections.append(f"text-align: {align_val} !important")
                    if align_val in ['center', 'right']:
                        injections.append("text-indent: 0 !important")

                if indent_match:
                    # We keep the indentation but cap it at 20% to prevent it going off-screen on small devices
                    val = indent_match.group(2)
                    if '%' in val and float(val.strip('%')) > 20: val = "10%"
                    injections.append(f"margin-left: {val}")

                if top_margin_match:
                    # If there's a top margin, we ensure it's honored but safe
                    injections.append(f"margin-top: 1.5em")

                # Rebuild style
                clean_style = clean_style.strip().strip(';')
                if injections:
                    clean_style += "; " + "; ".join(injections)

                if clean_style:
                    tag['style'] = clean_style
                else:
                    del tag['style']

        # --- PHASE 2: CSS PARSING (Preserved from your original) ---
        if css_text:
            css_clean = re.sub(r'\s+', ' ', css_text).lower()
            css_clean = re.sub(r'/\*.*?\*/', '', css_clean)
            blocks = css_clean.split('}')
            for block in blocks:
                if '{' not in block: continue
                parts = block.split('{')
                if len(parts) < 2: continue
                selector_str = parts[0].strip()
                prop_str = parts[1].strip()
                found_classes = re.findall(r'\.([a-z0-9_-]+)', selector_str)
                if not found_classes: continue
                if re.search(r'letter-spacing\s*:\s*(?!0|normal)', prop_str):
                    for cls in found_classes: spacing_classes.add(cls)

        # --- PHASE 3: APPLY SPACING (Preserved) ---
        target_tags = ['p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'span', 'blockquote', 'li']
        for tag in soup.find_all(target_tags):
            classes = tag.get('class', [])
            if isinstance(classes, str): classes = [classes]
            should_space = tag.get('data-letter-spacing') == 'true'
            if not should_space:
                for cls in classes:
                    if cls.lower() in spacing_classes:
                        should_space = True
                        break
            if should_space:
                THIN_SPACE = '\u2009'
                SOFT_HYPHEN = '\u00AD'
                for text_node in tag.find_all(string=True):
                    if not text_node.strip() or text_node.parent.name == 'rt': continue
                    txt = str(text_node)
                    if THIN_SPACE in txt: continue
                    new_chars = []
                    for i, char in enumerate(txt):
                        new_chars.append(char)
                        is_last = (i == len(txt) - 1)
                        next_char = txt[i + 1] if not is_last else None
                        if char != SOFT_HYPHEN and next_char != SOFT_HYPHEN and not is_last:
                            new_chars.append(THIN_SPACE)
                    text_node.replace_with(NavigableString("".join(new_chars)))

        return soup





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
        requested_start = self.layout_settings.get("toc_insert_page", 1) - 1

        # Calculate available book pages (excluding TOC)
        book_pages_count = len(self.page_map)

        # Clamp insertion point so it doesn't exceed book length
        toc_start = max(0, min(requested_start, book_pages_count))

        if num_toc > 0 and toc_start <= global_page_index < (toc_start + num_toc):
            is_toc = True
            toc_local_idx = global_page_index - toc_start
            img_content = self.toc_pages_images[toc_local_idx].copy().convert("L")
            page = None
            sx, sy = 1.0, 1.0
        else:
            is_toc = False
            if global_page_index < toc_start:
                book_idx = global_page_index
            else:
                book_idx = global_page_index - num_toc

            doc_idx, page_idx = self.page_map[book_idx]
            doc, _ = self.fitz_docs[doc_idx]
            page = doc[page_idx]

            sx = self.screen_width / page.rect.width
            sy = content_height / page.rect.height

            mat = fitz.Matrix(sx, sy)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img_content = Image.frombytes("RGB", [pix.width, pix.height], pix.samples).convert("L")

        # Assemble full page (Grayscale)
        full_page = Image.new("L", (self.screen_width, self.screen_height), 255)
        paste_x = (self.screen_width - img_content.width) // 2
        paste_y = 0 if is_toc else header_padding
        full_page.paste(img_content, (paste_x, paste_y))

        # 3. Enhance Grayscale (Global Contrast/White Clip)
        if not is_toc:
            if contrast != 1.0:
                full_page = ImageEnhance.Contrast(full_page).enhance(contrast)
            if white_clip < 255:
                full_page = full_page.point(lambda p: 255 if p > white_clip else p)

            # --- NEW: THE ANTI-BURN IMAGE CAPTURE ---
            # We grab the images NOW while they are still grayscale
            captured_images = []
            image_info = page.get_image_info()
            for info in image_info:
                bbox = info['bbox']
                # Scale coordinates to match our canvas
                ix0, iy0, ix1, iy1 = [int(bbox[0] * sx), int(bbox[1] * sy), int(bbox[2] * sx), int(bbox[3] * sy)]

                # Define the crop area (accounting for page centering/padding)
                crop_box = (ix0 + paste_x, iy0 + paste_y, ix1 + paste_x, iy1 + paste_y)

                # Verify crop is valid
                if ix1 > ix0 and iy1 > iy0:
                    # 1. Crop the clean grayscale image
                    img_crop = full_page.crop(crop_box)
                    # 2. Lighten it slightly so e-ink doesn't get too muddy
                    img_crop = ImageEnhance.Brightness(img_crop).enhance(1.1)
                    # 3. Dither it to 1-bit immediately
                    img_crop = img_crop.convert("1", dither=Image.Dither.FLOYDSTEINBERG).convert("L")
                    # 4. Store for later pasting
                    captured_images.append((img_crop, (ix0 + paste_x, iy0 + paste_y)))

            # 4. APPLY GLOBAL TEXT RENDERING (Threshold/Bit-Depth)
            # This turns the whole page (including original images) into high-contrast blobs
            render_mode = self.layout_settings.get("render_mode", "Threshold")
            bit_depth = self.layout_settings.get("bit_depth", "1-bit (XTG)")
            text_blur = self.layout_settings.get("text_blur", 1.0)

            if render_mode == "Threshold" and text_blur > 0:
                full_page = ImageEnhance.Sharpness(full_page).enhance(1.0 + (text_blur * 0.5))

            if "2-bit" in bit_depth:
                # 2-bit Quantization logic
                thresh = self.layout_settings.get("text_threshold", 130)
                brightness_shift = 128 - thresh
                full_page = full_page.point(lambda p: max(0, min(255, p + brightness_shift)))
                full_page = full_page.point(lambda p: (p // 64) * 85).convert("L")
            else:
                # Standard 1-bit logic
                thresh = self.layout_settings.get("text_threshold", 130)
                full_page = full_page.point(lambda p: 255 if p > thresh else 0).convert("L")

            # --- 5. RE-PASTE DITHERED IMAGES ---
            # This overwrites the "blobs" with our preserved dithered versions
            for img_crop, pos in captured_images:
                full_page.paste(img_crop, pos)

        # 6. UI Overlay & Final Conversion
        img_final = full_page.convert("RGB")
        draw = ImageDraw.Draw(img_final)

        if not is_toc:
            # Clear Header/Footer areas for UI
            if header_padding > 0:
                draw.rectangle([0, 0, self.screen_width, header_padding], fill=(255, 255, 255))
            if footer_padding > 0:
                draw.rectangle([0, self.screen_height - footer_padding, self.screen_width, self.screen_height],
                               fill=(255, 255, 255))

            self._draw_header(draw, global_page_index)
            self._draw_footer(draw, global_page_index)

            # Draw Spectra Annotations
            page_annotations = self.page_annotation_data.get((doc_idx, page_idx), {})
            if page_annotations and spectra_enabled:
                # ... (rest of your annotation drawing logic remains the same) ...
                annot_font_size = max(9, int(self.font_size * 0.65))
                annot_font = self._get_ui_font(annot_font_size)
                for x0, y0, x1, y1, text, block, line, word_idx in page.get_text("words"):
                    coord_key = f"{int(x0)}_{int(y0)}"
                    if coord_key in page_annotations:
                        defi = page_annotations[coord_key]
                        px_x, px_y = x0 * sx, y0 * sy
                        px_w = (x1 - x0) * sx
                        text_len = annot_font.getlength(defi)
                        draw_x = max(2, min(paste_x + px_x + (px_w - text_len) / 2, self.screen_width - text_len - 2))
                        draw_y = max(header_padding, paste_y + px_y - annot_font_size + 2)
                        draw.text((draw_x, draw_y), defi, font=annot_font, fill=(0, 0, 0))

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

                if render_mode == "Dither":
                    # 1. Convert to RGB to ensure PIL matches colors correctly
                    img_input = img_gray.convert("RGB")

                    pal = Image.new('P', (1, 1))

                    # --- PALETTE DEFINITION ---
                    # We need: White(255) -> Index 0
                    # We need: Black(0)   -> Index 3

                    # If your image is NEGATIVE, it means White is mapping to 3.
                    # Use THIS palette to fix it:
                    pal.putpalette([
                                       255, 255, 255,  # Index 0: White      (Maps to hardware '00')
                                       85, 85, 85,  # Index 1: Dark Grey  (Maps to hardware '01')
                                       170, 170, 170,  # Index 2: Light Grey (Maps to hardware '10')
                                       0, 0, 0,  # Index 3: Black      (Maps to hardware '11')
                                   ] + [0] * 756)


                    # Force PIL to map pixels to the nearest color in our strict palette
                    quant = img_input.quantize(palette=pal, dither=Image.Dither.FLOYDSTEINBERG)

                else:
                    # Threshold Logic (Confirmed Working)
                    # Maps White(255) -> 0
                    # Maps Black(0)   -> 3
                    quant = img_gray.point(lambda p: 3 - (p // 64))

                pix = quant.load()

                bytes_per_col = (h + 7) // 8
                p1, p2 = bytearray(bytes_per_col * w), bytearray(bytes_per_col * w)

                # Scan: Right to Left -> Top to Bottom
                for x_idx, x in enumerate(range(w - 1, -1, -1)):
                    for y in range(h):
                        val = pix[x, y] & 0x03

                        # --- OPTIONAL SAFETY CHECK ---
                        # If you still get negative middle greys, swap 1 and 2 here manually:
                        # if val == 1: val = 2
                        # elif val == 2: val = 1
                        # -----------------------------

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
        self.selected_chapter_indices = None
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

        # In ModernApp._build_sidebar, inside the 'c_lay' (Layout) card:
        r_toc_pos = ctk.CTkFrame(c_lay.content, fg_color="transparent")
        r_toc_pos.pack(fill="x", pady=2)
        ctk.CTkLabel(r_toc_pos, text="TOC Insert Page:", font=("Arial", 12), width=130, anchor="w").pack(side="left")
        self.var_toc_page = ctk.StringVar(value=str(self.startup_settings.get("toc_insert_page", 1)))
        # We use a trace to trigger the auto-reload when the user types
        self.var_toc_page.trace_add("write", lambda *args: self.schedule_update())
        ctk.CTkEntry(r_toc_pos, textvariable=self.var_toc_page, width=50, height=22).pack(side="right")


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
        # Simply pass self.selected_chapter_indices (which is None on first run)
        ChapterSelectionDialog(
            self,
            self.processor.raw_chapters,
            self.selected_chapter_indices,
            self._on_chapters_selected
        )


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

        try:
            toc_insert = int(self.var_toc_page.get())
        except (ValueError, AttributeError):
            toc_insert = 1

        return {
            "toc_insert_page": toc_insert,
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

        if hasattr(self, 'var_toc_page'):
            self.var_toc_page.set(str(s.get("toc_insert_page", 1)))

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