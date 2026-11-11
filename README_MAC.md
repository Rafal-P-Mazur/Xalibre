# Xalibre - macOS Setup & Build Guide

## Requirements
- macOS 11.0 (Big Sur) or newer (Apple Silicon M1/M2/M3/M4 or Intel)
- Python 3.10+ (Python 3.11 or 3.12 recommended with `python-tk` / Tkinter installed)
- Optional: Xcode Command Line Tools (`xcode-select --install`) for compiling C dithering accelerator (`dither_accel.dylib`)

---

## 1. Quick Start (Run from Source)

1. Create and activate a virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. (Optional) Compile C acceleration module for ultra-fast dithering:
   ```bash
   clang -O3 -shared -fPIC dither_accel.c -o dither_accel.dylib
   ```

4. Launch the application:
   ```bash
   python3 Xalibre.py
   ```

---

## 2. Building macOS Standalone App (`Xalibre.app`)

1. Install PyInstaller:
   ```bash
   pip install pyinstaller
   ```

2. Run the automated build script:
   ```bash
   chmod +x build_mac.sh
   ./build_mac.sh
   ```

   Or run PyInstaller manually:
   ```bash
   # (Optional) compile dither_accel.dylib
   clang -O3 -shared -fPIC dither_accel.c -o dither_accel.dylib

   # Build .app
   pyinstaller Xalibre_mac.spec --clean --noconfirm
   ```

3. The finished application bundle will be located at:
   `dist/Xalibre.app`

---

## 3. macOS Specific Notes
- **App Data & Presets**: On macOS, application data and user settings are automatically placed in:
  `~/Library/Application Support/Xalibre/` or `~/Xalibre/`
- **External Tools**: 
  - For `.cbr` / RAR comic decompression, macOS built-in `bsdtar` or `unar` (`brew install unar`) are supported out-of-the-box.
  - For Calibre integration, `ebook-convert` is automatically detected at `/Applications/calibre.app/Contents/MacOS/ebook-convert`.
