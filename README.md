# Xalibre Library Manager & Converter

**Xalibre** is a desktop e-book management suite, document processor, and rendering pipeline. It is designed to organize digital libraries, curate metadata, edit typography, and convert books and comics into the optimized binary **XTC** format for e-ink devices (including the Xteink X4 and X3).

Formerly known as *Epub2XTC*, Xalibre provides complete desktop library management with cross-platform support for Windows and macOS.

---

## Features

### Library Management & Cataloging
- **Local SQLite Database**: Manage your library with real-time searching, category filtering, and sorting by title, author, date added, status, or rating.
- **Reading Progress Tracker**: Track reading status (`Unread`, `Reading`, `Read`), assign 0–5 star ratings, and maintain book notes.
- **Automated Cloud Metadata**: Fetch book summaries, publication dates, genres, and high-resolution covers from Google Books, Apple Books, and Open Library.
- **In-Place Cover Injection**: Scraped or custom cover artwork can be written directly back into the source EPUB file container.
- **Granular Drag-and-Drop**: Drop files into the window to import. Drag cards or individual format badges out of the app to export covers (.BMP), source EPUBs, or compiled XTC files.
- **Batch Processing**: Queue multiple titles for background conversion across worker threads without freezing the user interface.

### EPUB & Document Conversion Engine
- **Target Formats**: Generates 1-bit monochrome (`XTG`) for sharp text or 2-bit grayscale (`XTH`, 4 levels) for illustrations and manga.
- **Advanced Dithering Modes**: Choose between Bayer 8x8 ordered dithering (preserves comic screentones), Atkinson error diffusion (clean whites without background noise), or Floyd-Steinberg.
- **Optional C Acceleration**: Includes a C dithering accelerator (`dither_accel.c`) with automatic fallback to pure Python/NumPy.
- **Inline Footnotes**: Automatically detects endnotes across the book and renders them inside clear callout boxes immediately below the referencing paragraph, eliminating the need to flip to the end of the book.
- **Layout & Typography Controls**: Full control over font size, weight, line height, paragraph spacing, word spacing, margins, padding, and text justification.
- **Header, Footer & Progress Bar**: Highly customizable layout elements with placement controls for book title, current page, chapter page, read percentage, and a visual progress bar with discrete chapter ticks.
- **Typography & Hyphenation**: Bundled reader fonts (Atkinson Hyperlegible, Charis SIL, Bookerly, Literata, DejaVu Sans/Serif), user font importing (`.ttf`, `.otf`), and language-aware soft hyphenation via Pyphen.
- **Automatic Table of Contents**: Generates and inserts a dedicated TOC page into the compiled document.
- **Configuration Presets**: Save and load custom styling profiles (e.g., "Night Reading", "Manga", "Compact Text") as reusable JSON presets.

### Specialized Comic & Manga Processing
- **Archive Ingestion**: Native extraction and rendering for CBZ, CBR, and PDF graphic novels.
- **Smart Tier Zoom**: Automatically detects page panels and slices tall pages into readable vertical tiers (top/bottom, 3-row, or 2x2 grid) with visual context "peeking".
- **Double-Page Spread Handling**: Automatically detects landscape spreads with options to split into consecutive portrait pages, rotate, or fit.
- **Manga Reading Order**: Automatic Right-to-Left (RTL) page ordering and panel flow for Japanese manga.
- **E-Ink Graphic Enhancements**:
  - *Speech Bubble Masking*: Whitens speech bubble interiors to maximize dialogue contrast.
  - *Line Boldening*: Strengthens fine linework for low-contrast e-ink screens.
  - *Image Processing*: Automatic margin cropping, auto-levels, contrast curves, gamma correction, and unsharp masking.

### Spectra AI (Vocabulary Assistant)
- **Inline Vocabulary Annotations**: Inspired by Kindle Word Wise, Spectra inserts small definitions or synonyms directly above difficult words.
- **Linguistic Difficulty Thresholds**: Filters words using the Kuperman Age-of-Acquisition (AoA) linguistic dataset and Zipf word frequency scores.
- **LLM Integration**: Context-aware explanations powered by OpenAI-compatible APIs (defaults to `gpt-4o-mini`, supports custom endpoints and models) with multi-language target translation.

### Hardware Sync & Device Profiles
- **Supported Profiles**: Native profiles for **Xteink X4** (480x800) and **Xteink X3** (528x792), with customizable resolution profiles via `devices.json`.
- **Wireless Device Manager**: Browse files on your Xteink device over local Wi-Fi, dedicated hotspots (`192.168.3.3`), or CrossPoint networks. Supports drag-and-drop file uploads directly to the device filesystem.
- **USB & Local Sync**: Single-click export structured for e-reader directories, including a dedicated `/Covers` folder for sleep screens.

### Built-in Inspection Viewers
- **XTC Binary Viewer**: An integrated emulator that parses binary XTC files from disk, unpacks vertical bit streams, navigates chapter tables, and renders pixel-perfect previews.
- **EPUB Document Viewer**: Quick reader to inspect HTML source layout, CSS styling, and chapter structure before conversion.

---

![App Screenshot 1](images/1.png)
![App Screenshot 2](images/2.png)

---

## Supported Input Formats

| Format | Import Pipeline |
| :--- | :--- |
| **EPUB** | Native EPUB parsing, CSS styling, chapter splitting, and metadata extraction. |
| **CBZ / CBR** | Comic archives with natural page sorting, orientation detection, and image filtering. |
| **PDF** | Reflowable text extraction and high-resolution page rendering via PyMuPDF. |
| **MOBI / AZW** | Decompression and conversion via `mobi` with optional `ebook-convert` fallback. |
| **FB2 / TXT** | Structural parsing, encoding detection, and chapter demarcation. |

---

## Getting Started

### Prerequisites

- Python 3.10, 3.11, or 3.12
- Git

### Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/Rafal-P-Mazur/Xalibre.git
   cd Xalibre
   ```

2. Create and activate a virtual environment:
   - **macOS / Linux:**
     ```bash
     python3 -m venv .venv
     source .venv/bin/activate
     ```
   - **Windows (PowerShell):**
     ```powershell
     python -m venv .venv
     .venv\Scripts\Activate.ps1
     ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. (Optional) Compile C Dithering Accelerator:
   - **macOS / Linux:**
     ```bash
     clang -O3 -shared -fPIC dither_accel.c -o dither_accel.dylib
     ```
   - **Windows:**
     A precompiled `dither_accel.dll` is used, or fallback pure Python/NumPy logic runs automatically.

5. Launch Xalibre:
   ```bash
   python Xalibre.py
   ```

---

## Building Standalone Packages

### macOS (`dist/Xalibre.app`)
Run the automated build script:
```bash
chmod +x build_mac.sh
./build_mac.sh
```
Or run PyInstaller directly:
```bash
pyinstaller Xalibre_mac.spec --clean --noconfirm
```
See [README_MAC.md](README_MAC.md) for details on macOS security entitlements and codesigning.

### Windows (`dist/Xalibre.exe`)
```powershell
pyinstaller Xalibre_windows.spec --clean --noconfirm
```

---

## Application Layout & Data Storage

On initial launch, Xalibre automatically creates its working directories:
- **Base Directory**: `~/Xalibre` (configurable in settings)
  - `Library/`: Managed copies of imported books.
  - `Exports/`: Converted files and exported packages.
  - `Presets/`: Saved converter and layout profiles (`.json`).
  - `Fonts/`: User-installed fonts (`.ttf`, `.otf`) in addition to bundled fonts.
- **Database**: SQLite library catalog stored at `~/Xalibre/xalibre.db`.
- **Configuration**: User settings stored in `~/Xalibre/default_settings.json` (or `Application Support/Xalibre` on macOS).

---

## Technical Stack

- **GUI Framework**: CustomTkinter with TkinterDnD2 drag-and-drop integration.
- **Document & Image Processing**: PyMuPDF (`fitz`), Pillow, NumPy, EbookLib, Beautiful Soup 4.
- **Hyphenation & Linguistics**: Pyphen hyphenation dictionaries and Kuperman Age-of-Acquisition dataset (`AoA_51715_words.csv`).
- **Typography**: Bundled fonts including Atkinson Hyperlegible, Charis SIL, DejaVu Sans/Serif, Bookerly, and Literata.

---

## License

This project is licensed under the MIT License. Bundled fonts retain their respective open font licenses (OFL / SIL).
