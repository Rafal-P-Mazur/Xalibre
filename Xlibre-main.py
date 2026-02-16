import customtkinter as ctk
from tkinterdnd2 import DND_FILES, TkinterDnD

try:
    # Try to import DnDWrapper from the internal module (newer versions)
    from tkinterdnd2.TkinterDnD import DnDWrapper
except ImportError:
    # Fallback for older versions
    from tkinterdnd2 import DnDWrapper

from tkinter import filedialog, messagebox, simpledialog
import os
import io
import shutil
import json
import threading
from PIL import Image, ImageEnhance, ImageOps
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup
import warnings
import re
import requests
import time
import glob
from datetime import datetime, timedelta
import struct
import sys
import pyphen
try:
    import pyphen.dictionaries
    import pymupdf._extra
    import fitz
except ImportError:
    pass

# --- IMPORTS ---
import database
import converter  # The provided converter.py file

warnings.filterwarnings("ignore")

# --- INITIALIZE AOA DATABASE ON STARTUP ---
converter.load_aoa_database()

# 1. Define standard config location (Hidden User Folder)
if os.name == 'nt':  # Windows
    APP_ROOT = os.path.join(os.getenv('APPDATA'), "Xlibre")
else:  # Mac/Linux
    APP_ROOT = os.path.join(os.path.expanduser("~"), ".config", "xlibre")

# Ensure the folder exists
os.makedirs(APP_ROOT, exist_ok=True)

# 2. Set the config file path
APP_SETTINGS_FILE = os.path.join(APP_ROOT, "Xlibre_config.json")

def load_app_config():
    default_config = {
        "base_dir": os.path.join(os.path.expanduser("~"), "Xlibre"),
        "device_ip": "192.168.0.202",
        "default_sort": "Date Added",
        "default_status": "All Statuses",
        "view_mode": "grid",
        "update_epub_cover": False
    }
    if os.path.exists(APP_SETTINGS_FILE):
        try:
            with open(APP_SETTINGS_FILE, "r") as f:
                config = default_config.copy()
                config.update(json.load(f))
                return config
        except:
            return default_config
    return default_config


def update_global_paths(new_base):
    """Sets global folder constants and ensures they exist."""
    global BASE_DIR, LIBRARY_DIR, EXPORT_DIR, PRESETS_DIR, FONTS_DIR
    BASE_DIR = new_base
    LIBRARY_DIR = os.path.join(BASE_DIR, "Library")
    EXPORT_DIR = os.path.join(BASE_DIR, "Exports")
    PRESETS_DIR = os.path.join(BASE_DIR, "Presets")
    FONTS_DIR = os.path.join(BASE_DIR, "Fonts")

    # Create all folders if they don't exist
    for d in [BASE_DIR, LIBRARY_DIR, EXPORT_DIR, PRESETS_DIR, FONTS_DIR]:
        os.makedirs(d, exist_ok=True)

    # Re-link converter constants to the new paths
    converter.PRESETS_DIR = PRESETS_DIR
    converter.SETTINGS_FILE = os.path.join(BASE_DIR, "default_settings.json")

# Load config immediately to set paths
config = load_app_config()
BASE_DIR = config["base_dir"]

LIBRARY_DIR = os.path.join(BASE_DIR, "Library")
EXPORT_DIR = os.path.join(BASE_DIR, "Exports")
PRESETS_DIR = os.path.join(BASE_DIR, "Presets")
FONTS_DIR = os.path.join(BASE_DIR, "Fonts")


# --- NEW: BUNDLED FONTS LOGIC ---
def get_resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


BUNDLED_FONTS_DIR = get_resource_path("Fonts")  # Internal/Bundled Fonts


# --- NEW: FONT AGGREGATOR ---
def get_combined_fonts():
    """Scans both Bundled and User font directories. User fonts override Bundled."""
    font_map = {}

    def scan_folder(directory):
        if not os.path.exists(directory): return
        # Walk through directory
        for root, dirs, files in os.walk(directory):
            for file in files:
                if file.lower().endswith((".ttf", ".otf")):
                    full_path = os.path.join(root, file)

                    # Determine Font Name (Folder Name if inside a folder, else Filename)
                    rel_path = os.path.relpath(root, directory)
                    if rel_path == ".":
                        # File is directly in Fonts root -> Use Filename
                        name = os.path.splitext(file)[0]
                    else:
                        # File is in a subfolder -> Use Subfolder Name (Family Name)
                        # We use the top-level folder name inside the fonts dir
                        name = rel_path.split(os.sep)[0]

                    # Add to map (Latest scan overwrites previous, so scan User last)
                    font_map[name] = full_path

    # 1. Scan Bundled First
    scan_folder(BUNDLED_FONTS_DIR)

    # 2. Scan User Second (Overrides Bundled)
    scan_folder(FONTS_DIR)

    return font_map

# Create all folders if they don't exist
for d in [BASE_DIR, LIBRARY_DIR, EXPORT_DIR, PRESETS_DIR, FONTS_DIR]:
    os.makedirs(d, exist_ok=True)

# Override converter defaults to point to Xlibre folders
converter.PRESETS_DIR = PRESETS_DIR
converter.SETTINGS_FILE = os.path.join(BASE_DIR, "default_settings.json")


def inject_cover_into_epub(epub_path, cover_bytes):
    """
    Safely injects a Full-Screen Cover.
    Strategy: Finds existing cover files and SWAPS their content instead of deleting them.
    This prevents file corruption while updating the image.
    """
    if not epub_path or not os.path.exists(epub_path):
        return

    try:
        # --- 1. PREPARE IMAGE (Resize & Convert) ---
        img = Image.open(io.BytesIO(cover_bytes))
        width, height = img.size

        # Resize if massive (optional, keeps file size sane)
        if width > 1600 or height > 2400:
            aspect = width / height
            height = 2000
            width = int(height * aspect)
            img = img.resize((width, height), Image.Resampling.LANCZOS)

        out_buffer = io.BytesIO()
        img = img.convert("RGB")
        img.save(out_buffer, format="JPEG", quality=90)
        final_cover_bytes = out_buffer.getvalue()

        # --- 2. OPEN BOOK ---
        book = epub.read_epub(epub_path)

        # Variables to track what we find
        cover_image_item = None
        cover_image_filename = "cover.jpg"  # Default name if creating new

        # --- 3. HANDLE IMAGE FILE (Swap or Create) ---

        # Check OPF Metadata for existing cover ID
        # Metadata format is usually list of tuples
        try:
            cover_meta = book.get_metadata('OPF', 'cover')
            if cover_meta:
                c_id = cover_meta[0][1]
                cover_image_item = book.get_item_with_id(c_id)
        except:
            pass

        if cover_image_item:
            print(f"Found existing cover image: {cover_image_item.get_name()}. Overwriting.")
            # SAFE SWAP: Just update the bytes. IDs and references stay untouched.
            cover_image_item.content = final_cover_bytes
            cover_image_filename = cover_image_item.get_name()
        else:
            print("No existing cover found. Creating new image.")
            # Use set_cover to safely handle metadata creation
            book.set_cover("cover.jpg", final_cover_bytes, create_page=False)
            cover_image_filename = "cover.jpg"

        # --- 4. HANDLE HTML PAGE (Swap or Create) ---

        # SVG Wrapper Content (Forces Full Screen)
        cover_content = f'''<?xml version="1.0" encoding="UTF-8"?>
        <html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xmlns:xlink="http://www.w3.org/1999/xlink">
        <head>
            <title>Cover</title>
            <style type="text/css">
                body {{ margin: 0; padding: 0; text-align: center; background-color: #FFFFFF; height: 100%; width: 100%; }}
                svg {{ padding: 0; margin: 0; }}
            </style>
        </head>
        <body>
            <svg xmlns="http://www.w3.org/2000/svg" version="1.1" 
                 width="100%" height="100%" viewBox="0 0 {width} {height}" 
                 preserveAspectRatio="xMidYMid meet">
                <image width="{width}" height="{height}" xlink:href="{cover_image_filename}" />
            </svg>
        </body>
        </html>'''

        # Try to find existing cover HTML page via Guide
        cover_page_item = None
        if book.guide:
            for link in book.guide:
                if link.get('type', '').lower() == 'cover':
                    cover_page_item = book.get_item_with_href(link.get('href'))
                    break

        if cover_page_item:
            print("Found existing cover page. Overwriting HTML.")
            # SAFE SWAP: Update HTML text. Spine/Guide references remain valid.
            cover_page_item.content = cover_content.encode("utf-8")
        else:
            print("Creating new cover page.")
            # Create new page
            cover_page_item = epub.EpubHtml(
                title="Cover",
                file_name="cover_wrapper.xhtml",
                lang="en",
                uid="cover_page_wrapper"
            )
            cover_page_item.content = cover_content.encode("utf-8")
            book.add_item(cover_page_item)

            # Insert at VERY START of Spine
            if book.spine:
                book.spine.insert(0, cover_page_item)
            else:
                book.spine = [cover_page_item]

            # Add to Guide
            book.guide.insert(0, {'type': 'cover', 'title': 'Cover', 'href': 'cover_wrapper.xhtml'})

        # --- 5. SAVE ---
        epub.write_epub(epub_path, book)
        print(f"Successfully updated cover in: {epub_path}")

    except Exception as e:
        print(f"Failed to update internal EPUB cover: {e}")

class SettingsDialog(ctk.CTkToplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent
        self.title("Xlibre Manager Settings")
        self.geometry("450x550")
        self.transient(parent)
        self.grab_set()

        self.config = load_app_config()

        ctk.CTkLabel(self, text="Manager Preferences", font=("Arial", 20, "bold")).pack(pady=20)

        # Library Path
        ctk.CTkLabel(self, text="Library Location:", anchor="w").pack(fill="x", padx=30)
        path_frame = ctk.CTkFrame(self, fg_color="transparent")
        path_frame.pack(fill="x", padx=30, pady=(0, 15))
        self.entry_path = ctk.CTkEntry(path_frame)
        self.entry_path.insert(0, self.config["base_dir"])
        self.entry_path.pack(side="left", fill="x", expand=True, padx=(0, 5))
        ctk.CTkButton(path_frame, text="📁", width=30, command=self.browse_path).pack(side="right")

        # Device IP
        ctk.CTkLabel(self, text="Default Device IP (Reader):", anchor="w").pack(fill="x", padx=30)
        self.entry_ip = ctk.CTkEntry(self)
        self.entry_ip.insert(0, self.config["device_ip"])
        self.entry_ip.pack(fill="x", padx=30, pady=(0, 15))

        # Default Sorting
        ctk.CTkLabel(self, text="Startup Sorting:", anchor="w").pack(fill="x", padx=30)
        self.sort_opt = ctk.CTkOptionMenu(self, values=["Date Added", "Title", "Author", "Genre"])
        self.sort_opt.set(self.config["default_sort"])
        self.sort_opt.pack(fill="x", padx=30, pady=(0, 15))

        # View Mode
        ctk.CTkLabel(self, text="Startup View:", anchor="w").pack(fill="x", padx=30)
        self.view_opt = ctk.CTkOptionMenu(self, values=["grid", "list"])
        self.view_opt.set(self.config["view_mode"])
        self.view_opt.pack(fill="x", padx=30, pady=(0, 15))

        self.var_update_epub = ctk.BooleanVar(value=self.config.get("update_epub_cover", False))
        ctk.CTkSwitch(self, text="Embed custom cover into original .EPUB file", variable=self.var_update_epub).pack(
            fill="x", padx=30, pady=(10, 15))

        ctk.CTkButton(self, text="Save and Restart App", fg_color="#E67E22", hover_color="#D35400",
                      command=self.save_and_restart).pack(pady=30)

        ctk.CTkLabel(self, text="*App will restart to apply all changes",
                     font=("Arial", 10), text_color="gray").pack()

    def browse_path(self):
        new_path = filedialog.askdirectory()
        if new_path:
            self.entry_path.delete(0, 'end')
            self.entry_path.insert(0, new_path)

    def save_and_restart(self):


        # 1. PREPARE PATHS
        new_base = self.entry_path.get()
        old_base = self.config["base_dir"]

        # 2. HANDLE MIGRATION (If needed)
        if os.path.normpath(new_base) != os.path.normpath(old_base):
            if messagebox.askyesno("Migrate Library", f"Move library to:\n{new_base}?"):
                try:
                    if hasattr(self.parent, 'db'): self.parent.db.close()
                    if not os.path.exists(new_base):
                        shutil.move(old_base, new_base)
                    else:
                        for item in os.listdir(old_base):
                            shutil.move(os.path.join(old_base, item), os.path.join(new_base, item))
                        os.rmdir(old_base)
                except Exception as e:
                    messagebox.showerror("Migration Error", str(e))
                    return

        # 3. SAVE SETTINGS
        self._final_save(new_base)

        # 4. NOTIFY USER AND EXIT
        try:
            # Close the database properly before exiting
            if hasattr(self.parent, 'db'):

                messagebox.showinfo(
                    "Settings Saved",
                    "Your settings have been updated.\n\nPlease manually restart Xlibre to apply all changes."
                )

                # Close the application completely
                self.parent.destroy()
                os._exit(0)

        except Exception as e:
            # Fallback in case the notification itself fails
            self.destroy()

    def _final_save(self, base_path):
        new_config = {
            "base_dir": base_path,
            "device_ip": self.entry_ip.get(),
            "default_sort": self.sort_opt.get(),
            "view_mode": self.view_opt.get(),
            "default_status": self.config.get("default_status", "All Statuses"),
            "update_epub_cover": self.var_update_epub.get() # <-- SAVE THE VALUE
        }
        with open(APP_SETTINGS_FILE, "w") as f:
            json.dump(new_config, f, indent=4)
# --- UNIFIED METADATA FETCHER ---
class UnifiedMetadataFetcher:
    @staticmethod
    def search_and_merge(title, author):
        final = {"description": "", "publisher": "", "publishedDate": "", "categories": "", "cover_blob": None,
                 "source": ""}

        sources = [
            ("Google Books", UnifiedMetadataFetcher._search_google),
            ("Apple Books", UnifiedMetadataFetcher._search_apple_books),
            ("Open Library", UnifiedMetadataFetcher._search_open_library)
        ]

        used_sources = []

        for source_name, search_func in sources:
            if all([final["description"], final["publisher"], final["publishedDate"], final["categories"],
                    final["cover_blob"]]):
                break

            res = search_func(title, author)
            if res:
                added_something = False
                for key in ["description", "publisher", "publishedDate", "categories"]:
                    if not final[key] and res.get(key):
                        final[key] = res[key]
                        added_something = True
                if not final["cover_blob"] and res.get("cover_blob"):
                    final["cover_blob"] = res.get("cover_blob")
                    added_something = True
                if added_something:
                    used_sources.append(source_name)

        final["source"] = " + ".join(used_sources) if used_sources else "None"
        return final

    @staticmethod
    def _clean_query(text):
        if not text: return ""
        return re.sub(r'[^\w\s]', '', text).replace(" ", "+")

    @staticmethod
    def _search_apple_books(title, author):
        try:
            query = UnifiedMetadataFetcher._clean_query(f"{title} {author}")
            url = f"https://itunes.apple.com/search?term={query}&media=ebook&limit=1"
            resp = requests.get(url, timeout=5)
            if resp.status_code != 200: return None
            data = resp.json()
            if not data.get("results"): return None
            info = data["results"][0]
            cover_blob = None
            img_url = info.get("artworkUrl100", "").replace("100x100bb", "600x600bb")
            if img_url:
                try:
                    c = requests.get(img_url, timeout=5)
                    if c.status_code == 200: cover_blob = c.content
                except:
                    pass
            desc = info.get("description", "")
            if desc: desc = BeautifulSoup(desc, "html.parser").get_text(separator="\n").strip()
            return {
                "description": desc,
                "publisher": info.get("sellerName", ""),
                "publishedDate": info.get("releaseDate", "")[:4] if info.get("releaseDate") else "",
                "categories": ", ".join(info.get("genres", [])),
                "cover_blob": cover_blob
            }
        except:
            return None

    @staticmethod
    def _search_google(title, author):
        try:
            url = f"https://www.googleapis.com/books/v1/volumes?q=intitle:{UnifiedMetadataFetcher._clean_query(title)}+inauthor:{UnifiedMetadataFetcher._clean_query(author)}&maxResults=1"
            resp = requests.get(url, timeout=5)
            if resp.status_code != 200: return None
            data = resp.json()
            if "items" not in data: return None
            info = data["items"][0]["volumeInfo"]
            cover_blob = None
            img_url = (info.get("imageLinks", {}).get("thumbnail") or "").replace("http://", "https://")
            img_url = img_url.replace("zoom=1", "zoom=3")
            if img_url:
                try:
                    c = requests.get(img_url, timeout=5)
                    if c.status_code == 200: cover_blob = c.content
                except:
                    pass
            return {
                "description": info.get("description", ""),
                "publisher": info.get("publisher", ""),
                "publishedDate": info.get("publishedDate", "")[:4],
                "categories": ", ".join(info.get("categories", [])),
                "cover_blob": cover_blob
            }
        except:
            return None

    @staticmethod
    def _search_open_library(title, author):
        try:
            url = f"https://openlibrary.org/search.json?title={UnifiedMetadataFetcher._clean_query(title)}&author={UnifiedMetadataFetcher._clean_query(author)}&limit=1"
            data = requests.get(url, timeout=5).json()
            if not data.get("docs"): return None
            doc = data["docs"][0]
            desc = ""
            if doc.get("key"):
                try:
                    w_data = requests.get(f"https://openlibrary.org{doc['key']}.json", timeout=5).json()
                    raw = w_data.get("description", "")
                    desc = raw.get("value", "") if isinstance(raw, dict) else str(raw)
                except:
                    pass
            cover_blob = None
            if doc.get("cover_i"):
                try:
                    c = requests.get(f"https://covers.openlibrary.org/b/id/{doc['cover_i']}-L.jpg", timeout=5)
                    if c.status_code == 200: cover_blob = c.content
                except:
                    pass
            return {
                "description": desc,
                "publisher": doc.get("publisher", [""])[0] if doc.get("publisher") else "",
                "publishedDate": str(doc.get("first_publish_year", "")),
                "categories": ", ".join(doc.get("subject", [])[:3]) if doc.get("subject") else "",
                "cover_blob": cover_blob
            }
        except:
            return None


# --- INTEGRATED CONVERTER WINDOW (Based on converter.ModernApp) ---
# This class replicates the UI logic of ModernApp but inherits CTkToplevel
# to function as a child window of Xlibre, and is wired to specific book data.
class IntegratedEditor(ctk.CTkToplevel):
    def __init__(self, book_data, db_instance, refresh_callback, parent):
        super().__init__(parent)
        self.book_data = book_data  # (id, title, author, path_epub, path_xtc, ...)
        self.db = db_instance
        self.refresh_callback = refresh_callback
        self.processor = converter.EpubProcessor()

        # Window Setup
        self.title(f"Convert: {book_data[1]}")
        self.geometry("1400x950")
        self.transient(parent)
        self.grab_set()
        self.configure(fg_color="#111111")

        # Internal State
        self.current_page_index = 0
        self.debounce_timer = None
        self.is_processing = False
        self.pending_rerun = False
        self.selected_chapter_indices = None

        # Settings
        self.startup_settings = converter.FACTORY_DEFAULTS.copy()
        if os.path.exists(converter.SETTINGS_FILE):
            try:
                with open(converter.SETTINGS_FILE, "r") as f:
                    self.startup_settings.update(json.load(f))
            except:
                pass

        # UI Construction (Reusing ModernApp logic)
        self._build_toolbar()
        self.main_container = ctk.CTkFrame(self, fg_color="#111111")
        self.main_container.pack(fill="both", expand=True)
        self._build_sidebar()
        self._build_preview_area()

        # Load Presets
        self.refresh_presets_list()

        # Apply Defaults
        self.apply_settings_dict(self.startup_settings)

        # START LOADING
        self.load_book()

    def import_custom_font(self):
        """Import an entire font family folder to Xlibre/Fonts."""
        src_folder = filedialog.askdirectory(title="Select Font Family Folder")
        if not src_folder: return

        family_name = os.path.basename(src_folder)
        dest_folder = os.path.join(FONTS_DIR, family_name)

        if os.path.exists(dest_folder):
            if not messagebox.askyesno("Overwrite", f"Font family '{family_name}' exists. Overwrite?"):
                return
            shutil.rmtree(dest_folder)

        try:
            shutil.copytree(src_folder, dest_folder)
            messagebox.showinfo("Success", f"Imported: {family_name}")
            self.refresh_fonts_list()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def refresh_fonts_list(self):
        """Re-scan the Fonts directory and update the dropdown menu."""
        # --- CHANGE: Use combined fonts function ---
        self.available_fonts = get_combined_fonts()
        self.font_options = ["Default (System)"] + sorted(list(self.available_fonts.keys()))

        # Rebuild the internal map
        self.font_map = self.available_fonts.copy()
        self.font_map["Default (System)"] = "DEFAULT"

        # Update the UI Dropdown
        self.font_dropdown.configure(values=self.font_options)
    # --- UI WRAPPERS (Mirrors ModernApp but points to self) ---
    # We include the helper methods from converter.ModernApp here to ensure full functionality

    def _create_icon_btn(self, parent, text, hover_col, cmd, state="normal"):
        b = ctk.CTkButton(parent, text=text, command=cmd, state=state, width=110, height=35, corner_radius=8,
                          font=("Arial", 12, "bold"), fg_color="#2B2B2B", hover_color=hover_col)
        b.pack(side="left", padx=5)
        return b

    def _create_divider(self, parent):
        ctk.CTkFrame(parent, width=2, height=30, fg_color="#333").pack(side="left", padx=10)

    def _create_divider_horizontal(self, parent):
        ctk.CTkFrame(parent, height=2, fg_color="#333").pack(fill="x", pady=10)

    def _create_slider(self, parent, label_attr, text, slider_attr, min_v, max_v, is_float=False, width=None,
                       trigger_auto_update=True):
        f = ctk.CTkFrame(parent, fg_color="transparent")
        f.pack(fill="x", pady=2)
        setting_key = slider_attr.replace('slider_', '')
        default_val = self.startup_settings.get(setting_key, min_v)

        def get_label_text(val):
            v_num = float(val) if is_float else int(val)
            fmt = f"{v_num:.1f}" if is_float else f"{v_num}"
            return f"{text}: {fmt}"

        lbl = ctk.CTkLabel(f, text=get_label_text(default_val), font=("Arial", 12), anchor="w", width=130)
        lbl.pack(side="left")
        setattr(self, label_attr, lbl)

        def on_slide(val):
            lbl.configure(text=get_label_text(val))
            if trigger_auto_update: self.schedule_update()

        sld = ctk.CTkSlider(f, from_=min_v, to=max_v, command=on_slide, height=16)
        if width: sld.configure(width=width)
        sld.set(default_val)
        sld.pack(side="left", fill="x", expand=True, padx=(5, 0))
        setattr(self, f"fmt_{slider_attr}", get_label_text)
        setattr(self, slider_attr, sld)
        return sld

    def _build_toolbar(self):
        tb = ctk.CTkFrame(self, height=70, fg_color="#1a1a1a", corner_radius=0)
        tb.pack(fill="x", side="top")

        # Logo/Title Area
        logo_f = ctk.CTkFrame(tb, fg_color="transparent")
        logo_f.pack(side="left", padx=(20, 30))
        ctk.CTkLabel(logo_f, text="EDITOR", font=("Arial", 20, "bold"), text_color="#3498DB").pack(anchor="w")
        self.lbl_file = ctk.CTkLabel(logo_f, text=self.book_data[1][:30], font=("Arial", 12), text_color="gray",
                                     anchor="w")
        self.lbl_file.pack(anchor="w")

        self._create_divider(tb)
        self.btn_chapters = self._create_icon_btn(tb, "☰ Edit TOC", "#E67E22", self.open_chapter_dialog, "disabled")

        self.btn_font_import = self._create_icon_btn(tb, "Aa Import Font", "#34495E", self.import_custom_font)
        # Save XTC -> Updates DB
        self.btn_export = self._create_icon_btn(tb, "💾 Save to Library", "#2ECC71", self.save_to_library, "disabled")
        self.btn_cover = self._create_icon_btn(tb, "🖼 Export Cover", "#8E44AD", self.open_cover_export, "disabled")

        right_f = ctk.CTkFrame(tb, fg_color="transparent")
        right_f.pack(side="right", padx=20, fill="y")
        self.progress_label = ctk.CTkLabel(right_f, text="Loading...", font=("Arial", 12), text_color="gray",
                                           anchor="e")
        self.progress_label.pack(side="top", pady=(15, 0), anchor="e")
        self.progress_bar = ctk.CTkProgressBar(right_f, width=200, height=8, progress_color="#3498DB")
        self.progress_bar.set(0)
        self.progress_bar.pack(side="bottom", pady=(0, 15))

    def save_current_as_default(self):
        # We use the converter.SETTINGS_FILE which we re-mapped to Xlibre/default_settings.json
        # at the top of the script
        try:
            with open(converter.SETTINGS_FILE, "w") as f:
                json.dump(self.gather_current_ui_settings(), f, indent=4)
            messagebox.showinfo("Saved", "Current settings saved as default.")
        except Exception as e:
            messagebox.showerror("Error", f"Could not save defaults: {e}")

    def _build_sidebar(self):
        # This mirrors converter.ModernApp._build_sidebar exactly
        self.sidebar = ctk.CTkScrollableFrame(self.main_container, width=400, fg_color="transparent")
        self.sidebar.pack(side="left", fill="y", padx=10, pady=10)

        # PRESETS
        c_pre = converter.SettingsCard(self.sidebar, "PRESETS")
        row_pre = ctk.CTkFrame(c_pre.content, fg_color="transparent")
        row_pre.pack(fill="x")
        self.preset_var = ctk.StringVar(value="Select Preset...")
        self.preset_dropdown = ctk.CTkOptionMenu(row_pre, variable=self.preset_var, values=[],
                                                 command=self.load_selected_preset, fg_color="#444", height=22)
        self.preset_dropdown.pack(side="left", fill="x", expand=True, padx=(0, 5))
        ctk.CTkButton(row_pre, text="💾", width=30, height=22, command=self.save_new_preset, fg_color="#2ECC71").pack(
            side="left")

        # RENDER ENGINE
        c_ren = converter.SettingsCard(self.sidebar, "RENDER ENGINE")
        r_depth = ctk.CTkFrame(c_ren.content, fg_color="transparent")
        r_depth.pack(fill="x", pady=2)
        ctk.CTkLabel(r_depth, text="Target Format:", width=130, anchor="w").pack(side="left")
        self.bit_depth_var = ctk.StringVar(value="1-bit (XTG)")
        ctk.CTkOptionMenu(r_depth, values=["1-bit (XTG)", "2-bit (XTH)"], variable=self.bit_depth_var,
                          command=self.schedule_update, height=22).pack(side="right", fill="x", expand=True)

        ctk.CTkButton(c_pre.content, text="Set Current as Default", command=self.save_current_as_default,
                      fg_color="#444", hover_color="#3498DB", height=24).pack(fill="x", pady=(5, 0))

        r_mode = ctk.CTkFrame(c_ren.content, fg_color="transparent")
        r_mode.pack(fill="x", pady=2)
        ctk.CTkLabel(r_mode, text="Conversion:", width=130, anchor="w").pack(side="left")
        self.render_mode_var = ctk.StringVar(value="Threshold")
        ctk.CTkOptionMenu(r_mode, values=["Threshold", "Dither"], variable=self.render_mode_var,
                          command=self.toggle_render_controls, height=22).pack(side="right", fill="x", expand=True)

        self.frm_render_dynamic = ctk.CTkFrame(c_ren.content, fg_color="transparent")
        self.frm_render_dynamic.pack(fill="x", pady=5)
        self.frm_dither = ctk.CTkFrame(self.frm_render_dynamic, fg_color="transparent")
        self.sld_white = self._create_slider(self.frm_dither, "lbl_white_clip", "White Clip", "slider_white_clip", 150,
                                             255)
        self.sld_contrast = self._create_slider(self.frm_dither, "lbl_contrast", "Contrast", "slider_contrast", 0.5,
                                                2.0, is_float=True)
        self.frm_thresh = ctk.CTkFrame(self.frm_render_dynamic, fg_color="transparent")
        self.sld_thresh = self._create_slider(self.frm_thresh, "lbl_threshold", "Threshold", "slider_text_threshold",
                                              50, 200)
        self.sld_blur = self._create_slider(self.frm_thresh, "lbl_blur", "Definition", "slider_text_blur", 0.0, 3.0,
                                            is_float=True)

        # TYPOGRAPHY
        c_type = converter.SettingsCard(self.sidebar, "TYPOGRAPHY")
        r_font = ctk.CTkFrame(c_type.content, fg_color="transparent")
        r_font.pack(fill="x", pady=2)
        ctk.CTkLabel(r_font, text="Font Family:", width=130, anchor="w").pack(side="left")

        self.available_fonts = get_combined_fonts()
        self.font_options = ["Default (System)"] + sorted(list(self.available_fonts.keys()))
        self.font_map = self.available_fonts.copy()
        self.font_map["Default (System)"] = "DEFAULT"
        self.font_dropdown = ctk.CTkOptionMenu(r_font, values=self.font_options, command=self.on_font_change, height=22)
        self.font_dropdown.pack(side="right", fill="x", expand=True)
        r_align = ctk.CTkFrame(c_type.content, fg_color="transparent")
        r_align.pack(fill="x", pady=2)
        ctk.CTkLabel(r_align, text="Alignment:", width=130, anchor="w").pack(side="left")
        self.align_dropdown = ctk.CTkOptionMenu(r_align, values=["justify", "left"], command=self.schedule_update,
                                                height=22)
        self.align_dropdown.pack(side="right", fill="x", expand=True)

        self._create_slider(c_type.content, "lbl_size", "Font Size", "slider_font_size", 12, 48)
        self._create_slider(c_type.content, "lbl_weight", "Font Weight", "slider_font_weight", 100, 900)
        self._create_slider(c_type.content, "lbl_line", "Line Height", "slider_line_height", 1.0, 2.5, is_float=True)

        # LAYOUT
        c_lay = converter.SettingsCard(self.sidebar, "PAGE LAYOUT")
        r_ori = ctk.CTkFrame(c_lay.content, fg_color="transparent")
        r_ori.pack(fill="x", pady=2)
        ctk.CTkLabel(r_ori, text="Orientation:", width=130, anchor="w").pack(side="left")
        self.orientation_var = ctk.StringVar(value="Portrait")
        ctk.CTkOptionMenu(r_ori, values=["Portrait", "Landscape"], variable=self.orientation_var,
                          command=self.schedule_update, height=22).pack(side="right", fill="x", expand=True)
        r_tog = ctk.CTkFrame(c_lay.content, fg_color="transparent")
        r_tog.pack(fill="x", pady=5)
        self.var_toc = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(r_tog, text="Generate TOC", variable=self.var_toc, command=self.schedule_update).pack(
            side="left")
        self.var_footnotes = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(r_tog, text="Inline Footnotes", variable=self.var_footnotes, command=self.schedule_update).pack(
            side="right")
        self._create_slider(c_lay.content, "lbl_margin", "Side Margin", "slider_margin", 0, 100)
        self._create_slider(c_lay.content, "lbl_top_padding", "Top Padding", "slider_top_padding", 0, 150)
        self._create_slider(c_lay.content, "lbl_padding", "Bottom Padding", "slider_bottom_padding", 0, 150)

        # --- ADD THIS BLOCK FOR TOC POSITION ---
        r_toc_pos = ctk.CTkFrame(c_lay.content, fg_color="transparent")
        r_toc_pos.pack(fill="x", pady=2)
        ctk.CTkLabel(r_toc_pos, text="TOC Insert Page:", width=130, anchor="w").pack(side="left")
        self.var_toc_page = ctk.StringVar(value=str(self.startup_settings.get("toc_insert_page", 1)))
        self.var_toc_page.trace_add("write", lambda *args: self.schedule_update())
        ctk.CTkEntry(r_toc_pos, textvariable=self.var_toc_page, width=50, height=22).pack(side="right")

        # HEADER & FOOTER
        c_hf = converter.SettingsCard(self.sidebar, "HEADER & FOOTER")
        grid_hf = ctk.CTkFrame(c_hf.content, fg_color="transparent")
        grid_hf.pack(fill="x")

        # SPECTRA AI
        c_spectra = converter.SettingsCard(self.sidebar, "SPECTRA AI ANNOTATIONS")
        if not converter.HAS_WORDFREQ or not converter.HAS_OPENAI:
            ctk.CTkLabel(c_spectra.content, text="Missing libraries:\npip install wordfreq openai",
                         text_color="#C0392B").pack()
            self.var_spectra_enabled = ctk.BooleanVar(value=False)
        else:
            self.var_spectra_enabled = ctk.BooleanVar(value=False)
            ctk.CTkCheckBox(c_spectra.content, text="Show Definitions Overlay", variable=self.var_spectra_enabled,
                            command=self.schedule_update).pack(anchor="w", pady=5)
            ctk.CTkLabel(c_spectra.content, text="Target Language:", anchor="w").pack(anchor="w")
            self.var_spectra_lang = ctk.StringVar(value="English")
            ctk.CTkOptionMenu(c_spectra.content, variable=self.var_spectra_lang,
                              values=["English", "Spanish", "French", "German", "Italian", "Polish", "Portuguese",
                                      "Russian", "Chinese", "Japanese"]).pack(fill="x", pady=(0, 5))
            self.btn_spectra_gen = ctk.CTkButton(c_spectra.content, text="⚡ Analyze & Generate",
                                                 command=self.run_spectra_analysis, fg_color="#E67E22")
            self.btn_spectra_gen.pack(fill="x", pady=5)

            # Helper for Level Selection
            def set_level(choice):
                if choice == "A2 (Beginner)":
                    self.slider_spectra_threshold.set(5.5);
                    self.slider_spectra_aoa_threshold.set(4.0)
                elif choice == "B1 (Intermediate)":
                    self.slider_spectra_threshold.set(4.5);
                    self.slider_spectra_aoa_threshold.set(8.0)
                elif choice == "B2 (Upper Intermediate)":
                    self.slider_spectra_threshold.set(3.8);
                    self.slider_spectra_aoa_threshold.set(10.0)
                elif choice == "C1 (Advanced)":
                    self.slider_spectra_threshold.set(3.2);
                    self.slider_spectra_aoa_threshold.set(13.0)
                self.lbl_spectra_thresh.configure(text=f"Zipf Difficulty: {self.slider_spectra_threshold.get():.1f}")
                self.lbl_spectra_aoa.configure(text=f"Min. AoA: {self.slider_spectra_aoa_threshold.get():.1f}")
                self.schedule_update()

            ctk.CTkOptionMenu(c_spectra.content,
                              values=["A2 (Beginner)", "B1 (Intermediate)", "B2 (Upper Intermediate)", "C1 (Advanced)"],
                              command=set_level).pack(fill="x", pady=5)

            self._create_slider(c_spectra.content, "lbl_spectra_thresh", "Zipf Difficulty", "slider_spectra_threshold",
                                1.0, 7.0, is_float=True, trigger_auto_update=False)
            self._create_slider(c_spectra.content, "lbl_spectra_aoa", "Min. Age of Acquisition",
                                "slider_spectra_aoa_threshold", 0.0, 25.0, is_float=True, trigger_auto_update=False)

            ctk.CTkLabel(c_spectra.content, text="API Key:", anchor="w").pack(anchor="w")
            self.entry_spectra_key = ctk.CTkEntry(c_spectra.content, show="*");
            self.entry_spectra_key.pack(fill="x")
            ctk.CTkLabel(c_spectra.content, text="Base URL:", anchor="w").pack(anchor="w")
            self.entry_spectra_url = ctk.CTkEntry(c_spectra.content);
            self.entry_spectra_url.pack(fill="x")
            ctk.CTkLabel(c_spectra.content, text="Model:", anchor="w").pack(anchor="w")
            self.entry_spectra_model = ctk.CTkEntry(c_spectra.content);
            self.entry_spectra_model.pack(fill="x")

        # Header/Footer Positioning Helper
        def add_elem_row(txt, var_pos_name, var_ord_name):
            r = ctk.CTkFrame(grid_hf, fg_color="transparent")
            r.pack(fill="x", pady=2)
            ctk.CTkLabel(r, text=txt, width=110, anchor="w").pack(side="left")
            var_p = ctk.StringVar(value="Hidden");
            setattr(self, f"var_{var_pos_name}", var_p)
            ctk.CTkOptionMenu(r, variable=var_p, values=["Header", "Footer", "Hidden"], width=90, height=22,
                              command=self.schedule_update).pack(side="left", padx=5)
            var_o = ctk.StringVar(value="1");
            setattr(self, f"var_{var_ord_name}", var_o)
            ctk.CTkEntry(r, textvariable=var_o, width=35, height=22).pack(side="right")

        add_elem_row("Chapter Title", "pos_title", "order_title")
        add_elem_row("Page Number", "pos_pagenum", "order_pagenum")
        add_elem_row("Chapter Page", "pos_chap_page", "order_chap_page")
        add_elem_row("Reading %", "pos_percent", "order_percent")

        self._create_divider_horizontal(c_hf.content)
        row_progress = ctk.CTkFrame(c_hf.content, fg_color="transparent");
        row_progress.pack(fill="x", pady=2)
        ctk.CTkLabel(row_progress, text="Progress Bar:", width=130, anchor="w").pack(side="left")
        self.var_pos_progress = ctk.StringVar(value="Footer (Below Text)")
        ctk.CTkOptionMenu(row_progress, variable=self.var_pos_progress,
                          values=["Header (Above Text)", "Header (Below Text)", "Footer (Above Text)",
                                  "Footer (Below Text)", "Hidden"], command=self.schedule_update, height=22).pack(
            side="right", fill="x")

        row_chk = ctk.CTkFrame(c_hf.content, fg_color="transparent");
        row_chk.pack(fill="x", pady=5)
        self.var_bar_ticks = ctk.BooleanVar(value=True);
        ctk.CTkCheckBox(row_chk, text="Ticks", variable=self.var_bar_ticks, command=self.schedule_update).pack(
            side="left")
        self.var_bar_marker = ctk.BooleanVar(value=True);
        ctk.CTkCheckBox(row_chk, text="Marker", variable=self.var_bar_marker, command=self.schedule_update).pack(
            side="right")

        row_mc = ctk.CTkFrame(c_hf.content, fg_color="transparent");
        row_mc.pack(fill="x", pady=2)
        ctk.CTkLabel(row_mc, text="Marker Color:", width=130, anchor="w").pack(side="left")
        self.var_marker_color = ctk.StringVar(value="Black")
        ctk.CTkOptionMenu(row_mc, variable=self.var_marker_color, values=["Black", "White"], width=90, height=22,
                          command=self.schedule_update).pack(side="left", fill="x", expand=True)

        self._create_slider(c_hf.content, "lbl_marker_size", "Marker Radius", "slider_bar_marker_radius", 2, 10)
        self._create_slider(c_hf.content, "lbl_tick_height", "Tick Height", "slider_bar_tick_height", 2, 20)
        self._create_slider(c_hf.content, "lbl_bar_thick", "Bar Thickness", "slider_bar_height", 1, 10)

        self._create_divider_horizontal(c_hf.content)
        f_adv = ctk.CTkFrame(c_hf.content, fg_color="transparent");
        f_adv.pack(fill="x")
        r_h_align = ctk.CTkFrame(f_adv, fg_color="transparent");
        r_h_align.pack(fill="x", pady=2)
        ctk.CTkLabel(r_h_align, text="Header Align:", width=130, anchor="w").pack(side="left")
        self.var_header_align = ctk.StringVar(value="Center")
        ctk.CTkOptionMenu(r_h_align, variable=self.var_header_align, values=["Left", "Center", "Right", "Justify"],
                          command=self.schedule_update, height=22).pack(side="right", fill="x")
        self._create_slider(f_adv, "lbl_header_size", "Header Size", "slider_header_font_size", 8, 30)
        self._create_slider(f_adv, "lbl_header_margin", "Header Y", "slider_header_margin", 0, 80)

        r_f_align = ctk.CTkFrame(f_adv, fg_color="transparent");
        r_f_align.pack(fill="x", pady=2)
        ctk.CTkLabel(r_f_align, text="Footer Align:", width=130, anchor="w").pack(side="left")
        self.var_footer_align = ctk.StringVar(value="Center")
        ctk.CTkOptionMenu(r_f_align, variable=self.var_footer_align, values=["Left", "Center", "Right", "Justify"],
                          command=self.schedule_update, height=22).pack(side="right", fill="x")
        self._create_slider(f_adv, "lbl_footer_size", "Footer Size", "slider_footer_font_size", 8, 30)
        self._create_slider(f_adv, "lbl_footer_margin", "Footer Y", "slider_footer_margin", 0, 80)

    def _build_preview_area(self):
        self.preview_frame = ctk.CTkFrame(self.main_container, fg_color="#181818")
        self.preview_frame.pack(side="left", fill="both", expand=True, padx=(0, 10), pady=10)
        self.preview_scroll = ctk.CTkScrollableFrame(self.preview_frame, fg_color="transparent")
        self.preview_scroll.pack(fill="both", expand=True)
        self.preview_scroll.grid_columnconfigure(0, weight=1);
        self.preview_scroll.grid_rowconfigure(0, weight=1)
        self.img_label = ctk.CTkLabel(self.preview_scroll, text="Loading EPUB...", font=("Arial", 16, "bold"),
                                      text_color="#333")
        self.img_label.grid(row=0, column=0, pady=20, padx=20)

        def pass_scroll_event(event):
            if event.num == 4 or event.delta > 0:
                self.preview_scroll._parent_canvas.yview_scroll(-1, "units")
            elif event.num == 5 or event.delta < 0:
                self.preview_scroll._parent_canvas.yview_scroll(1, "units")

        self.img_label.bind("<MouseWheel>", pass_scroll_event)

        ctrl_bar = ctk.CTkFrame(self.preview_frame, height=50, fg_color="#1a1a1a", corner_radius=15)
        ctrl_bar.pack(side="bottom", fill="x", padx=20, pady=20)
        f_nav = ctk.CTkFrame(ctrl_bar, fg_color="transparent");
        f_nav.place(relx=0.5, rely=0.5, anchor="center")
        ctk.CTkButton(f_nav, text="◀", width=40, command=self.prev_page, fg_color="#333").pack(side="left", padx=5)
        f_page_stack = ctk.CTkFrame(f_nav, fg_color="transparent");
        f_page_stack.pack(side="left", padx=10)
        self.lbl_page = ctk.CTkLabel(f_page_stack, text="0 / 0", font=("Arial", 14, "bold"), width=80);
        self.lbl_page.pack(side="top")
        self.entry_page = ctk.CTkEntry(f_page_stack, width=50, height=20, placeholder_text="#", justify="center",
                                       font=("Arial", 10))
        self.entry_page.pack(side="top", pady=(2, 0));
        self.entry_page.bind('<Return>', lambda e: self.go_to_page())
        ctk.CTkButton(f_nav, text="▶", width=40, command=self.next_page, fg_color="#333").pack(side="left", padx=5)
        f_zoom = ctk.CTkFrame(ctrl_bar, fg_color="transparent");
        f_zoom.pack(side="right", padx=20, pady=10)
        self._create_slider(f_zoom, "lbl_preview_zoom", "Zoom", "slider_preview_zoom", 200, 800, width=150)

    # --- LOGIC WIRING ---

    def load_book(self):
        self.processor.input_file = self.book_data[3]
        self.current_page_index = 0
        threading.Thread(target=self._task_parse_structure).start()

    def _task_parse_structure(self):
        success = self.processor.parse_book_structure(self.processor.input_file)
        self.after(0, lambda: self._on_structure_parsed(success))

    def _on_structure_parsed(self, success):
        if not success:
            messagebox.showerror("Error", "Failed to parse EPUB.")
            return

        # Enable the toolbar button
        self.btn_chapters.configure(state="normal")

        # --- FIX: STOP AUTO-RENDER. OPEN DIALOG FIRST ---
        # OLD: self.run_processing()  <-- This was causing the immediate render

        # NEW: Force the selection dialog to open immediately.
        # This pauses the process until you click "Confirm" in the popup.
        self.open_chapter_dialog()

    def run_processing(self):
        if not self.processor.input_file: return
        if self.selected_chapter_indices is None:
            return
        self.is_processing = True
        self.pending_rerun = False
        self.progress_label.configure(text="Rendering Layout...")
        settings = self.gather_current_ui_settings()
        threading.Thread(target=lambda: self._task_render(settings)).start()

    def _task_render(self, layout_settings):
        # --- FIX START: Resolve None to a concrete list ---
        # The renderer needs an actual list, not None.
        # If None, we generate the "Smart Filter" list here.
        indices_to_render = self.selected_chapter_indices

        if indices_to_render is None:
            indices_to_render = []
            for i, chap in enumerate(self.processor.raw_chapters):
                # Logic: Include chapter unless it is named "Section X"
                is_generic = re.match(r"^Section \d+$", chap['title'])
                if not is_generic:
                    indices_to_render.append(i)

            # Safety: If the filter removed everything, select all chapters
            if not indices_to_render:
                indices_to_render = list(range(len(self.processor.raw_chapters)))
        # --- FIX END ---

        self.selected_chapter_indices = indices_to_render

        success = self.processor.render_chapters(
            indices_to_render,  # <--- Pass the resolved list here
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
        if self.pending_rerun:
            self.after(10, self.run_processing)
            return

        self.progress_label.configure(text="Ready")
        self.progress_bar.set(0)

        if success and self.processor.total_pages > 0:
            self.btn_export.configure(state="normal")
            self.btn_cover.configure(state="normal")

            # Ensure we stay within bounds if the total pages decreased
            if self.current_page_index >= self.processor.total_pages:
                self.current_page_index = self.processor.total_pages - 1

            self.show_page(self.current_page_index)
        else:
            # If it failed or has 0 pages, show a message
            self.img_label.configure(text="No pages rendered.")

    def save_to_library(self):
        # Auto-generate destination path in EXPORT_DIR
        safe_title = "".join([c for c in self.book_data[1] if c.isalnum() or c in " -_"])
        filename = f"{safe_title}.xtc"
        out_path = os.path.join(EXPORT_DIR, filename)
        threading.Thread(target=lambda: self._run_export(out_path)).start()

    def _run_export(self, path):
        self.processor.save_xtc(path, progress_callback=lambda v: self.update_progress_ui(v, "Exporting"))
        self.after(0, lambda: self._on_export_complete(path))

    def _on_export_complete(self, path):
        # Update Database
        self.db.update_xtc_path(self.book_data[0], path)
        messagebox.showinfo("Success", "Book converted and saved to library.")
        self.refresh_callback()
        self.destroy()

    # --- SPECTRA ---
    def run_spectra_analysis(self):
        if not self.processor.input_file or not self.selected_chapter_indices: return
        if not self.entry_spectra_key.get():
            messagebox.showerror("Missing Key", "Please enter an OpenAI API Key.")
            return

        force_regen = False
        if hasattr(self.processor.annotator, 'master_cache') and self.processor.annotator.master_cache:
            ans = messagebox.askyesnocancel("Regenerate?", "Definitions exist. Force regenerate?")
            if ans is None: return
            force_regen = ans

        self.is_processing = True
        self.progress_label.configure(text="Scanning Words...")
        settings = self.gather_current_ui_settings()
        threading.Thread(target=lambda: self._task_analyze(settings, force_regen)).start()

    def _task_analyze(self, layout_settings, force=False):
        self.processor.init_annotator(layout_settings)
        self.processor.annotator.analyze_chapters(
            self.processor.raw_chapters,
            self.selected_chapter_indices,
            progress_callback=lambda v: self.update_progress_ui(v, "Analyzing"),
            force=force
        )
        self.after(0, self.run_processing)

    # --- HELPERS ---
    def update_progress_ui(self, val, stage_text="Processing"):
        try:
            if not self.winfo_exists(): return
            self.after(0, lambda: [
                self.progress_bar.set(val),
                self.progress_label.configure(text=f"{stage_text} {int(val * 100)}%")
            ])
        except:
            pass

    def show_page(self, idx):
        # GUARD: If layout isn't built or map is empty, do nothing
        if not self.processor.is_ready or not self.processor.page_map:
            self.img_label.configure(image=None, text="Calculating Layout...")
            return

        # Ensure index stays in bounds
        idx = max(0, min(idx, self.processor.total_pages - 1))
        self.current_page_index = idx

        try:
            img = self.processor.render_page(idx)
            if not img: return

            # Preview scaling logic
            base_size = int(self.slider_preview_zoom.get())
            ratio = img.width / img.height
            tw, th = (int(base_size * ratio), base_size) if img.width > img.height else (base_size, int(base_size / ratio))

            ctk_img = ctk.CTkImage(light_image=img, size=(tw, th))
            self.img_label.configure(image=ctk_img, text="")
            self.lbl_page.configure(text=f"{idx + 1} / {self.processor.total_pages}")
        except Exception as e:
            print(f"Render sync: {e}") # Caught during rapid slider movement

    def prev_page(self):
        self.show_page(max(0, self.current_page_index - 1))

    def next_page(self):
        self.show_page(min(self.processor.total_pages - 1, self.current_page_index + 1))

    def go_to_page(self):
        try:
            self.show_page(max(0, min(int(self.entry_page.get()) - 1, self.processor.total_pages - 1)))
        except:
            pass

    def open_chapter_dialog(self):
        # Pass self.selected_chapter_indices (3rd argument) to the dialog
        converter.ChapterSelectionDialog(
            self,
            self.processor.raw_chapters,
            self.selected_chapter_indices,
            self._on_chapters_selected
        )

    def _on_chapters_selected(self, selected_indices):
        self.selected_chapter_indices = selected_indices
        self.run_processing()

    def open_cover_export(self):
        if self.processor.cover_image_obj:
            converter.CoverExportDialog(self, self._process_cover_export)
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
        self.progress_label.configure(text="Pending changes...")
        if self.debounce_timer:
            try:
                self.after_cancel(self.debounce_timer)
            except:
                pass
        self.debounce_timer = self.after(500, self.trigger_processing)

    def trigger_processing(self):
        if self.is_processing: self.pending_rerun = True; return
        self.run_processing()

    def on_font_change(self, choice):
        self.processor.font_path = self.font_map[choice]
        self.schedule_update()

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

    def toggle_render_controls(self, _=None):
        mode = self.render_mode_var.get()
        self.frm_dither.pack_forget();
        self.frm_thresh.pack_forget()
        if mode == "Dither":
            self.frm_dither.pack(fill="x")
        elif mode == "Threshold":
            self.frm_thresh.pack(fill="x")
        self.schedule_update()

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
        spectra_thresh = float(self.slider_spectra_threshold.get()) if hasattr(self,
                                                                               'slider_spectra_threshold') else 4.0
        spectra_aoa = float(self.slider_spectra_aoa_threshold.get()) if hasattr(self,
                                                                                'slider_spectra_aoa_threshold') else 0.0
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
            "bit_depth": self.bit_depth_var.get(),
            "white_clip": int(self.slider_white_clip.get()),
            "contrast": float(self.slider_contrast.get()),
            "text_threshold": int(self.slider_text_threshold.get()),
            "text_blur": float(self.slider_text_blur.get()),
            "spectra_enabled": spectra_en,
            "spectra_api_key": spectra_key,
            "spectra_base_url": spectra_url,
            "spectra_model": spectra_model,
            "spectra_threshold": spectra_thresh,
            "spectra_aoa_threshold": spectra_aoa,
            "spectra_target_lang": spectra_lang,
        }

    def apply_settings_dict(self, s):
        defaults = converter.FACTORY_DEFAULTS.copy()
        defaults.update(s);
        s = defaults
        if hasattr(self, 'var_toc_page'): self.var_toc_page.set(str(s.get("toc_insert_page", 1)))
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
        self.render_mode_var.set(s.get("render_mode", "Threshold"))
        self.slider_white_clip.set(s.get("white_clip", 220))
        self.slider_contrast.set(s.get("contrast", 1.2))
        self.slider_text_threshold.set(s.get("text_threshold", 130))
        self.slider_text_blur.set(s.get("text_blur", 1.0))
        if hasattr(self, 'var_spectra_enabled'): self.var_spectra_enabled.set(s.get('spectra_enabled', False))
        if hasattr(self, 'slider_spectra_threshold'): self.slider_spectra_threshold.set(s.get('spectra_threshold', 4.0))
        if hasattr(self, 'slider_spectra_aoa_threshold'): self.slider_spectra_aoa_threshold.set(
            s.get('spectra_aoa_threshold', 0.0))
        if hasattr(self, 'entry_spectra_key'): self.entry_spectra_key.delete(0, 'end'); self.entry_spectra_key.insert(0,
                                                                                                                      s.get(
                                                                                                                          'spectra_api_key',
                                                                                                                          ""))
        if hasattr(self, 'entry_spectra_url'): self.entry_spectra_url.delete(0, 'end'); self.entry_spectra_url.insert(0,
                                                                                                                      s.get(
                                                                                                                          'spectra_base_url',
                                                                                                                          "https://api.openai.com/v1"))
        if hasattr(self, 'entry_spectra_model'): self.entry_spectra_model.delete(0,
                                                                                 'end'); self.entry_spectra_model.insert(
            0, s.get('spectra_model', "gpt-4o-mini"))
        if hasattr(self, 'var_spectra_lang'): self.var_spectra_lang.set(s.get('spectra_target_lang', "English"))

        # Refresh Sliders Text
        for attr in dir(self):
            if attr.startswith("slider_") and hasattr(self, f"fmt_{attr}"):
                fmt = getattr(self, f"fmt_{attr}")
                sld = getattr(self, attr)
                lbl_name = attr.replace("slider_", "lbl_")
                if hasattr(self, lbl_name): getattr(self, lbl_name).configure(text=fmt(sld.get()))

        if s["font_name"] in self.font_options:
            self.font_dropdown.set(s["font_name"])
            self.processor.font_path = self.font_map[s["font_name"]]
        else:
            self.font_dropdown.set("Default (System)")
            self.processor.font_path = self.font_map["Default (System)"]

        self.toggle_render_controls()
        self.schedule_update()


# --- MANUAL METADATA EDITOR ---
class MetadataEditorDialog(ctk.CTkToplevel):
    def __init__(self, parent, current_data, db_instance, on_save):
        super().__init__(parent)
        self.db = db_instance
        self.on_save = on_save
        self.b_id = current_data[0]
        self.epub_path = current_data[3]
        self.current_cover_blob = current_data[5]

        self.title("Edit Metadata")
        self.geometry("900x600")
        self.transient(parent)
        self.grab_set()

        self.left_frame = ctk.CTkFrame(self, width=250, fg_color="transparent")
        self.left_frame.pack(side="left", fill="y", padx=20, pady=20)
        self.left_frame.pack_propagate(False)

        self.lbl_cover = ctk.CTkLabel(self.left_frame, text="Click to Change", cursor="hand2")
        self.lbl_cover.pack(expand=True, fill="both")
        self.lbl_cover.bind("<Button-1>", self.change_cover)
        self._render_cover()

        self.right_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.right_frame.pack(side="left", fill="both", expand=True, padx=20, pady=20)
        self.right_frame.grid_columnconfigure(1, weight=1)
        self.right_frame.grid_rowconfigure(5, weight=1)

        self._add_field("Title:", 0, current_data[1], "entry_title")
        self._add_field("Author:", 1, current_data[2], "entry_author")
        self._add_field("Genre:", 2, current_data[8], "entry_genre")
        self._add_field("Publisher:", 3, current_data[9], "entry_pub")
        self._add_field("Date:", 4, current_data[10], "entry_date")

        ctk.CTkLabel(self.right_frame, text="Description:").grid(row=5, column=0, sticky="ne", padx=5, pady=(10, 0))
        self.txt_desc = ctk.CTkTextbox(self.right_frame, height=150)
        self.txt_desc.grid(row=5, column=1, padx=5, pady=(5, 20), sticky="nsew")
        if current_data[7]: self.txt_desc.insert("0.0", current_data[7])

        btn_row = ctk.CTkFrame(self.right_frame, fg_color="transparent")
        btn_row.grid(row=7, column=0, columnspan=2, pady=10)
        ctk.CTkButton(btn_row, text="Cancel", fg_color="gray", command=self.destroy).pack(side="left", padx=10)
        ctk.CTkButton(btn_row, text="Save Changes", fg_color="green", command=self.save).pack(side="left", padx=10)

    def _add_field(self, label, row, value, attr_name):
        ctk.CTkLabel(self.right_frame, text=label).grid(row=row, column=0, sticky="e", padx=5, pady=5)
        entry = ctk.CTkEntry(self.right_frame)
        entry.grid(row=row, column=1, sticky="ew", padx=5, pady=5)
        if value: entry.insert(0, str(value))
        setattr(self, attr_name, entry)

    def _render_cover(self):
        if self.current_cover_blob:
            try:
                img = Image.open(io.BytesIO(self.current_cover_blob))
                img.thumbnail((220, 330))
                ctk_img = ctk.CTkImage(img, size=(220, 330))
                self.lbl_cover.configure(image=ctk_img, text="")
            except:
                self.lbl_cover.configure(text="Error loading cover")
        else:
            self.lbl_cover.configure(image=None, text="No Cover\nClick to add")

    def change_cover(self, event=None):
        path = filedialog.askopenfilename(filetypes=[("Images", "*.jpg *.jpeg *.png *.bmp")])
        if path:
            try:
                with io.BytesIO() as output:
                    Image.open(path).save(output, format="PNG")
                    self.current_cover_blob = output.getvalue()
                self._render_cover()
            except Exception as e:
                messagebox.showerror("Error", f"Failed: {e}")

    def save(self):
        t = self.entry_title.get()
        a = self.entry_author.get()
        g = self.entry_genre.get()
        p = self.entry_pub.get()
        d = self.entry_date.get()
        desc = self.txt_desc.get("0.0", "end").strip()

        # 1. Update SQLite Database
        self.db.update_book_details(self.b_id, desc, g, p, d, self.current_cover_blob, title=t, author=a)

        # 2. Check Global Config and Inject if Enabled
        config = load_app_config()
        if config.get("update_epub_cover", False) and self.epub_path and self.current_cover_blob:
            # Change window title to show it's working (file I/O can take a second)
            self.title("Injecting Cover into EPUB... Please wait")
            self.update_idletasks()
            inject_cover_into_epub(self.epub_path, self.current_cover_blob)

        self.on_save()
        self.destroy()


# --- BATCH CONVERT ---
class BatchConvertDialog(ctk.CTkToplevel):
    def __init__(self, parent, selected_ids, all_books_ref, on_process_complete):
        super().__init__(parent)
        self.title("Batch Convert")
        self.geometry("400x350")
        self.transient(parent)
        self.grab_set()

        self.selected_ids = selected_ids
        self.all_books = all_books_ref
        self.on_process_complete = on_process_complete
        self.presets = self._load_presets()

        ctk.CTkLabel(self, text=f"Converting {len(selected_ids)} books", font=("Arial", 16, "bold")).pack(pady=(20, 10))
        ctk.CTkLabel(self, text="Select Preset:").pack(pady=(10, 5))
        self.preset_var = ctk.StringVar(value="Factory Defaults")
        options = ["Factory Defaults"] + list(self.presets.keys())
        ctk.CTkOptionMenu(self, variable=self.preset_var, values=options).pack(pady=5)
        self.lbl_info = ctk.CTkLabel(self, text="Ready to start.", text_color="gray")
        self.lbl_info.pack(pady=10)
        self.prog_bar = ctk.CTkProgressBar(self, width=300)
        self.prog_bar.set(0)
        self.prog_bar.pack(pady=15)
        self.btn_start = ctk.CTkButton(self, text="Start Conversion", command=self.start_process, fg_color="#27AE60")
        self.btn_start.pack(pady=10)

    def _load_presets(self):
        p = {}
        for f in glob.glob(os.path.join(PRESETS_DIR, "*.json")):
            try:
                with open(f, "r") as j:
                    p[os.path.basename(f).replace(".json", "")] = json.load(j)
            except:
                pass
        return p

    def start_process(self):
        self.btn_start.configure(state="disabled")
        settings = converter.FACTORY_DEFAULTS.copy()
        settings.update(self.presets.get(self.preset_var.get(), {}))

        # Start background thread
        threading.Thread(target=lambda: self._task_batch_convert(settings)).start()

    def _task_batch_convert(self, settings):
        total = len(self.selected_ids)
        font_map = get_combined_fonts()
        font_map["Default (System)"] = ""

        # Use the master DB instance from XlibreApp
        # Note: self.master refers to XlibreApp which has .db
        app_db = self.master.db

        for i, bid in enumerate(self.selected_ids):
            # Find book data
            b = next((x for x in self.all_books if x[0] == bid), None)
            if not b: continue

            path_epub = b[3]
            if not path_epub or not os.path.exists(path_epub): continue

            title = b[1]
            safe_title = "".join([c for c in title if c.isalnum() or c in " -_"])
            path_xtc = os.path.join(EXPORT_DIR, f"{safe_title}.xtc")

            self.update_ui(i, total, f"Converting: {title[:20]}...")

            try:
                proc = converter.EpubProcessor()
                if not proc.parse_book_structure(path_epub): continue

                if settings.get("spectra_enabled", False):
                    proc.init_annotator(settings)
                    all_indices = list(range(len(proc.raw_chapters)))
                    proc.annotator.analyze_chapters(proc.raw_chapters, all_indices)

                f_name = settings.get("font_name", "Default (System)")
                proc.font_path = font_map.get(f_name, "")

                proc.render_chapters(
                    list(range(len(proc.raw_chapters))),
                    proc.font_path,
                    int(settings['font_size']),
                    int(settings['margin']),
                    float(settings['line_height']),
                    int(settings['font_weight']),
                    int(settings['bottom_padding']),
                    int(settings['top_padding']),
                    text_align=settings['text_align'],
                    orientation=settings['orientation'],
                    add_toc=settings['generate_toc'],
                    show_footnotes=settings.get('show_footnotes', True),
                    layout_settings=settings
                )

                proc.save_xtc(path_xtc)

                # FIX: Explicitly capture bid and path_xtc in the lambda default args
                # We use app_db directly here if thread-safe, or schedule it on main thread
                self.after(0, lambda b_id=bid, p_xtc=path_xtc: app_db.update_xtc_path(b_id, p_xtc))

            except Exception as e:
                print(f"Error converting {title}: {e}")

        self.update_ui(total, total, "Completed!")
        self.after(0, self.on_process_complete)

    def update_ui(self, current, total, status_msg):
        self.after(0, lambda: [
            self.prog_bar.set(current / max(1, total)),
            self.lbl_info.configure(text=status_msg),
            self.btn_start.configure(text="Close", state="normal", command=self.destroy) if current >= total else None
        ])


# --- FILE BADGE ---
class FileBadge(ctk.CTkFrame):
    def __init__(self, parent, text, color, on_delete_click, scroll_callback):
        # Auto-width: 60px for 5+ chars (e.g. ".EPUB", "2-BIT"), 55px for short ones (".XTC")
        w = 60 if len(text) >= 5 else 55

        super().__init__(parent, fg_color=color, corner_radius=6, height=22, width=w)
        self.on_delete_click = on_delete_click
        self.pack_propagate(False)

        self.lbl = ctk.CTkLabel(self, text=text, text_color="white", font=("Arial", 9, "bold"))
        self.lbl.place(relx=0.5, rely=0.5, anchor="center")

        self.lbl_x = ctk.CTkLabel(self, text="✕", text_color="white", font=("Arial", 10, "bold"), fg_color=color)
        self.lbl_x.bind("<Button-1>", self._delete)
        self.lbl_x.bind("<Enter>", lambda e: self.lbl_x.configure(text_color="#C0392B"))
        self.lbl_x.bind("<Leave>", lambda e: self.lbl_x.configure(text_color="white"))

        # BIND SCROLL EVENTS RECURSIVELY
        for w in [self, self.lbl, self.lbl_x]:
            w.bind("<Enter>", self._on_enter)
            w.bind("<Leave>", self._on_leave)
            w.bind("<MouseWheel>", scroll_callback)
            w.bind("<Button-4>", scroll_callback)
            w.bind("<Button-5>", scroll_callback)

    def _on_enter(self, event):
        self.lbl.place(relx=0.35, rely=0.5, anchor="center")
        self.lbl_x.place(relx=0.8, rely=0.5, anchor="center")

    def _on_leave(self, event):
        try:
            x, y = self.winfo_pointerx(), self.winfo_pointery()
            widget_under_mouse = self.winfo_containing(x, y)
        except:
            widget_under_mouse = None

        if widget_under_mouse is self or widget_under_mouse in self.winfo_children():
            return

        self.lbl.place(relx=0.5, rely=0.5, anchor="center")
        self.lbl_x.place_forget()

    def _delete(self, event=None):
        if messagebox.askyesno("Confirm", "Delete this file?"): self.on_delete_click()

# --- BOOK DETAILS (UPDATED) ---
class BookDetailsWindow(ctk.CTkToplevel):
    def __init__(self, parent, book_data, on_convert, db_instance, refresh_cb):
        super().__init__(parent)
        self.db, self.refresh_cb, self.book_data, self.on_convert_cb = db_instance, refresh_cb, book_data, on_convert
        self.title("Book Details")
        self.geometry("750x600")
        self.transient(parent)
        self.grab_set()
        self.b_id = book_data[0]
        self.render_ui()

    def render_ui(self):
        for w in self.winfo_children(): w.destroy()
        curr = next((b for b in self.db.get_all_books() if b[0] == self.b_id), self.book_data)

        # Left Column: Cover & Metadata Actions
        left = ctk.CTkFrame(self, width=240, fg_color="transparent")
        left.pack(side="left", fill="y", padx=20, pady=20)

        img_lbl = ctk.CTkLabel(left, text="No Cover", width=200, height=300, fg_color="#333", corner_radius=10)
        if curr[5]:
            try:
                img_lbl.configure(image=ctk.CTkImage(Image.open(io.BytesIO(curr[5])), size=(220, 330)), text="")
            except:
                pass
        img_lbl.pack()

        self.btn_fetch = ctk.CTkButton(left, text="☁ Fetch Metadata", fg_color="#8E44AD", command=self.run_fetch)
        self.btn_fetch.pack(pady=(15, 5), fill="x")
        ctk.CTkButton(left, text="✎ Edit Manually", fg_color="#F39C12", command=self.open_editor).pack(pady=(5, 10),
                                                                                                       fill="x")

        # Right Column: Info & Viewers
        right = ctk.CTkFrame(self, fg_color="transparent")
        right.pack(side="left", fill="both", expand=True, padx=(0, 20), pady=20)

        ctk.CTkLabel(right, text=curr[1], font=("Arial", 22, "bold"), anchor="w", wraplength=450, justify="left").pack(
            fill="x")
        ctk.CTkLabel(right, text=curr[2], font=("Arial", 16), text_color="gray", anchor="w").pack(fill="x", pady=5)

        # --- FILES & VIEWERS SECTION ---
        files_frame = ctk.CTkFrame(right, fg_color="#222", corner_radius=6)
        files_frame.pack(fill="x", pady=10)

        # EPUB Viewer
        epub_path = curr[3]
        if epub_path and os.path.exists(epub_path):
            row_epub = ctk.CTkFrame(files_frame, fg_color="transparent")
            row_epub.pack(fill="x", padx=10, pady=5)
            ctk.CTkLabel(row_epub, text="Original EPUB", font=("Arial", 12, "bold"), text_color="#E67E22").pack(
                side="left")
            ctk.CTkButton(row_epub, text="📖 View Epub", width=120, height=24, fg_color="#E67E22",
                          command=lambda: SimpleEpubReader(self, epub_path, curr[1])).pack(side="right")

        # XTC Viewer (Visual)
        xtc_path = curr[4]
        if xtc_path and os.path.exists(xtc_path):
            row_xtc = ctk.CTkFrame(files_frame, fg_color="transparent")
            row_xtc.pack(fill="x", padx=10, pady=5)
            ctk.CTkLabel(row_xtc, text="Converted XTC", font=("Arial", 12, "bold"), text_color="#2ECC71").pack(
                side="left")
            ctk.CTkButton(row_xtc, text="🖼 View XTC", width=120, height=24, fg_color="#27AE60",
                          command=lambda: XtcBookViewer(self, xtc_path)).pack(side="right")
        # -------------------------------

        # Metadata Details
        meta = ctk.CTkFrame(right, fg_color="transparent")
        meta.pack(fill="x", pady=15)
        for l, v in [("Publisher", curr[9]), ("Published", curr[10]), ("Genre", curr[8])]:
            r = ctk.CTkFrame(meta, fg_color="transparent")
            r.pack(anchor="w")
            ctk.CTkLabel(r, text=f"{l}: ", font=("Arial", 12, "bold"), text_color="#aaa").pack(side="left")
            ctk.CTkLabel(r, text=v or "Unknown", font=("Arial", 12)).pack(side="left")

        ctk.CTkLabel(right, text="Description", font=("Arial", 12, "bold"), anchor="w").pack(fill="x")
        tb = ctk.CTkTextbox(right, height=120, fg_color="#2B2B2B", wrap="word")
        tb.pack(fill="x", pady=5)
        tb.insert("0.0", curr[7] or "No description.")
        tb.configure(state="disabled")

        # Bottom Actions
        action_row = ctk.CTkFrame(right, fg_color="transparent")
        action_row.pack(side="bottom", fill="x")
        ctk.CTkButton(action_row, text="Close", fg_color="gray", command=self.destroy, width=100).pack(side="left")
        ctk.CTkButton(action_row, text="⚡ Open Converter", width=140, fg_color="#3498DB",
                      command=lambda: [self.destroy(), self.on_convert_cb(curr)]).pack(side="right")

    def run_fetch(self):
        self.btn_fetch.configure(state="disabled", text="☁ Fetching...")
        threading.Thread(target=self._task).start()

    def _task(self):
        res = UnifiedMetadataFetcher.search_and_merge(self.book_data[1], self.book_data[2])
        if not res or res["source"] == "None":
            self.after(0, lambda: [
                self.btn_fetch.configure(state="normal", text="☁ Fetch Metadata"),
                messagebox.showinfo("Not Found", "No additional metadata found online.")
            ])
            return

        curr = next((b for b in self.db.get_all_books() if b[0] == self.b_id), None)

        def pick(o, n): return n if (
                    not o or str(o).lower() in ["unknown", "unknown author", "no description found.", ""]) else o

        self.db.update_book_details(self.b_id, pick(curr[7], res["description"]), pick(curr[8], res["categories"]),
                                    pick(curr[9], res["publisher"]), pick(curr[10], res["publishedDate"]),
                                    curr[5] if curr[5] else res["cover_blob"])
        self.after(0, lambda: [self.refresh_cb(), self.render_ui()])

    def open_editor(self):
        curr = next((b for b in self.db.get_all_books() if b[0] == self.b_id), self.book_data)
        MetadataEditorDialog(self, curr, self.db, lambda: [self.refresh_cb(), self.render_ui()])


# --- VIEWERS ---
class RawFileViewer(ctk.CTkToplevel):
    def __init__(self, parent, file_path):
        super().__init__(parent)
        self.title(f"Raw Viewer: {os.path.basename(file_path)}")
        self.geometry("900x600")
        self.transient(parent)
        self.file_path = file_path

        # Toolbar
        toolbar = ctk.CTkFrame(self, height=40)
        toolbar.pack(fill="x", padx=5, pady=5)
        ctk.CTkLabel(toolbar, text="Hex / ASCII Dump", font=("Arial", 12, "bold")).pack(side="left", padx=10)

        # Content
        self.textbox = ctk.CTkTextbox(self, font=("Courier New", 14), activate_scrollbars=True)
        self.textbox.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        # Load content in a thread to prevent freezing
        threading.Thread(target=self._load_hex).start()

    def _load_hex(self):
        try:
            display_text = []
            with open(self.file_path, 'rb') as f:
                offset = 0
                while True:
                    chunk = f.read(16)
                    if not chunk: break

                    # Hex section
                    hex_vals = " ".join(f"{b:02X}" for b in chunk)
                    padding = "   " * (16 - len(chunk))

                    # ASCII section
                    ascii_vals = "".join((chr(b) if 32 <= b < 127 else ".") for b in chunk)

                    line = f"{offset:08X}  {hex_vals}{padding}  |{ascii_vals}|"
                    display_text.append(line)
                    offset += 16

                    # Limit for performance (first 2000 lines ~ 32KB)
                    if offset > 32000:
                        display_text.append(f"\n... File too large, showing first 32KB only ...")
                        break

            full_text = "\n".join(display_text)
            self.after(0, lambda: self._update_text(full_text))
        except Exception as e:
            self.after(0, lambda: self._update_text(f"Error reading file: {e}"))

    def _update_text(self, text):
        self.textbox.insert("0.0", text)
        self.textbox.configure(state="disabled")  # Read-only


# --- VIEWERS ---

class SimpleEpubReader(ctk.CTkToplevel):
    def __init__(self, parent, file_path, book_title):
        super().__init__(parent)
        self.attributes("-topmost", True)
        self.title(f"Reading: {book_title}")
        self.geometry("1000x800")
        self.file_path = file_path

        # Sidebar for TOC
        self.sidebar = ctk.CTkScrollableFrame(self, width=250, label_text="Chapters")
        self.sidebar.pack(side="left", fill="y", padx=10, pady=10)

        # Main content
        self.content_area = ctk.CTkTextbox(self, font=("Georgia", 16), wrap="word")
        self.content_area.pack(side="left", fill="both", expand=True, padx=(0, 10), pady=10)

        # --- FIX: Access the underlying Tkinter widget (._textbox) to apply font tags ---
        self.content_area._textbox.tag_config("h1", font=("Georgia", 24, "bold"), spacing3=15)
        self.content_area._textbox.tag_config("h2", font=("Georgia", 20, "bold"), spacing3=10)
        self.content_area._textbox.tag_config("p", font=("Georgia", 16), spacing2=5)
        # -----------------------------------------------------------------------------

        threading.Thread(target=self._load_book).start()

    def _load_book(self):
        try:
            self.book = epub.read_epub(self.file_path)
            toc_items = []

            # Helper function to handle nested TOCs (Parts > Chapters)
            def process_toc(toc_list):
                for item in toc_list:
                    if isinstance(item, tuple):
                        # This is a section/parent, process the sub-items
                        process_toc(item[1])
                    elif isinstance(item, epub.Link):
                        # This is a chapter link
                        # Get the actual document item using the href
                        doc = self.book.get_item_with_href(item.href)
                        if doc:
                            toc_items.append((item.title, doc))

            # Start processing from the root TOC
            process_toc(self.book.toc)

            # If TOC was empty/failed, fallback to your previous logic
            if not toc_items:
                for item in self.book.get_items():
                    if item.get_type() == ebooklib.ITEM_DOCUMENT:
                        toc_items.append((item.get_name(), item))

            self.after(0, lambda: self._populate_sidebar(toc_items))
        except Exception as e:
            print(f"Error loading EPUB: {e}")

    def _populate_sidebar(self, items):
        for title, item in items:
            # Use a closure (i=item) to capture the specific chapter
            btn = ctk.CTkButton(self.sidebar, text=title, anchor="w", fg_color="transparent",
                                hover_color="#444", height=30,
                                command=lambda i=item: self._display_chapter(i))
            btn.pack(fill="x")
        if items: self._display_chapter(items[0][1])

    def _display_chapter(self, item):
        self.content_area.configure(state="normal")
        self.content_area.delete("0.0", "end")

        try:
            soup = BeautifulSoup(item.get_content(), 'html.parser')
            for element in soup.find_all(['h1', 'h2', 'p', 'div']):
                text = element.get_text().strip()
                if not text: continue
                tag = "h1" if element.name == 'h1' else "h2" if element.name == 'h2' else "p"
                self.content_area.insert("end", text + "\n\n", tag)
        except Exception as e:
            self.content_area.insert("end", f"Error: {e}")

        self.content_area.configure(state="disabled")


class XtcBookViewer(ctk.CTkToplevel):
    def __init__(self, parent, file_path):
        super().__init__(parent)
        self.attributes("-topmost", True)
        self.title(f"XTC Viewer: {os.path.basename(file_path)}")
        self.geometry("1200x800")  # Wider to accommodate sidebar
        self.file_path = file_path

        # State
        self.total_pages = 0
        self.current_page = 0
        self.page_offsets = []
        self.chapters = []

        # --- Layout ---
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # 1. Sidebar (TOC)
        self.sidebar = ctk.CTkScrollableFrame(self, width=220, label_text="Chapters")
        self.sidebar.grid(row=0, column=0, rowspan=3, sticky="nsew", padx=(10, 0), pady=10)

        # 2. Toolbar
        self.toolbar = ctk.CTkFrame(self, height=40, fg_color="#222")
        self.toolbar.grid(row=0, column=1, sticky="ew", padx=10, pady=(10, 5))

        self.lbl_info = ctk.CTkLabel(self.toolbar, text="Loading...", font=("Arial", 12, "bold"), text_color="#AAA")
        self.lbl_info.pack(side="left", padx=10)

        # 3. Image Area
        self.scroll = ctk.CTkScrollableFrame(self, fg_color="#1a1a1a")
        self.scroll.grid(row=1, column=1, sticky="nsew", padx=10, pady=5)

        self.img_label = ctk.CTkLabel(self.scroll, text="Initializing...", text_color="gray")
        self.img_label.pack(expand=True, pady=20)

        # 4. Navigation Bar
        self.nav = ctk.CTkFrame(self, height=50, fg_color="#222")
        self.nav.grid(row=2, column=1, sticky="ew", padx=10, pady=(5, 10))

        ctk.CTkButton(self.nav, text="◀ Prev", width=80, command=self.prev_page).pack(side="left", padx=10)
        self.lbl_page = ctk.CTkLabel(self.nav, text="Page 0/0", font=("Arial", 14, "bold"))
        self.lbl_page.pack(side="left", expand=True)
        ctk.CTkButton(self.nav, text="Next ▶", width=80, command=self.next_page).pack(side="right", padx=10)

        # Start Parsing
        self.parse_xtc_structure()

    def parse_xtc_structure(self):
        try:
            with open(self.file_path, "rb") as f:
                # --- 1. READ HEADER (56 bytes) ---
                header_data = f.read(56)
                if len(header_data) < 56: raise ValueError("File too short")

                # Unpack: ID, Ver, Pages, Flags(5), MetaOff, IndexOff, DataOff, Reserved, ChapterOff
                fields = struct.unpack("<IHHBBBBIQQQQQ", header_data)

                self.total_pages = fields[2]
                metadata_offset = fields[8]
                index_offset = fields[9]
                chapter_offset = fields[12]

                # --- 2. READ CHAPTERS ---
                # First, get chapter count from Metadata block (Offset 0xF6 = 246)
                f.seek(metadata_offset + 246)
                chap_count_data = f.read(2)
                chapter_count = struct.unpack("<H", chap_count_data)[0]

                # Now read the Chapter Table
                f.seek(chapter_offset)
                self.chapters = []
                for _ in range(chapter_count):
                    # Each chapter is 96 bytes: Name(80), Start(2), End(2), Res(12)
                    c_data = f.read(96)
                    if len(c_data) < 96: break

                    c_name_bytes, c_start, c_end = struct.unpack("<80sHH", c_data[:84])
                    c_name = c_name_bytes.decode('utf-8', errors='ignore').strip('\x00')

                    self.chapters.append((c_name, c_start))

                # Populate Sidebar
                for name, start_pg in self.chapters:
                    btn = ctk.CTkButton(self.sidebar, text=name, anchor="w", fg_color="transparent",
                                        hover_color="#444", height=28,
                                        command=lambda p=start_pg: self.show_page(p))
                    btn.pack(fill="x")

                # --- 3. READ PAGE INDEX ---
                f.seek(index_offset)
                self.page_offsets = []
                for _ in range(self.total_pages):
                    entry_data = f.read(16)
                    if len(entry_data) < 16: break
                    p_off, p_size, p_w, p_h = struct.unpack("<QIHH", entry_data)
                    self.page_offsets.append({"offset": p_off, "size": p_size, "width": p_w, "height": p_h})

                # Update UI info
                self.lbl_info.configure(text=f"Pages: {self.total_pages} | Chapters: {len(self.chapters)}")

            if self.page_offsets:
                self.show_page(0)
            else:
                self.img_label.configure(text="Error: No pages found.")

        except Exception as e:
            self.img_label.configure(text=f"Parse Error: {e}")

    def show_page(self, idx):
        if not self.page_offsets: return

        if idx < 0: idx = 0
        if idx >= self.total_pages: idx = self.total_pages - 1
        self.current_page = idx
        self.lbl_page.configure(text=f"Page {idx + 1} / {self.total_pages}")

        threading.Thread(target=self._render_thread).start()

    def _render_thread(self):
        try:
            page_info = self.page_offsets[self.current_page]
            abs_offset = page_info["offset"]
            w = page_info["width"]
            h = page_info["height"]

            with open(self.file_path, "rb") as f:
                f.seek(abs_offset)
                page_head = f.read(22)
                file_id = struct.unpack("<I", page_head[:4])[0]
                is_2bit = (file_id == 0x00485458)

                bitmap_len = page_info["size"] - 22
                bitmap_data = f.read(bitmap_len)

            if is_2bit:
                # --- CORRECT 2-BIT DECODING (VERTICAL UNPACKING) ---
                # 1. Calculate dimensions
                bytes_per_col = (h + 7) // 8
                plane_size = bytes_per_col * w

                # 2. Separate Bit Planes
                plane1 = bitmap_data[:plane_size]
                plane2 = bitmap_data[plane_size:]

                # 3. Create a target image (Grayscale)
                img = Image.new("L", (w, h))
                pixels = img.load()

                # 4. Iterate columns Right-to-Left (matching converter.py)
                # Note: This loop is slow in pure Python but necessary for the preview
                for x_idx, x in enumerate(range(w - 1, -1, -1)):
                    for y in range(h):
                        # Calculate byte index in the vertical column
                        byte_offset = (x_idx * bytes_per_col) + (y // 8)
                        bit_pos = 7 - (y % 8)  # MSB is top

                        # Extract bits from both planes
                        try:
                            val1 = (plane1[byte_offset] >> bit_pos) & 1
                            val2 = (plane2[byte_offset] >> bit_pos) & 1

                            # Combine bits: (Bit1 << 1) | Bit2
                            # Map 0-3 to 0-255: 0=Black, 3=White
                            color_val = ((val1 << 1) | val2) * 85
                            pixels[x, y] = color_val
                        except IndexError:
                            pass  # Padding bytes

            else:
                # 1-bit Standard (Horizontal)
                img = Image.frombytes('1', (w, h), bitmap_data)

            # --- PREVIEW SCALING ---
            # 1. Convert to Grayscale (L) for smooth scaling
            if img.mode != "L":
                img = img.convert("L")

            # 2. Calculate display size (Fixed height 550px)
            display_h = 550
            ratio = display_h / h
            display_w = int(w * ratio)

            # 3. High Quality Resize
            img = img.resize((display_w, display_h), Image.Resampling.LANCZOS)

            ctk_img = ctk.CTkImage(light_image=img, size=(display_w, display_h))
            self.after(0, lambda: self.img_label.configure(image=ctk_img, text=""))
            self.after(0, lambda: self.lbl_info.configure(
                text=f"Resolution: {w}x{h} px ({'2-bit' if is_2bit else '1-bit'})"))

        except Exception as e:
            self.after(0, lambda: self.img_label.configure(text=f"Render Error: {e}", image=None))

    def prev_page(self):
        self.show_page(self.current_page - 1)

    def next_page(self):
        self.show_page(self.current_page + 1)


def detect_xtc_version(filepath):
    """
    Inspects an XTC file to determine if it is 1-bit or 2-bit.
    Returns a tuple: (Label Text, Badge Color)
    """
    default = (".XTC", "#2ECC71")  # Default Green

    if not filepath or not os.path.exists(filepath):
        return default

    try:
        with open(filepath, "rb") as f:
            # 1. Read Container Header (56 bytes)
            # The Data Offset is a Q (unsigned long long) at offset 32
            # struct format: <IHHBBBBIQQQQQ
            # Offsets: I(0) H(4) H(6) B(8) B(9) B(10) B(11) I(12) Q(16) Q(24) Q(32)
            f.seek(32)
            data_offset_bytes = f.read(8)
            if len(data_offset_bytes) < 8: return default

            data_offset = struct.unpack("<Q", data_offset_bytes)[0]

            # 2. Jump to First Page Header
            f.seek(data_offset)
            page_head = f.read(4)
            if len(page_head) < 4: return default

            file_id = struct.unpack("<I", page_head)[0]

            # 3. Check ID
            if file_id == 0x00485458:
                return ("2-BIT", "#1ABC9C")  # Teal for 2-bit (High Quality)
            elif file_id == 0x00475458:
                return ("1-BIT", "#2ECC71")  # Green for 1-bit (Standard)

            return default
    except Exception:
        return default

# --- BOOK CARD ---
class BookCard(ctk.CTkFrame, DnDWrapper):
    def __init__(self, parent, book_data, on_convert, on_details, on_delete, on_select, on_delete_file,
                 on_status_update, scroll_cb):
        super().__init__(parent, fg_color="#2B2B2B", corner_radius=10, border_width=1, border_color="#333")
        self.TkdndVersion = TkinterDnD._require(self)
        self.book_data = book_data
        self.var = ctk.BooleanVar(value=False)
        self.scroll_cb = scroll_cb

        self._start_pos = (0, 0)

        # 1. Header Frame
        h = ctk.CTkFrame(self, fg_color="transparent", height=24)
        h.pack(fill="x", padx=8, pady=(8, 0))

        # Checkbox
        ctk.CTkCheckBox(h, text="", width=20, variable=self.var,
                        command=lambda: on_select(book_data[0], self.var.get())).pack(side="left")

        # 2. Badges Container (.XTC / .EPUB)
        badges = ctk.CTkFrame(self, fg_color="transparent")
        badges.place(relx=0.97, rely=0.02, anchor="ne")

        path_xtc = book_data[4]
        path_epub = book_data[3]

        if path_xtc and os.path.exists(path_xtc):
            # DETECT VERSION
            xtc_label, xtc_color = detect_xtc_version(path_xtc)
            FileBadge(badges, xtc_label, xtc_color, lambda: on_delete_file(book_data[0], "xtc"), scroll_cb).pack(
                side="right", padx=2)

        if path_epub and os.path.exists(path_epub):
            FileBadge(badges, ".EPUB", "#E67E22", lambda: on_delete_file(book_data[0], "epub"), scroll_cb).pack(
                side="right", padx=2)

        # 3. Content Frame (Cover Image)
        self.c_f = ctk.CTkFrame(self, fg_color="transparent")
        self.c_f.pack(pady=5, padx=5)

        self.lbl_cover = ctk.CTkLabel(self.c_f, text="NO COVER", width=120, height=160, fg_color="#444")
        if book_data[5]:
            try:
                i = ctk.CTkImage(Image.open(io.BytesIO(book_data[5])), size=(120, 160))
                self.lbl_cover = ctk.CTkLabel(self.c_f, image=i, text="")
            except:
                pass
        self.lbl_cover.pack()

        # Bind Click vs Drag for Cover
        self.lbl_cover.configure(cursor="hand2")
        self.lbl_cover.bind("<ButtonPress-1>", self._on_press, add="+")
        self.lbl_cover.bind("<ButtonRelease-1>", lambda e: self._on_release(e, on_details), add="+")

        # Status Badge
        status_val = book_data[-1] if len(book_data) > 12 else "Unread"
        if status_val not in ["Unread", "Reading", "Finished"]: status_val = "Unread"

        status_colors = {"Unread": "#444", "Reading": "#2980B9", "Finished": "#27AE60"}
        status_cycle = ["Unread", "Reading", "Finished"]

        def on_status_click():
            current = self.btn_status.cget("text")
            if current not in status_cycle: current = "Unread"
            next_idx = (status_cycle.index(current) + 1) % len(status_cycle)
            new_status = status_cycle[next_idx]
            self.btn_status.configure(text=new_status, fg_color=status_colors.get(new_status, "#444"))
            on_status_update(book_data[0], new_status)

        self.btn_status = ctk.CTkButton(self.c_f, text=status_val, width=50, height=20, corner_radius=6,
                                        font=("Arial", 9, "bold"), fg_color=status_colors.get(status_val, "#444"),
                                        command=on_status_click)
        self.btn_status.place(relx=0.95, rely=0.95, anchor="se")

        # 4. Text Info
        t = book_data[1] if len(book_data[1]) < 16 else book_data[1][:13] + "..."
        self.lbl_title = ctk.CTkLabel(self, text=t, font=("Arial", 13, "bold"))
        self.lbl_title.pack(pady=(2, 0))

        self.lbl_auth = ctk.CTkLabel(self, text=(book_data[2][:15] + "..." if len(book_data[2]) > 18 else book_data[2]),
                                     text_color="#AAAAAA", font=("Arial", 11))
        self.lbl_auth.pack(pady=(0, 5))

        # 5. Action Buttons
        act = ctk.CTkFrame(self, fg_color="transparent")
        act.pack(fill="x", padx=10, pady=(0, 10))
        ctk.CTkButton(act, text="CONVERT", height=24, width=65, fg_color="#3498DB",
                      command=lambda: on_convert(book_data)).pack(side="left", padx=2)
        ctk.CTkButton(act, text="✕", height=24, width=30, fg_color="#444", hover_color="red",
                      command=lambda: on_delete(book_data[0])).pack(side="right", padx=2)

        # --- SCROLL BINDING ---
        self._bind_scroll_recursive(self, scroll_cb)

        # --- DRAG REGISTRATION ---
        self._register_drag_recursive(self)

    def _bind_scroll_recursive(self, widget, cb):
        try:
            widget.bind("<MouseWheel>", cb)
            widget.bind("<Button-4>", cb)
            widget.bind("<Button-5>", cb)
        except:
            pass
        for child in widget.winfo_children():
            self._bind_scroll_recursive(child, cb)

    def _register_drag_recursive(self, widget):
        if isinstance(widget, (ctk.CTkButton, ctk.CTkCheckBox, ctk.CTkEntry, ctk.CTkSlider, ctk.CTkOptionMenu)):
            return
        try:
            widget.drag_source_register(1, DND_FILES)
            widget.dnd_bind('<<DragInitCmd>>', self.drag_init)
            if hasattr(widget, '_canvas'):
                widget._canvas.drag_source_register(1, DND_FILES)
                widget._canvas.dnd_bind('<<DragInitCmd>>', self.drag_init)
        except:
            pass
        for child in widget.winfo_children():
            self._register_drag_recursive(child)

    def _on_press(self, event):
        self._start_pos = (event.x_root, event.y_root)

    def _on_release(self, event, callback):
        dx = abs(event.x_root - self._start_pos[0])
        dy = abs(event.y_root - self._start_pos[1])
        if dx < 5 and dy < 5:
            callback(self.book_data)

    def drag_init(self, event):
        # Identify specific target
        target = self.winfo_containing(event.x_root, event.y_root)

        # Helper to find parent badges
        def check_badge(w):
            curr = w
            for _ in range(4):
                if not curr: break
                try:
                    txt = str(curr.cget("text"))
                    if ".EPUB" in txt: return "epub"
                    # FIX: Check for 1-BIT and 2-BIT labels as well as .XTC
                    if ".XTC" in txt or "1-BIT" in txt or "2-BIT" in txt: return "xtc"
                except:
                    pass
                curr = curr.master
            return None

        # --- DETECT MODE ---
        mode = "folder"  # Default (Background)

        badge = check_badge(target)
        if badge:
            mode = badge
        else:
            # Check if target is the cover
            # We check: The label, the label's canvas, the container frame (c_f), or if the target's master is the label
            is_cover = False
            if target == self.lbl_cover:
                is_cover = True
            elif target == self.c_f:
                is_cover = True
            elif hasattr(self.lbl_cover, '_canvas') and target == self.lbl_cover._canvas:
                is_cover = True
            elif hasattr(target, 'master') and target.master == self.lbl_cover:
                is_cover = True

            if is_cover:
                mode = "cover"

        # Gather Files
        files = []
        app = self.winfo_toplevel()
        books = [self.book_data]
        if hasattr(app, 'selected_ids') and self.book_data[0] in app.selected_ids and len(app.selected_ids) > 1:
            books = [b for b in app.all_books if b[0] in app.selected_ids]

        if mode == "folder":
            temp_root = os.path.join(EXPORT_DIR, "DragTemp")
            os.makedirs(temp_root, exist_ok=True)
            for b in books:
                safe = "".join([c for c in b[1] if c.isalnum() or c in " -_"]).strip()
                b_dir = os.path.join(temp_root, safe)
                os.makedirs(b_dir, exist_ok=True)
                if b[3] and os.path.exists(b[3]): shutil.copy2(b[3], b_dir)
                if b[4] and os.path.exists(b[4]): shutil.copy2(b[4], b_dir)
                if b[5]:
                    try:
                        img = Image.open(io.BytesIO(b[5])).convert("RGB")
                        img = ImageOps.fit(img, (480, 800), method=Image.Resampling.LANCZOS)
                        img = ImageEnhance.Contrast(img.convert("L")).enhance(1.6).convert("1")
                        img.save(os.path.join(b_dir, "cover.bmp"))
                    except:
                        pass
                files.append(b_dir)

        elif mode == "cover":
            for b in books:
                if b[5]:
                    safe = "".join([c for c in b[1] if c.isalnum() or c in " -_"]).strip()
                    out = os.path.join(EXPORT_DIR, f"{safe}_cover.bmp")
                    try:
                        img = Image.open(io.BytesIO(b[5])).convert("RGB")
                        img = ImageOps.fit(img, (480, 800), method=Image.Resampling.LANCZOS)
                        img = ImageEnhance.Contrast(img.convert("L")).enhance(1.6).convert("1")
                        img.save(out)
                        files.append(out)
                    except:
                        pass

        elif mode in ["epub", "xtc"]:
            idx = 3 if mode == "epub" else 4
            for b in books:
                if b[idx] and os.path.exists(b[idx]): files.append(b[idx])

        if files:
            return (('copy',), (DND_FILES,), tuple(files))
        return None

class MultipartStreamer:
    def __init__(self, filepath, field_name, remote_filename, callback):
        self.filepath = filepath
        self.field_name = field_name
        self.remote_filename = remote_filename
        self.callback = callback

        self.boundary = '------------------------XlibreBoundary12345'
        self.content_type = f'multipart/form-data; boundary={self.boundary}'
        self.total_file_size = os.path.getsize(filepath)
        self.bytes_read = 0

        self.header = (
            f'--{self.boundary}\r\n'
            f'Content-Disposition: form-data; name="{self.field_name}"; filename="{self.remote_filename}"\r\n'
            f'Content-Type: application/octet-stream\r\n\r\n'
        ).encode('utf-8')

        self.footer = f'\r\n--{self.boundary}--\r\n'.encode('utf-8')
        self.len = len(self.header) + self.total_file_size + len(self.footer)

    def __iter__(self):
        yield self.header
        chunk_size = 32768

        with open(self.filepath, 'rb') as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk: break

                self.bytes_read += len(chunk)
                if self.callback:
                    # Send bytes read and total file size to the callback
                    self.callback(self.bytes_read, self.total_file_size)

                yield chunk
                time.sleep(0.1)  # Throttling for device stability

        yield self.footer

    def __len__(self):
        return self.len


class BookRow(ctk.CTkFrame, DnDWrapper):
    def __init__(self, parent, book_data, on_convert, on_details, on_delete, on_select, on_delete_file,
                 on_status_update, scroll_cb):
        super().__init__(parent, fg_color="#2B2B2B", height=50, corner_radius=6)
        self.TkdndVersion = TkinterDnD._require(self)
        self.pack_propagate(False)
        self.book_data = book_data
        self.var = ctk.BooleanVar(value=False)
        self._start_pos = (0, 0)
        self.scroll_cb = scroll_cb

        # Columns
        self.grid_columnconfigure(2, weight=1)

        # 1. Checkbox
        ctk.CTkCheckBox(self, text="", width=20, variable=self.var,
                        command=lambda: on_select(book_data[0], self.var.get())).grid(row=0, column=0, padx=10)

        # 2. Icon
        self.icon_frame = ctk.CTkFrame(self, width=30, height=40, fg_color="transparent")
        self.icon_frame.grid(row=0, column=1, padx=5)

        if book_data[5]:
            try:
                img = Image.open(io.BytesIO(book_data[5]))
                img.thumbnail((30, 40))
                self.lbl_icon = ctk.CTkLabel(self.icon_frame, image=ctk.CTkImage(img, size=(30, 40)), text="")
            except:
                self.lbl_icon = ctk.CTkLabel(self.icon_frame, text="📓", font=("Arial", 20))
        else:
            self.lbl_icon = ctk.CTkLabel(self.icon_frame, text="📓", font=("Arial", 20))

        self.lbl_icon.pack()
        self.lbl_icon.bind("<ButtonPress-1>", self._on_press, add="+")
        self.lbl_icon.bind("<ButtonRelease-1>", lambda e: self._on_release(e, on_details), add="+")

        # 3. Info
        self.info_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.info_frame.grid(row=0, column=2, padx=10, sticky="w")
        ctk.CTkLabel(self.info_frame, text=book_data[1], font=("Arial", 13, "bold")).pack(anchor="w")
        ctk.CTkLabel(self.info_frame, text=book_data[2], text_color="#AAA", font=("Arial", 11)).pack(anchor="w")

        # 4. Badges
        badge_frame = ctk.CTkFrame(self, fg_color="transparent")
        badge_frame.grid(row=0, column=3, padx=10)

        if book_data[4] and os.path.exists(book_data[4]):
            # DETECT VERSION
            xtc_label, xtc_color = detect_xtc_version(book_data[4])
            FileBadge(badge_frame, xtc_label, xtc_color, lambda: on_delete_file(book_data[0], "xtc"), scroll_cb).pack(
                side="left", padx=2)

        if book_data[3] and os.path.exists(book_data[3]):
            FileBadge(badge_frame, ".EPUB", "#E67E22", lambda: on_delete_file(book_data[0], "epub"), scroll_cb).pack(
                side="left", padx=2)
        # 5. Status
        status_val = book_data[-1] if len(book_data) > 12 else "Unread"
        if status_val not in ["Unread", "Reading", "Finished"]: status_val = "Unread"
        status_colors = {"Unread": "#444", "Reading": "#2980B9", "Finished": "#27AE60"}

        def on_status_click():
            cycle = ["Unread", "Reading", "Finished"]
            curr = self.btn_status.cget("text")
            if curr not in cycle: curr = "Unread"
            new_s = cycle[(cycle.index(curr) + 1) % 3]
            self.btn_status.configure(text=new_s, fg_color=status_colors.get(new_s, "#444"))
            on_status_update(book_data[0], new_s)

        self.btn_status = ctk.CTkButton(self, text=status_val, width=70, height=24, corner_radius=6,
                                        font=("Arial", 10, "bold"), fg_color=status_colors.get(status_val, "#444"),
                                        command=on_status_click)
        self.btn_status.grid(row=0, column=4, padx=10)

        # 6. Actions
        act_frame = ctk.CTkFrame(self, fg_color="transparent")
        act_frame.grid(row=0, column=5, padx=10)
        ctk.CTkButton(act_frame, text="Convert", width=60, height=24, fg_color="#3498DB",
                      command=lambda: on_convert(book_data)).pack(side="left", padx=2)
        ctk.CTkButton(act_frame, text="🗑", width=30, height=24, fg_color="#444", hover_color="red",
                      command=lambda: on_delete(book_data[0])).pack(side="left", padx=2)

        # Scroll
        self._bind_scroll_recursive(self, scroll_cb)

        # Drag Registration
        self._register_drag_recursive(self)

    def _bind_scroll_recursive(self, w, cb):
        try:
            w.bind("<MouseWheel>", cb);
            w.bind("<Button-4>", cb);
            w.bind("<Button-5>", cb)
        except:
            pass
        for c in w.winfo_children(): self._bind_scroll_recursive(c, cb)

    def _register_drag_recursive(self, widget):
        if isinstance(widget, (ctk.CTkButton, ctk.CTkCheckBox, ctk.CTkEntry)): return
        try:
            widget.drag_source_register(1, DND_FILES)
            widget.dnd_bind('<<DragInitCmd>>', self.drag_init)
            if hasattr(widget, '_canvas'):
                widget._canvas.drag_source_register(1, DND_FILES)
                widget._canvas.dnd_bind('<<DragInitCmd>>', self.drag_init)
        except:
            pass
        for child in widget.winfo_children(): self._register_drag_recursive(child)

    def _on_press(self, event):
        self._start_pos = (event.x_root, event.y_root)

    def _on_release(self, event, callback):
        if abs(event.x_root - self._start_pos[0]) < 5 and abs(event.y_root - self._start_pos[1]) < 5:
            callback(self.book_data)

    def drag_init(self, event):
        target = self.winfo_containing(event.x_root, event.y_root)

        def check_badge(w):
            curr = w
            for _ in range(4):
                if not curr: break
                try:
                    txt = str(curr.cget("text"))
                    if ".EPUB" in txt: return "epub"
                    # FIX: Check for 1-BIT and 2-BIT labels as well as .XTC
                    if ".XTC" in txt or "1-BIT" in txt or "2-BIT" in txt: return "xtc"
                except:
                    pass
                curr = curr.master
            return None

        # --- DETECT MODE ---
        mode = "folder"
        badge = check_badge(target)
        if badge:
            mode = badge
        else:
            # Check if target is the icon
            is_cover = False
            if target == self.lbl_icon:
                is_cover = True
            elif target == self.icon_frame:
                is_cover = True
            elif hasattr(self.lbl_icon, '_canvas') and target == self.lbl_icon._canvas:
                is_cover = True
            elif hasattr(target, 'master') and target.master == self.lbl_icon:
                is_cover = True

            if is_cover:
                mode = "cover"

        # Gather Files
        files = []
        app = self.winfo_toplevel()
        books = [self.book_data]
        if hasattr(app, 'selected_ids') and self.book_data[0] in app.selected_ids and len(app.selected_ids) > 1:
            books = [b for b in app.all_books if b[0] in app.selected_ids]

        if mode == "folder":
            temp_root = os.path.join(EXPORT_DIR, "DragTemp")
            os.makedirs(temp_root, exist_ok=True)
            for b in books:
                safe = "".join([c for c in b[1] if c.isalnum() or c in " -_"]).strip()
                b_dir = os.path.join(temp_root, safe)
                os.makedirs(b_dir, exist_ok=True)
                if b[3] and os.path.exists(b[3]): shutil.copy2(b[3], b_dir)
                if b[4] and os.path.exists(b[4]): shutil.copy2(b[4], b_dir)
                if b[5]:
                    try:
                        img = Image.open(io.BytesIO(b[5])).convert("RGB")
                        img = ImageOps.fit(img, (480, 800), method=Image.Resampling.LANCZOS)
                        img = ImageEnhance.Contrast(img.convert("L")).enhance(1.6).convert("1")
                        img.save(os.path.join(b_dir, "cover.bmp"))
                    except:
                        pass
                files.append(b_dir)
        elif mode == "cover":
            for b in books:
                if b[5]:
                    safe = "".join([c for c in b[1] if c.isalnum() or c in " -_"]).strip()
                    out = os.path.join(EXPORT_DIR, f"{safe}_cover.bmp")
                    try:
                        img = Image.open(io.BytesIO(b[5])).convert("RGB")
                        img = ImageOps.fit(img, (480, 800), method=Image.Resampling.LANCZOS)
                        img = ImageEnhance.Contrast(img.convert("L")).enhance(1.6).convert("1")
                        img.save(out)
                        files.append(out)
                    except:
                        pass
        elif mode in ["epub", "xtc"]:
            idx = 3 if mode == "epub" else 4
            for b in books:
                if b[idx] and os.path.exists(b[idx]): files.append(b[idx])

        if files:
            return (('copy',), (DND_FILES,), tuple(files))
        return None


class DeviceRow(ctk.CTkFrame, DnDWrapper):
    def __init__(self, parent, item_data, browser_ref, current_path):
        super().__init__(parent, fg_color="transparent", height=40, corner_radius=6)
        self.TkdndVersion = TkinterDnD._require(self)
        self.browser = browser_ref
        self.item = item_data
        self.full_path = f"{current_path}/{item_data['name']}".replace("//", "/")
        self.is_dir = item_data['type'] == 'dir'

        # Internal state for Click vs Drag
        self._start_pos = (0, 0)
        self.pack_propagate(False)

        # 1. Main Button (Modified to handle Click vs Drag manually)
        icon_char = "📁" if self.is_dir else "📄"
        self.btn_main = ctk.CTkButton(
            self, text=f"  {icon_char}   {item_data['name']}",
            anchor="w", fg_color="transparent", text_color="#DDDDDD",
            hover_color="#333", font=("Arial", 13)
        )
        self.btn_main.place(relx=0, rely=0, relwidth=0.7, relheight=1)

        # Bind the detection logic
        self.btn_main.bind("<ButtonPress-1>", self._on_press)
        self.btn_main.bind("<ButtonRelease-1>", self._on_release)

        # 2. Register for Dragging OUT (Only for files)
        if not self.is_dir:
            self.drag_source_register(1, DND_FILES)
            self.dnd_bind('<<DragInitCmd>>', self.drag_init_out)

        # 3. Size Info
        if not self.is_dir:
            try:
                sz = int(item_data.get('size', 0)) / 1024
                sz_str = f"{sz:.1f} KB"
            except:
                sz_str = "0 KB"
            self.lbl_size = ctk.CTkLabel(self, text=sz_str, text_color="gray", font=("Arial", 11))
            self.lbl_size.place(relx=0.7, rely=0.25)

        # 4. Action Buttons
        self.actions_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.actions_frame.place(relx=0.85, rely=0.1, relheight=0.8)

        ctk.CTkButton(self.actions_frame, text="✎", width=25, fg_color="#F39C12",
                      command=lambda: self.browser.rename_item(self.full_path, item_data['name'])).pack(side="left",
                                                                                                        padx=2)
        ctk.CTkButton(self.actions_frame, text="🗑", width=25, fg_color="#C0392B",
                      command=lambda: self.browser.delete_item(self.full_path)).pack(side="left", padx=2)

        # 5. Drop Target (For folders)
        if self.is_dir:
            self.drop_target_register(DND_FILES)
            self.dnd_bind('<<Drop>>', self.on_drop)

        # Important: Register DND on the button too!
        self._register_drag_on_all(self)

    def _on_press(self, event):
        self._start_pos = (event.x_root, event.y_root)

    def _on_release(self, event):
        dx = abs(event.x_root - self._start_pos[0])
        dy = abs(event.y_root - self._start_pos[1])
        # If moved less than 5px, it's a CLICK
        if dx < 5 and dy < 5:
            if self.is_dir:
                self.browser.load_path(self.full_path)
            else:
                # Optional: Add logic for clicking a file (e.g., preview)
                pass

    def _register_drag_on_all(self, widget):
        """Recursively ensures all sub-widgets can initiate a drag."""
        try:
            if not isinstance(widget, (ctk.CTkButton, ctk.CTkCheckBox, ctk.CTkEntry)):
                widget.drag_source_register(1, DND_FILES)
            # We specifically want the main button to initiate drag
            if widget == self.btn_main:
                widget.drag_source_register(1, DND_FILES)

            widget.dnd_bind('<<DragInitCmd>>', self.drag_init_out)
        except:
            pass
        for child in widget.winfo_children():
            self._register_drag_on_all(child)

    def drag_init_out(self, event):
        if self.is_dir: return None

        filename = self.item['name']
        local_path = os.path.join(EXPORT_DIR, filename)
        self.browser.lbl_status.configure(text="Downloading for drag...", text_color="#E67E22")

        try:
            url = self.browser.get_url(f"/edit?path={self.full_path}")
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                with open(local_path, "wb") as f:
                    f.write(resp.content)
                self.browser.lbl_status.configure(text="Ready to drag", text_color="#2ECC71")
                return (('copy',), (DND_FILES,), (local_path,))
        except Exception as e:
            print(f"Drag error: {e}")
        return None

    def on_drop(self, event):
        self.browser.handle_drop(event, target_folder=self.full_path)


class RemoteDeviceBrowser(ctk.CTkToplevel, DnDWrapper):
    def __init__(self, parent, default_ip="192.168.0.202"):
        super().__init__(parent)
        self.parent_app = parent
        self.title("Device Manager")
        self.geometry("1000x700")
        self.attributes("-topmost", True)
        self.after(10, self.focus_force)
        self.ip = default_ip
        self.current_path = "/"

        # --- Enable Drop Support ---
        self.TkdndVersion = TkinterDnD._require(self)
        self.drop_target_register(DND_FILES)
        self.dnd_bind('<<Drop>>', lambda e: self.handle_drop(e, self.current_path))

        # --- Top Bar ---
        top = ctk.CTkFrame(self, height=60, fg_color="#222")
        top.pack(fill="x")

        ctk.CTkLabel(top, text="DEVICE IP:", font=("Arial", 10, "bold"), text_color="gray").pack(side="left",
                                                                                                 padx=(20, 5))
        self.entry_ip = ctk.CTkEntry(top, width=120, fg_color="#111", border_color="#444")
        self.entry_ip.insert(0, self.ip)
        self.entry_ip.pack(side="left", padx=5)

        ctk.CTkButton(top, text="⟳ Connect", width=100, fg_color="#34495E", command=self.refresh_root).pack(side="left",
                                                                                                            padx=10)
        ctk.CTkButton(top, text="⟳ Refresh", width=100, fg_color="#34495E", command=self.refresh_current_path).pack(
            side="left", padx=10)

        self.lbl_status = ctk.CTkLabel(top, text="Not Connected", font=("Arial", 12, "bold"))
        self.lbl_status.pack(side="right", padx=20)

        # --- Breadcrumb & Actions ---
        nav = ctk.CTkFrame(self, height=40, fg_color="#2B2B2B")
        nav.pack(fill="x", padx=20, pady=(20, 10))

        ctk.CTkButton(nav, text="⬆", width=40, fg_color="#444", command=self.go_up).pack(side="left", padx=5)
        self.lbl_path = ctk.CTkLabel(nav, text="/", font=("Arial", 14, "bold"), text_color="#3498DB")
        self.lbl_path.pack(side="left", padx=10)

        ctk.CTkButton(nav, text="+ New Folder", width=100, fg_color="#27AE60", command=self.create_folder).pack(
            side="right", padx=5)

        # --- Header ---
        header = ctk.CTkFrame(self, height=30, fg_color="transparent")
        header.pack(fill="x", padx=25, pady=(0, 5))
        ctk.CTkLabel(header, text="NAME", width=300, anchor="w", text_color="gray", font=("Arial", 10, "bold")).place(
            relx=0.05, rely=0)
        ctk.CTkLabel(header, text="SIZE", anchor="w", text_color="gray", font=("Arial", 10, "bold")).place(relx=0.7,
                                                                                                           rely=0)
        ctk.CTkLabel(header, text="ACTIONS", anchor="w", text_color="gray", font=("Arial", 10, "bold")).place(relx=0.85,
                                                                                                              rely=0)

        # --- List ---
        self.scroll = ctk.CTkScrollableFrame(self, fg_color="#151515")
        self.scroll.pack(fill="both", expand=True, padx=20, pady=(0, 20))

        self.refresh_root()

    # --- LOGIC ---
    def get_url(self, endpoint):
        return f"http://{self.entry_ip.get().strip()}{endpoint}"

    def refresh_current_path(self):
        self.load_path(self.current_path)
        self.update_storage_status()

    def refresh_root(self):
        self.load_path("/")
        self.update_storage_status()

    def update_storage_status(self):
        threading.Thread(target=self._task_status).start()

    def _task_status(self):
        try:
            resp = requests.get(self.get_url("/status"), timeout=3)
            if resp.status_code == 200:
                data = resp.json()
                total = data.get('totalBytes', 1)
                used = data.get('usedBytes', 0)
                pct = (used / total) * 100
                self.after(0, lambda: self.lbl_status.configure(text=f"{pct:.1f}% Used", text_color="#2ECC71"))
        except:
            pass

    def load_path(self, path):
        self.current_path = path
        self.lbl_path.configure(text=path)
        for w in self.scroll.winfo_children(): w.destroy()

        loading = ctk.CTkLabel(self.scroll, text="Loading...", text_color="gray")
        loading.pack(pady=20)
        threading.Thread(target=lambda: self._fetch_list(path, loading)).start()

    def _fetch_list(self, path, loader):
        try:
            url = self.get_url("/list")
            resp = requests.get(url, params={"dir": path}, timeout=5)
            resp.encoding = 'utf-8'
            data = resp.json()
            data.sort(key=lambda x: (x['type'] == 'file', x['name'].lower()))
            self.after(0, lambda: [loader.destroy(), self._render(data)])
        except Exception as e:
            msg = "Device not found" if "timeout" in str(e).lower() else str(e)
            self.after(0, lambda: loader.configure(text=msg, text_color="#E74C3C"))

    def _render(self, items):
        if not items:
            ctk.CTkLabel(self.scroll, text="Empty Folder", text_color="#444").pack(pady=20)
            return
        for item in items:
            row = DeviceRow(self.scroll, item, self, self.current_path)
            row.pack(fill="x", pady=2)

    # --- DRAG HANDLER ---
    def handle_drop(self, event, target_folder):
        raw_data = event.data
        files = re.findall(r'{(.*?)}|(\S+)', raw_data)
        file_list = [f[0] if f[0] else f[1] for f in files]

        if not file_list: return

        self.lbl_status.configure(text="Analyzing files...", text_color="#E67E22")
        threading.Thread(target=lambda: self._direct_upload_task(file_list, target_folder)).start()

    def _direct_upload_task(self, inputs, root_target):
        ip = self.entry_ip.get().strip()
        base_url = f"http://{ip}"
        is_x4 = "192.168.0.202" in ip or "192.168.3.3" in ip

        # --- Helper: Create Directory ---
        def make_remote_dir(path):
            if not path.endswith("/"): path += "/"  # Critical for X4
            try:
                if is_x4:
                    requests.put(f"{base_url}/edit", data={"path": path}, timeout=5)
                else:
                    requests.post(f"{base_url}/mkdir", data={"name": path.strip("/"), "path": "/"}, timeout=5)
            except:
                pass

        # --- 1. BUILD QUEUE ---
        upload_queue = []

        for item in inputs:
            if os.path.isfile(item):
                remote_file = f"{root_target.rstrip('/')}/{os.path.basename(item)}".replace("//", "/")
                upload_queue.append((item, remote_file))

            elif os.path.isdir(item):
                dirname = os.path.basename(item)
                remote_parent = f"{root_target.rstrip('/')}/{dirname}/".replace("//", "/")
                make_remote_dir(remote_parent)

                for root, dirs, files in os.walk(item):
                    for d in dirs:
                        local_d = os.path.join(root, d)
                        rel = os.path.relpath(local_d, item)
                        make_remote_dir(f"{remote_parent}{rel}/".replace("\\", "/"))

                    for f in files:
                        local_f = os.path.join(root, f)
                        rel = os.path.relpath(local_f, item)
                        remote_f = f"{remote_parent}{rel}".replace("\\", "/").replace("//", "/")
                        upload_queue.append((local_f, remote_f))

        # --- 2. EXECUTE WITH PROGRESS ---
        total_files = len(upload_queue)

        for i, (local, remote) in enumerate(upload_queue):
            fname = os.path.basename(local)

            # This is the callback that was missing/dummy in the previous version
            def progress_cb(cur, tot):
                file_pct = int((cur / tot) * 100)
                # Format: "File 1/5: 45%"
                self.after(0, lambda: self.lbl_status.configure(
                    text=f"File {i + 1}/{total_files}: {file_pct}%",
                    text_color="#3498DB"
                ))

            try:
                # We pass 'progress_cb' here so MultipartStreamer can report back
                streamer = MultipartStreamer(local, 'data', remote, progress_cb)
                headers = {'Content-Type': streamer.content_type}

                if is_x4:
                    requests.post(f"{base_url}/edit", data=streamer, headers=headers, timeout=900)
                else:
                    # Non-X4 usually needs the parent path param
                    remote_dir = os.path.dirname(remote).replace("\\", "/")
                    requests.post(f"{base_url}/upload", params={"path": remote_dir}, data=streamer, headers=headers,
                                  timeout=900)

            except Exception as e:
                print(f"Failed to upload {fname}: {e}")

        # --- FINISH ---
        self.after(0, lambda: [
            self.lbl_status.configure(text="Transfer Complete", text_color="#2ECC71"),
            self.refresh_current_path()
        ])

    def go_up(self):
        if self.current_path == "/": return
        parent = os.path.dirname(self.current_path.rstrip("/"))
        if not parent: parent = "/"
        self.load_path(parent)

    def delete_item(self, path):
        if messagebox.askyesno("Delete", f"Delete {os.path.basename(path)}?"):
            threading.Thread(target=lambda: [
                requests.delete(self.get_url("/edit"), data={"path": path}, timeout=5),
                self.after(0, self.refresh_current_path)
            ]).start()

    def rename_item(self, path, name):
        new = simpledialog.askstring("Rename", "Name:", initialvalue=name)
        if new:
            base = os.path.dirname(path)
            threading.Thread(target=lambda: [
                requests.put(self.get_url("/edit"), data={"src": path, "path": f"{base}/{new}"}, timeout=5),
                self.after(0, self.refresh_current_path)
            ]).start()

    def create_folder(self):
        name = simpledialog.askstring("Folder", "Name:")
        if name:
            path = f"{self.current_path}/{name}/".replace("//", "/")
            threading.Thread(target=lambda: [
                requests.put(self.get_url("/edit"), data={"path": path}, timeout=5),
                self.after(0, self.refresh_current_path)
            ]).start()


class TransferDialog(ctk.CTkToplevel):
    def __init__(self, parent, file_paths, target_folder, app_ref, device_ip, on_complete=None):
        super().__init__(parent)
        self.title("Transfer Options")
        self.geometry("500x550")
        self.files = file_paths
        self.target_folder = target_folder
        self.app = app_ref
        self.ip = device_ip
        self.on_complete = on_complete
        self.transient(parent)
        self.grab_set()

        ctk.CTkLabel(self, text="Transferring Files", font=("Arial", 16, "bold")).pack(pady=(20, 5))
        ctk.CTkLabel(self, text=f"Target: {target_folder}", text_color="gray").pack(pady=(0, 15))

        self.scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.scroll.pack(pady=5, padx=20, fill="both", expand=True)

        self.progress_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.progress_label = ctk.CTkLabel(self.progress_frame, text="Ready", font=("Arial", 12))
        self.progress_label.pack()
        self.progress_bar = ctk.CTkProgressBar(self.progress_frame, width=300)
        self.progress_bar.set(0)
        self.progress_bar.pack(pady=5)

        self.identified_books = []
        self.unknown_files = []

        # [IDENTIFICATION LOGIC REMAIN SAME AS BEFORE]
        detected_mode = None
        db_books = self.app.all_books
        for f_path in self.files:
            found = False
            try:
                f_norm = os.path.normpath(os.path.abspath(f_path)).lower()
            except:
                f_norm = str(f_path).lower()
            for b in db_books:
                try:
                    p_epub = os.path.normpath(os.path.abspath(b[3])).lower() if b[3] else ""
                except:
                    p_epub = ""
                try:
                    p_xtc = os.path.normpath(os.path.abspath(b[4])).lower() if b[4] else ""
                except:
                    p_xtc = ""
                if f_norm == p_epub:
                    self.identified_books.append(b);
                    if not detected_mode: detected_mode = "epub"
                    found = True;
                    break
                elif f_norm == p_xtc:
                    self.identified_books.append(b);
                    if not detected_mode: detected_mode = "xtc"
                    found = True;
                    break
            if not found: self.unknown_files.append(f_path)

        # [UI LIST LOGIC REMAIN SAME AS BEFORE]
        if not detected_mode: detected_mode = "epub"
        self.var_mode = ctk.StringVar(value=detected_mode)

        if self.identified_books:
            f_books = ctk.CTkFrame(self.scroll, fg_color="#222", corner_radius=6)
            f_books.pack(fill="x", pady=5)
            ctk.CTkLabel(f_books, text=f"📚 Library Books ({len(self.identified_books)})", text_color="#3498DB",
                         font=("Arial", 12, "bold"), anchor="w").pack(fill="x", padx=10, pady=5)
            ctk.CTkRadioButton(f_books, text="Original (.EPUB)", variable=self.var_mode, value="epub").pack(anchor="w",
                                                                                                            padx=10,
                                                                                                            pady=2)
            ctk.CTkRadioButton(f_books, text="Converted (.XTC)", variable=self.var_mode, value="xtc").pack(anchor="w",
                                                                                                           padx=10,
                                                                                                           pady=2)
            ctk.CTkRadioButton(f_books, text="Generate Cover (.BMP)", variable=self.var_mode, value="bmp").pack(
                anchor="w", padx=10, pady=2)
            for b in self.identified_books[:3]: ctk.CTkLabel(f_books, text=f"• {b[1]}", text_color="gray",
                                                             anchor="w").pack(fill="x", padx=20)

        if self.unknown_files:
            f_raw = ctk.CTkFrame(self.scroll, fg_color="#2B2B2B", corner_radius=6)
            f_raw.pack(fill="x", pady=5)
            ctk.CTkLabel(f_raw, text=f"📄 Files to Transfer ({len(self.unknown_files)})", text_color="#E67E22",
                         font=("Arial", 12, "bold"), anchor="w").pack(fill="x", padx=10, pady=5)
            for f in self.unknown_files: ctk.CTkLabel(f_raw, text=f"• {os.path.basename(f)}", text_color="#DDD",
                                                      anchor="w").pack(fill="x", padx=20)

        self.btn_transfer = ctk.CTkButton(self, text="Start Transfer", width=200, height=40, fg_color="#27AE60",
                                          command=self.run_transfer)
        self.btn_transfer.pack(side="bottom", pady=20)

    def run_transfer(self):
        self.btn_transfer.configure(state="disabled", text="Transferring...")
        self.progress_frame.pack(side="bottom", pady=(0, 10))
        mode = self.var_mode.get()
        threading.Thread(target=lambda: self._thread_runner(mode)).start()

    def _thread_runner(self, mode):
        # 1. THE UI CALLBACK
        # This function receives updates from the background threads
        def ui_callback(pct, text, current_bytes=None, total_bytes=None):
            # Update the progress bar percentage
            if pct is not None:
                self.after(0, lambda: self.progress_bar.set(pct))

            # Create the MB string (e.g., " (1.2 MB / 4.5 MB)")
            size_info = ""
            if current_bytes is not None and total_bytes is not None:
                cur_mb = current_bytes / (1024 * 1024)
                tot_mb = total_bytes / (1024 * 1024)
                size_info = f" ({cur_mb:.2f} MB / {tot_mb:.2f} MB)"

            # Update the label text
            if text:
                # New file starting: set the filename + MB
                self.after(0, lambda t=text, s=size_info: self.progress_label.configure(text=f"{t}{s}"))
            elif size_info:
                # Existing file uploading: update just the MB numbers
                # We strip the old MB info and append the new one
                current_full_text = self.progress_label.cget("text")
                base_text = current_full_text.split(" (")[0]
                self.after(0, lambda b=base_text, s=size_info: self.progress_label.configure(text=f"{b}{s}"))

        # 2. RUN BOOK TRANSFERS (Identified Books)
        if self.identified_books:
            book_ids = [b[0] for b in self.identified_books]
            self.app._task_wireless_send(
                mode, "Custom", self.ip,
                override_folder=self.target_folder,
                override_ids=book_ids,
                progress_callback=ui_callback  # Pass the callback here
            )

        # 3. RUN RAW TRANSFERS (Unknown / Generated Covers)
        if self.unknown_files:
            self._send_raw(self.unknown_files, ui_callback)

        # 4. FINAL REFRESH AND CLOSE
        def finish():
            if self.on_complete:
                self.on_complete()  # This refreshes the device file list
            self.destroy()

        self.after(0, finish)

    def _send_raw(self, files, cb):
        base_url = f"http://{self.ip}"
        total = len(files)
        is_x4 = "192.168.3.3" in self.ip or "192.168.0.202" in self.ip

        try:
            if is_x4:
                requests.put(f"{base_url}/edit", data={"path": self.target_folder}, timeout=5)
            else:
                requests.post(f"{base_url}/mkdir", data={"name": self.target_folder.strip("/"), "path": "/"}, timeout=5)
        except:
            pass

        for i, f in enumerate(files):
            if not os.path.isfile(f): continue
            fname = os.path.basename(f)
            remote = f"{self.target_folder.rstrip('/')}/{fname}".replace("//", "/")

            # Initial call to set the filename in the UI
            cb((i / total), f"Sending: {fname}")

            try:
                # The streamer callback now passes bytes to the UI callback 'cb'
                def update_prog(cur, tot):
                    pct = (i + (cur / tot)) / total
                    cb(pct, None, current_bytes=cur, total_bytes=tot)

                streamer = MultipartStreamer(f, 'data', remote, update_prog)
                headers = {'Content-Type': streamer.content_type}

                if is_x4:
                    requests.post(f"{base_url}/edit", data=streamer, headers=headers, timeout=900)
                else:
                    requests.post(f"{base_url}/upload", params={"path": self.target_folder}, data=streamer,
                                  headers=headers, timeout=900)
            except Exception as e:
                print(f"Error: {e}")

# --- MAIN APP ---
class XlibreApp(ctk.CTk, DnDWrapper):
    def __init__(self):
        super().__init__()
        # 1. Load User Preferences FIRST
        self.config = load_app_config()

        # 2. Initialize the Drag & Drop System
        self.TkdndVersion = TkinterDnD._require(self)

        # 3. Setup UI Variables using the loaded config
        self.status_filter_var = ctk.StringVar(value=self.config.get("default_status", "All Statuses"))
        self.sort_var = ctk.StringVar(value=self.config.get("default_sort", "Date Added"))

        # 4. Window Setup
        self.title("Xlibre Library Manager")
        self.geometry("1500x800")

        # 5. Database Setup (Uses BASE_DIR initialized at top of script)
        db_path = os.path.join(BASE_DIR, "xlibre.db")
        self.db = database.LibraryDB(db_path)

        # 6. View Mode from config
        self.view_mode = self.config.get("view_mode", "grid")

        self.selected_ids = set()
        self.current_editor = None
        self.cols = 5
        self.resize_timer = None

        self.bind("<Button-1>", self.on_global_click)
        self.bind("<Configure>", self.on_resize)

        # --- TOOLBAR ---
        tb = ctk.CTkFrame(self, fg_color="#1a1a1a", corner_radius=0)
        tb.pack(fill="x", side="top")

        tb_top = ctk.CTkFrame(tb, fg_color="transparent")
        tb_top.pack(fill="x", side="top", pady=(10, 5))

        # BRANDING AS SETTINGS BUTTON
        self.btn_settings = ctk.CTkButton(
            tb_top,
            text="",
            fg_color="transparent",
            hover_color="#222",
            width=150,
            height=55,
            corner_radius=8,
            command=self.open_settings
        )
        self.btn_settings.pack(side="left", padx=(20, 10))

        # We use .place instead of .pack here to avoid the grid/pack conflict
        title_box = ctk.CTkFrame(self.btn_settings, fg_color="transparent")
        title_box.place(relx=0.5, rely=0.5, anchor="center")

        # Labels inside the frame can use .pack safely
        l1 = ctk.CTkLabel(title_box, text="XLIBRE", font=("Arial", 24, "bold"), text_color="#3498DB")
        l1.pack()
        l2 = ctk.CTkLabel(title_box, text="MANAGER", font=("Arial", 10, "bold"), text_color="#555")
        l2.pack(pady=(0, 2))

        # Ensure labels don't intercept the click intended for the button
        l1.bind("<Button-1>", lambda e: self.open_settings())
        l2.bind("<Button-1>", lambda e: self.open_settings())
        self.create_icon_button(tb_top, "＋ Import", "#2ECC71", self.import_book_dialog)
        self.create_icon_button(tb_top, "☑ Select All", "#95A5A6", self.toggle_select_all)
        btn_text = "▤ List View" if self.view_mode == "grid" else "▦ Grid View"
        self.btn_view = self.create_icon_button(tb_top, btn_text, "#7F8C8D", self.toggle_view_mode)

        self.create_divider(tb_top)

        self.btn_conv = self.create_icon_button(tb_top, "⚡ Convert", "#D35400", self.open_batch_convert, "disabled")
        self.btn_fetch = self.create_icon_button(tb_top, "☁ Fetch", "#8E44AD", self.batch_fetch, "disabled")
        self.btn_send = self.create_icon_button(tb_top, "📲 Send", "#2980B9", self.open_send_dialog, "disabled")
        self.create_icon_button(tb_top, "📟 Device Mgr", "#34495E", self.open_device_manager)
        self.create_divider(tb_top)
        self.btn_del = self.create_icon_button(tb_top, "🗑 Delete", "#C0392B", self.batch_delete, "disabled")

        right = ctk.CTkFrame(tb_top, fg_color="transparent")
        right.pack(side="right", padx=20)

        self.status_filter_var = ctk.StringVar(value="All Statuses")
        ctk.CTkOptionMenu(right, variable=self.status_filter_var, width=110,
                          values=["All Statuses", "Unread", "Reading", "Finished"],
                          command=self.refresh_library, fg_color="#2B2B2B", button_color="#333").pack(side="right",
                                                                                                      padx=(10, 0))

        self.sort_var = ctk.StringVar(value="Date Added")
        ctk.CTkOptionMenu(right, variable=self.sort_var, width=130, values=["Date Added", "Title", "Author", "Genre"],
                          command=self.refresh_library, fg_color="#333", button_color="#444").pack(side="right",
                                                                                                   padx=(10, 0))

        self.entry_search = ctk.CTkEntry(right, width=220, height=35, corner_radius=20, border_width=1,
                                         border_color="#444", fg_color="#111", text_color="gray")
        self.entry_search.pack(side="right")
        self.entry_search.insert(0, "Search title or author...")
        self.entry_search.bind("<FocusIn>", self.on_search_focus_in)
        self.entry_search.bind("<FocusOut>", self.on_search_focus_out)
        self.entry_search.bind("<KeyRelease>", self.filter_books)

        # --- ROW 2: BATCH PROGRESS BAR ---
        self.progress_frame = ctk.CTkFrame(tb, fg_color="transparent", height=15)
        self.progress_frame.pack(fill="x", side="top", padx=20, pady=(0, 10))

        self.progress_label = ctk.CTkLabel(self.progress_frame, text="", font=("Arial", 11, "bold"), text_color="gray",
                                           height=15)
        self.progress_label.pack(side="left")

        self.progress_bar = ctk.CTkProgressBar(self.progress_frame, height=6, progress_color="#8E44AD")
        self.progress_bar.set(0)
        self.progress_bar.pack(side="left", fill="x", expand=True, padx=(10, 0))

        # GRID
        self.main_container = ctk.CTkFrame(self, fg_color="#111")
        self.main_container.pack(fill="both", expand=True)
        self.scroll = ctk.CTkScrollableFrame(self.main_container, fg_color="transparent")
        self.scroll.pack(fill="both", expand=True, padx=20, pady=20)

        self.all_books = []
        self.current_display = []
        self.after(100, self.refresh_library)

        self.setup_drag_and_drop()

    def open_settings(self):
        """Open the settings dialog and refresh the library state when finished."""
        SettingsDialog(self)
        # Reload local config reference for sorting/view changes
        self.config = load_app_config()
        self.refresh_library()

    def setup_drag_and_drop(self):
        """Configure the MAIN WINDOW to accept dropped files."""
        # Registers the entire window (self) as the drop target
        self.drop_target_register(DND_FILES)

        self.dnd_bind('<<DragEnter>>', self.on_drag_enter)
        self.dnd_bind('<<DragLeave>>', self.on_drag_leave)
        self.dnd_bind('<<Drop>>', self.handle_file_drop)

    def on_drag_enter(self, event):
        # Visual feedback: Turn the scroll background green
        try:
            self.scroll.configure(fg_color="#27AE60")
        except:
            pass

    def on_drag_leave(self, event):
        # Reset visual feedback
        try:
            self.scroll.configure(fg_color="transparent")
        except:
            pass

    def handle_file_drop(self, event):
        # 1. Reset color
        self.on_drag_leave(event)

        # 2. Parse paths (handle Windows {} wrapping)
        raw_data = event.data
        files = re.findall(r'{(.*?)}|(\S+)', raw_data)
        file_list = [f[0] if f[0] else f[1] for f in files]

        # 3. Filter and Import
        epubs = [f for f in file_list if f.lower().endswith('.epub')]
        if epubs:
            self.progress_label.configure(text=f"Importing {len(epubs)} books...")
            threading.Thread(target=lambda: self._process_imports(epubs)).start()
        else:
            messagebox.showwarning("Invalid File", "Please drop .epub files only.")

    def handle_file_drop(self, event):
        """Parse dropped files and trigger import."""
        # event.data contains the paths.
        # Windows often wraps paths with spaces in {brackets}.
        raw_data = event.data

        # regex to extract paths even if they are in { }
        files = re.findall(r'{(.*?)}|(\S+)', raw_data)
        file_list = [f[0] if f[0] else f[1] for f in files]

        # Filter for EPUBs only
        epubs = [f for f in file_list if f.lower().endswith('.epub')]

        if epubs:
            self.progress_label.configure(text=f"Importing {len(epubs)} books...")
            threading.Thread(target=lambda: self._process_imports(epubs)).start()
        else:
            messagebox.showwarning("Invalid File", "Please drop .epub files only.")

    def toggle_view_mode(self):
        if self.view_mode == "grid":
            self.view_mode = "list"
            self.btn_view.configure(text="▦ Grid View")
        else:
            self.view_mode = "grid"
            self.btn_view.configure(text="▤ List View")

        # NEW: Save to DB immediately
        self.db.set_config("view_mode", self.view_mode)

        self.refresh_library()

    def create_icon_button(self, parent, text, col, cmd, state="normal"):
        b = ctk.CTkButton(parent, text=text, command=cmd, state=state, width=100, height=35, corner_radius=8,
                          font=("Arial", 12, "bold"), fg_color="#2B2B2B", hover_color=col)
        b.pack(side="left", padx=5)
        return b

    def create_divider(self, parent):
        ctk.CTkFrame(parent, width=2, height=30, fg_color="#333").pack(side="left", padx=10, pady=10)

    def on_global_click(self, event):
        try:
            if event.widget != self.entry_search._entry: self.focus()
        except:
            self.focus()

    def on_search_focus_in(self, event):
        if self.entry_search.get() == "Search title or author...":
            self.entry_search.delete(0, "end")
            self.entry_search.configure(text_color=("black", "white"))

    def on_search_focus_out(self, event):
        if not self.entry_search.get().strip():
            self.entry_search.delete(0, "end")
            self.entry_search.insert(0, "Search title or author...")
            self.entry_search.configure(text_color="gray")

    def on_resize(self, event):
        if event.widget != self: return
        if self.resize_timer: self.after_cancel(self.resize_timer)
        self.resize_timer = self.after(150, lambda: self._process_resize(self.winfo_width()))

    def _process_resize(self, width):
        safe_width = width if width > 200 else 1200
        new_cols = max(1, safe_width // 220)
        if new_cols != self.cols:
            self.cols = new_cols
            # CHANGE THIS LINE:
            # Old: self.display_books(self.current_display)
            # New:
            self.display_grouped_books()

    def update_book_status(self, bid, new_status):
        self.db.update_book_status(bid, new_status)
        for i, b in enumerate(self.all_books):
            if b[0] == bid:
                lst = list(b)
                lst[-1] = new_status
                self.all_books[i] = tuple(lst)
                break

    def refresh_library(self, _=None):
        self.all_books = self.db.get_all_books()
        choice = self.sort_var.get()
        status_filter = self.status_filter_var.get()

        def get_date_label(date_str):
            if not date_str: return "Unknown Date"
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
                now = datetime.now()
                diff = now - dt
                if diff.days == 0: return "Today"
                if diff.days == 1: return "Yesterday"
                if diff.days < 7: return "This Week"
                if diff.days < 14: return "Last Week"
                if diff.days < 30: return "This Month"
                if diff.days < 365: return "This Year"
                return "Older"
            except:
                return "Unknown Date"

        groupers = {
            "Author": lambda x: x[2] if x[2] else "Unknown Author",
            "Genre": lambda x: x[8] if x[8] else "Uncategorized",
            "Title": lambda x: x[1][0].upper() if x[1] else "#",
            "Date Added": lambda x: get_date_label(x[11])
        }

        key_map = {
            "Title": lambda x: x[1].lower(),
            "Author": lambda x: x[2].lower(),
            "Genre": lambda x: (x[8] or "z").lower(),
            "Date Added": lambda x: x[11]
        }

        sort_key = key_map.get(choice, lambda x: x[11])
        reverse_sort = True if choice == "Date Added" else False
        self.all_books.sort(key=sort_key, reverse=reverse_sort)

        q = self.entry_search.get().lower().strip()
        if q == "search title or author...": q = ""

        self.current_display = []
        for b in self.all_books:
            b_status = b[-1] if len(b) > 12 else "Unread"
            if q and q not in b[1].lower() and q not in b[2].lower(): continue
            if status_filter != "All Statuses" and b_status != status_filter: continue
            self.current_display.append(b)

        self.grouped_data = {}
        grouper = groupers.get(choice, lambda x: "All Books")

        for book in self.current_display:
            group_name = grouper(book)
            if group_name not in self.grouped_data: self.grouped_data[group_name] = []
            self.grouped_data[group_name].append(book)

        self.display_grouped_books()

    def filter_books(self, *args):
        self.refresh_library()

    def display_grouped_books(self):
        # Clear current children
        for w in self.scroll.winfo_children(): w.destroy()

        # Reset Grid Configuration
        for i in range(50): self.scroll.grid_columnconfigure(i, weight=0)

        if self.view_mode == "grid":
            # --- GRID LOGIC ---
            for i in range(self.cols): self.scroll.grid_columnconfigure(i, weight=1)

            current_row = 0
            for group_name, books in self.grouped_data.items():
                if not books: continue
                if group_name != "All Books":
                    header_frame = ctk.CTkFrame(self.scroll, fg_color="transparent")
                    header_frame.grid(row=current_row, column=0, columnspan=self.cols, sticky="ew", pady=(20, 5))
                    ctk.CTkLabel(header_frame, text=group_name, font=("Arial", 18, "bold"), text_color="#3498DB",
                                 anchor="w").pack(side="left", padx=10)
                    ctk.CTkFrame(header_frame, height=2, fg_color="#333").pack(side="left", fill="x", expand=True,
                                                                               padx=10)
                    current_row += 1

                for i, b in enumerate(books):
                    row_offset = i // self.cols
                    col_pos = i % self.cols
                    c = BookCard(self.scroll, b, self.launch_editor, self.show_details, self.delete_book,
                                 self.handle_select, self.delete_single_file, self.update_book_status,
                                 self._on_mouse_wheel)
                    if b[0] in self.selected_ids: c.var.set(True)
                    c.grid(row=current_row + row_offset, column=col_pos, padx=10, pady=10, sticky="nsew")

                rows_used = (len(books) - 1) // self.cols + 1
                current_row += rows_used

        else:
            # --- LIST LOGIC ---
            self.scroll.grid_columnconfigure(0, weight=1)  # One wide column

            current_row = 0
            for group_name, books in self.grouped_data.items():
                if not books: continue

                # Group Header
                if group_name != "All Books":
                    header_frame = ctk.CTkFrame(self.scroll, fg_color="transparent")
                    header_frame.grid(row=current_row, column=0, sticky="ew", pady=(15, 5))
                    ctk.CTkLabel(header_frame, text=group_name, font=("Arial", 16, "bold"), text_color="#3498DB").pack(
                        side="left", padx=5)
                    ctk.CTkFrame(header_frame, height=1, fg_color="#444").pack(side="left", fill="x", expand=True,
                                                                               padx=10)
                    current_row += 1

                # List Rows
                for b in books:
                    c = BookRow(self.scroll, b, self.launch_editor, self.show_details, self.delete_book,
                                self.handle_select, self.delete_single_file, self.update_book_status,
                                self._on_mouse_wheel)
                    if b[0] in self.selected_ids: c.var.set(True)
                    c.grid(row=current_row, column=0, padx=5, pady=2, sticky="ew")
                    current_row += 1

        self.scroll.update_idletasks()

    def display_books(self, books):
        for w in self.scroll.winfo_children(): w.destroy()

        if self.view_mode == "grid":
            for i in range(50): self.scroll.grid_columnconfigure(i, weight=0)
            for i in range(self.cols): self.scroll.grid_columnconfigure(i, weight=1)
            for i, b in enumerate(books):
                c = BookCard(self.scroll, b, self.launch_editor, self.show_details, self.delete_book,
                             self.handle_select,
                             self.delete_single_file, self.update_book_status, self._on_mouse_wheel)
                if b[0] in self.selected_ids: c.var.set(True)
                c.grid(row=i // self.cols, column=i % self.cols, padx=10, pady=10, sticky="nsew")
        else:
            self.scroll.grid_columnconfigure(0, weight=1)
            for i, b in enumerate(books):
                c = BookRow(self.scroll, b, self.launch_editor, self.show_details, self.delete_book, self.handle_select,
                            self.delete_single_file, self.update_book_status, self._on_mouse_wheel)
                if b[0] in self.selected_ids: c.var.set(True)
                c.grid(row=i, column=0, padx=5, pady=2, sticky="ew")

        self.scroll.update_idletasks()

    def _on_mouse_wheel(self, event):
        if event.num == 5 or event.delta < 0:
            self.scroll._parent_canvas.yview_scroll(1, "units")
        elif event.num == 4 or event.delta > 0:
            self.scroll._parent_canvas.yview_scroll(-1, "units")

    def handle_select(self, bid, val):
        if val:
            self.selected_ids.add(bid)
        else:
            self.selected_ids.discard(bid)
        self.update_toolbar()

    def toggle_select_all(self):
        # Update check to include BookRow
        cards = [w for w in self.scroll.winfo_children() if isinstance(w, (BookCard, BookRow))]
        if not cards: return
        should_select = any(not c.var.get() for c in cards)
        for c in cards:
            c.var.set(should_select)
            if should_select:
                self.selected_ids.add(c.book_data[0])
            else:
                self.selected_ids.discard(c.book_data[0])
        self.update_toolbar()

    def update_toolbar(self):
        c = len(self.selected_ids)
        st = "normal" if c > 0 else "disabled"
        self.btn_conv.configure(state=st, text=f"⚡ Convert ({c})" if c > 0 else "⚡ Convert")
        self.btn_fetch.configure(state=st, text=f"☁ Fetch ({c})" if c > 0 else "☁ Fetch")
        self.btn_del.configure(state=st, text=f"🗑 Delete ({c})" if c > 0 else "🗑 Delete")
        self.btn_send.configure(state=st, text=f"📲 Send ({c})" if c > 0 else "📲 Send")

    def delete_single_file(self, bid, ftype):
        b = next((x for x in self.all_books if x[0] == bid), None)
        if not b: return
        path = b[4] if ftype == "xtc" else b[3]
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except:
                pass
        if ftype == "xtc":
            self.db.update_xtc_path(bid, "")
        else:
            self.db.conn.cursor().execute("UPDATE books SET path_epub = ? WHERE id = ?", ("", bid))
            self.db.conn.commit()
        other = b[3] if ftype == "xtc" else b[4]
        if not other or not os.path.exists(other): self.db.delete_book(bid)
        self.refresh_library()

    def delete_book(self, bid):
        if messagebox.askyesno("Confirm", "Delete this book?"):
            b = next((x for x in self.all_books if x[0] == bid), None)
            if b:
                if b[3] and os.path.exists(b[3]):
                    try:
                        os.remove(b[3])
                    except:
                        pass
                if b[4] and os.path.exists(b[4]):
                    try:
                        os.remove(b[4])
                    except:
                        pass
            self.db.delete_book(bid)
            self.selected_ids.discard(bid)
            self.refresh_library()
            self.update_toolbar()

    def batch_delete(self):
        if not messagebox.askyesno("Confirm", f"Delete {len(self.selected_ids)} books?"): return
        for bid in self.selected_ids:
            b = next((x for x in self.all_books if x[0] == bid), None)
            if b:
                if b[3] and os.path.exists(b[3]):
                    try:
                        os.remove(b[3])
                    except:
                        pass
                if b[4] and os.path.exists(b[4]):
                    try:
                        os.remove(b[4])
                    except:
                        pass
            self.db.delete_book(bid)
        self.selected_ids.clear()
        self.refresh_library()
        self.update_toolbar()

    def batch_fetch(self):
        if not messagebox.askyesno("Confirm", f"Fetch metadata for {len(self.selected_ids)} books?"): return
        self.btn_fetch.configure(state="disabled")
        self.progress_label.configure(text="Starting Metadata Fetch...")
        self.progress_bar.set(0)
        threading.Thread(target=self._task_batch_fetch).start()

    def _task_batch_fetch(self):
        total = len(self.selected_ids)
        for i, bid in enumerate(list(self.selected_ids)):
            b = next((x for x in self.all_books if x[0] == bid), None)
            if not b: continue
            safe_title = b[1][:15] + "..." if len(b[1]) > 15 else b[1]

            self.after(0, lambda c=i, t=total, name=safe_title: [
                self.progress_bar.set(c / max(1, t)),
                self.progress_label.configure(text=f"Fetching ({c + 1}/{t}): {name}")
            ])

            res = UnifiedMetadataFetcher.search_and_merge(b[1], b[2])
            curr = b

            def pick(o, n):
                return n if (not o or str(o).lower() in ["unknown", "unknown author", "no description found.",
                                                         ""]) else o

            self.db.update_book_details(bid, pick(curr[7], res["description"]), pick(curr[8], res["categories"]),
                                        pick(curr[9], res["publisher"]), pick(curr[10], res["publishedDate"]),
                                        curr[5] if curr[5] else res["cover_blob"])
            time.sleep(0.5)

        self.after(0, lambda: [
            self.progress_bar.set(1.0),
            self.progress_label.configure(text="Ready"),
            self.progress_bar.set(0),
            self.refresh_library(),
            self.update_toolbar(),
            messagebox.showinfo("Done", f"Metadata fetch complete for {total} books.")
        ])

    def execute_wireless_send(self, mode, device_type, ip):
        if not self.selected_ids: return
        self.btn_send.configure(state="disabled")
        self.progress_label.configure(text="Connecting to device...")
        self.progress_bar.set(0)
        threading.Thread(target=self._task_wireless_send, args=(mode, device_type, ip)).start()

    # --- UPDATED SEND TASK: Based on the HTML logic ---
        # --- FIXED SEND TASK ---
        # FIND THIS METHOD in XlibreApp and REPLACE it with this version
        # REPLACE THIS METHOD INSIDE XlibreApp CLASS
    def _task_wireless_send(self, mode, device_type, ip, override_folder=None, override_ids=None,
                            progress_callback=None):
        # 1. SETUP TARGETS
        target_ids = override_ids if override_ids else list(self.selected_ids)
        total_files = len(target_ids)
        success_count = 0
        ip = ip.strip()
        is_x4 = "192.168.0.202" in ip or "X4" in device_type
        base_url = f"http://{ip}"

        # 2. DEFINE PROGRESS HANDLER
        # If no external callback (Dialog) is provided, use the Main Window's bar
        if not progress_callback:
            def _default_prog(val, text=None):
                self.after(0, lambda: self.progress_bar.set(val))
                if text: self.after(0, lambda: self.progress_label.configure(text=text))

            progress_callback = _default_prog

        # 3. FOLDER LOGIC
        if override_folder:
            target_folder_path = override_folder
        else:
            target_folder_path = "/Covers/" if mode == "bmp" else "/send-to-device/"
            try:
                if is_x4:
                    requests.put(f"{base_url}/edit", data={"path": target_folder_path}, timeout=5)
                else:
                    requests.post(f"{base_url}/mkdir", data={"name": target_folder_path.strip("/"), "path": "/"},
                                  timeout=5)
            except:
                pass

        if not target_folder_path.endswith("/"): target_folder_path += "/"
        if not target_folder_path.startswith("/"): target_folder_path = "/" + target_folder_path

        # 4. TRANSFER LOOP
        for i, bid in enumerate(target_ids):
            b = next((x for x in self.all_books if x[0] == bid), None)
            if not b: continue

            safe_title = "".join([c for c in b[1] if c.isalnum() or c in " -_"])
            file_name = f"{safe_title}.{mode}"
            src = b[4] if mode == "xtc" else b[3]
            temp_path = None

            # Update Label
            progress_callback(i / total_files, f"Sending: {file_name}")

            # [BMP GENERATION LOGIC - SAME AS BEFORE]
            if mode == "bmp":
                epub_path = b[3]
                img_obj = None
                if epub_path and os.path.exists(epub_path):
                    try:
                        book = epub.read_epub(epub_path)
                        temp_proc = converter.EpubProcessor()
                        img_obj = temp_proc._find_cover_image(book)
                    except:
                        pass
                if not img_obj and b[5]: img_obj = Image.open(io.BytesIO(b[5]))

                if img_obj:
                    try:
                        img = ImageOps.fit(img_obj, (480, 800), method=Image.Resampling.LANCZOS,
                                           centering=(0.5, 0.5))
                        img = ImageEnhance.Contrast(img.convert("L")).enhance(1.6)
                        img = img.convert("1", dither=Image.Dither.FLOYDSTEINBERG)
                        temp_path = os.path.join(EXPORT_DIR, f"send_cv_{bid}.bmp")
                        img.save(temp_path, "BMP")
                        src = temp_path
                    except:
                        continue
                else:
                    continue

            # [UPLOAD LOGIC]
            if src and os.path.exists(src):
                try:
                    remote_name = f"{target_folder_path}{file_name}".replace("//", "/")

                    # Callback for the Streamer (updates percentage within the current file)
                    def update_stream_prog(cur, tot):
                        pct = (i + (cur / tot)) / total_files
                        # Pass bytes to the external callback
                        progress_callback(pct, None, current_bytes=cur, total_bytes=tot)

                    streamer = MultipartStreamer(src, 'data', remote_name, update_stream_prog)
                    headers = {'Content-Type': streamer.content_type}

                    if is_x4:
                        requests.post(f"{base_url}/edit", data=streamer, headers=headers, timeout=900)
                    else:
                        requests.post(f"{base_url}/upload", params={"path": target_folder_path}, data=streamer,
                                      headers=headers, timeout=900)

                    success_count += 1
                except Exception as e:
                    print(f"Error: {e}")
                finally:
                    if temp_path and os.path.exists(temp_path): os.remove(temp_path)

        # Final Update
        progress_callback(1.0, "Ready")
        # If running from Main App (no override folder), show popup
        if not override_folder:
            self.after(0, lambda: [
                self.progress_bar.set(0),
                messagebox.showinfo("Transfer Complete", f"Sent {success_count} files.")
            ])

    def open_batch_convert(self):
        if not self.selected_ids: return
        BatchConvertDialog(self, self.selected_ids, self.all_books, self.refresh_library)

    def launch_editor(self, data):
        if self.current_editor and self.current_editor.winfo_exists(): self.current_editor.destroy()
        # Instantiate the IntegratedEditor, pointing to this specific book
        self.current_editor = IntegratedEditor(data, self.db, self.refresh_library, self)

    def show_details(self, data):
        BookDetailsWindow(self, data, self.launch_editor, self.db, self.refresh_library)

    def import_book_dialog(self):
        files = filedialog.askopenfilenames(filetypes=[("EPUB", "*.epub")])
        if files: threading.Thread(target=lambda: self._process_imports(files)).start()

    def _process_imports(self, files):
        dupes = 0
        os.makedirs(LIBRARY_DIR, exist_ok=True)
        for f in files:
            try:
                book = epub.read_epub(f)

                def get(n, k, d=""):
                    try:
                        return str(book.get_metadata(n, k)[0][0])
                    except:
                        return d

                t = get('DC', 'title', os.path.basename(f).replace(".epub", ""))
                a = get('DC', 'creator', "Unknown Author")
                existing = next((b for b in self.all_books if b[1] == t and b[2] == a), None)
                if existing: dupes += 1; continue
                d = BeautifulSoup(get('DC', 'description', ""), "html.parser").get_text()
                c = None
                try:
                    c_id = book.get_metadata('OPF', 'cover')
                    if c_id: item = book.get_item_with_id(c_id[0][1]); c = Image.open(io.BytesIO(item.get_content()))
                    if not c:
                        items = [x for x in book.get_items() if x.get_type() == ebooklib.ITEM_COVER]
                        if items: c = Image.open(io.BytesIO(items[0].get_content()))
                    if not c:
                        for i in book.get_items_of_type(ebooklib.ITEM_IMAGE):
                            if 'cover' in i.get_name().lower():
                                c = Image.open(io.BytesIO(i.get_content()));
                                break
                except:
                    pass

                safe_title = "".join([char for char in t if char.isalnum() or char in " -_"]).strip()
                if not safe_title: safe_title = f"book_{int(time.time())}"
                dest_path = os.path.join(LIBRARY_DIR, f"{safe_title}.epub")
                counter = 1
                while os.path.exists(dest_path):
                    dest_path = os.path.join(LIBRARY_DIR, f"{safe_title}_{counter}.epub")
                    counter += 1
                shutil.copy2(f, dest_path)
                self.db.add_book(dest_path, t, a, d, "", "", "", c)
            except Exception as e:
                print(f"Failed to import {f}: {e}")
        self.after(0, self.refresh_library)
        if dupes > 0: self.after(0, lambda: messagebox.showwarning("Duplicates",
                                                                   f"Skipped {dupes} books (already in library)."))

    def open_device_manager(self):
        """Opens Device Manager using the IP address from settings."""
        # Reload config in case it was changed without a restart
        conf = load_app_config()
        RemoteDeviceBrowser(self, default_ip=conf.get("device_ip", "192.168.0.202"))

    def open_send_dialog(self):
        d = ctk.CTkToplevel(self)
        d.title("Send to Device")
        d.geometry("420x480")
        d.transient(self)
        d.grab_set()



        ctk.CTkLabel(d, text="1. Select Format", font=("Arial", 14, "bold")).pack(pady=(15, 5))
        var_fmt = ctk.StringVar(value="xtc")
        f_fmt = ctk.CTkFrame(d, fg_color="transparent")
        f_fmt.pack()
        ctk.CTkRadioButton(f_fmt, text="Converted (.XTC)", variable=var_fmt, value="xtc").pack(side="left", padx=10)
        ctk.CTkRadioButton(f_fmt, text="Original (.EPUB)", variable=var_fmt, value="epub").pack(side="left", padx=10)
        ctk.CTkRadioButton(f_fmt, text="Cover (.BMP)", variable=var_fmt, value="bmp").pack(side="left", padx=10)

        ctk.CTkLabel(d, text="2. Choose Transfer Method", font=("Arial", 14, "bold")).pack(pady=(15, 0))
        tabs = ctk.CTkTabview(d, width=360, height=250)
        tabs.pack(pady=5, padx=20, fill="both", expand=True)

        # --- Wi-Fi Tab ---
        tab_wifi = tabs.add("Wireless (Wi-Fi)")
        tab_usb = tabs.add("USB Cable")

        ctk.CTkLabel(tab_wifi, text="Device Profile:").pack(pady=(10, 2))

        var_device = ctk.StringVar(value="Custom / Home Network")
        device_opt = ctk.CTkOptionMenu(tab_wifi, variable=var_device,
                                       values=["Custom / Home Network", "X4 Hotspot (192.168.3.3)",
                                               "CrossPoint Hotspot (192.168.4.1)"])
        device_opt.pack()

        ctk.CTkLabel(tab_wifi, text="IP Address:").pack(pady=(10, 2))

        # Create entry only ONCE
        entry_ip = ctk.CTkEntry(tab_wifi, width=150, justify="center")

        # Insert the IP from your global settings config
        entry_ip.insert(0, self.config.get("device_ip", "192.168.0.202"))
        entry_ip.pack()

        def update_ip(choice):
            entry_ip.delete(0, 'end')
            if "X4" in choice:
                entry_ip.insert(0, "192.168.3.3")
            elif "CrossPoint" in choice:
                entry_ip.insert(0, "192.168.4.1")
            else:
                # Revert to your custom default IP from settings
                entry_ip.insert(0, self.config.get("device_ip", "192.168.0.202"))

        device_opt.configure(command=update_ip)

        # --- FIX STARTS HERE ---
        def _confirm_wifi_send():
            # 1. Get values BEFORE destroying the window
            fmt = var_fmt.get()
            dev = var_device.get()
            ip = entry_ip.get()

            # 2. Destroy the window
            d.destroy()

            # 3. Execute the function
            self.execute_wireless_send(fmt, dev, ip)

        ctk.CTkButton(tab_wifi, text="Send via Wi-Fi", fg_color="#2980B9",
                      command=_confirm_wifi_send).pack(pady=20)
        # --- FIX ENDS HERE ---

        # --- USB Tab ---
        var_cov = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(tab_usb, text="Include Covers (Creates /Covers folder)", variable=var_cov).pack(pady=30)

        # We also fix the USB button just to be safe, though BooleanVars usually survive better
        def _confirm_usb_send():
            fmt = var_fmt.get()
            inc_covers = var_cov.get()
            d.destroy()
            self.execute_send(fmt, inc_covers)

        ctk.CTkButton(tab_usb, text="Select Folder & Send", fg_color="#27AE60",
                      command=_confirm_usb_send).pack(pady=10)

    def execute_send(self, mode, inc_covers):
        target = filedialog.askdirectory(title="Device Folder")
        if not target: return
        if inc_covers: os.makedirs(os.path.join(target, "Covers"), exist_ok=True)
        cnt = 0
        for bid in self.selected_ids:
            b = next((x for x in self.all_books if x[0] == bid), None)
            if not b: continue
            src = b[4] if mode == "xtc" else b[3]
            if src and os.path.exists(src):
                safe = "".join([c for c in b[1] if c.isalnum() or c in " -_"])
                try:
                    shutil.copy2(src, os.path.join(target, f"{safe}.{mode}"))
                    if inc_covers and b[5]:
                        img = Image.open(io.BytesIO(b[5])).convert("L")
                        img = ImageEnhance.Contrast(img).enhance(1.3).resize((480, 800)).convert("1",
                                                                                                 dither=Image.Dither.FLOYDSTEINBERG)
                        img.save(os.path.join(target, "Covers", f"{safe}.bmp"))
                    cnt += 1
                except:
                    pass
        messagebox.showinfo("Done", f"Sent {cnt} files.")



if __name__ == "__main__":
    app = XlibreApp()
    app.mainloop()