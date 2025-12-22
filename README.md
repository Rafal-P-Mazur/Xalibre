# EPUB to XTC Converter for Xteink X4
Epub to XTC converter

A GUI-based tool designed to convert standard `.epub` files into the `.xtc` binary format required by the **Xteink X4** e-reader. It renders HTML content into paginated, bitmapped images optimized for e-ink displays.

[![Web Version](https://img.shields.io/badge/Web_Version-Live-green)](https://epub2xtc.streamlit.app/)

## Main Features

* **Smart Hyphenation:** Uses `pyphen` to inject soft hyphens into text nodes, ensuring proper line breaks and justified text flow.
* **Table of Contents Generation:** Automatically creates visual TOC pages at the start of the file, linked to specific rendered page numbers.
* **Visual Progress Bar:** Generates a reading progress bar at the bottom of every page, including indicators for chapter start points.
* **Custom Typography:** Supports system fonts and external `.ttf` / `.otf` fonts. Allows adjustment of font weight, size, and line height.
* **Image Optimization:** Automatically extracts, scales, contrast-enhances, and dithers (Floyd-Steinberg) images embedded in the EPUB.
* **Layout Control:** Configurable margins, top/bottom padding, and text alignment (Justified/Left).

![App Screenshot 1](images/xtc.png)
![App Screenshot 2](images/xtc2.png)


## 📥 Installation

### Option 1: Run from Source
1. **Install the dependencies:**
    ```bash
    pip install pymupdf Pillow EbookLib beautifulsoup4 pyphen customtkinter
    ```
2.  **Clone the repository:**
    ```bash
    git clone https://github.com/Rafal-P-Mazur/EPUB2XTC.git
    cd EPUB2XTC
    ```
3.  **Run the App:**
    ```bash
    python EPUB2XTC.py
    ```

### Option 2: Standalone Executable (.exe)
If you have downloaded the [Release version](https://github.com/Rafal-P-Mazur/EPUB2XTC/releases), simply unzip the file and run `EPUB2XTC.exe`. No Python installation is required.

## 📖 User Manual

1.  **Load an EPUB:** Click **Select EPUB** in the sidebar. The application will instantly parse the book structure.
2.  **Select Chapters:** A dialog will automatically appear displaying all detected chapters.
    * **Uncheck** any chapters you wish to hide from the **Table of Contents** and **Progress Bar**.
    * *Note:* These chapters are **not deleted**; they remain in the book for reading but will not clutter your navigation.
3.  **Configure Layout:**
    * **Font:** Choose from detected system/local fonts.
    * **Settings:** Adjust Size, Weight, Line Height, Margins, and Padding. The preview will **automatically update** after a short delay when settings are changed.
    * **Orientation:** Switch between Portrait and Landscape modes.
    * **Preview Zoom:** Use the slider to resize the preview image (Smart Scaling automatically optimizes the zoom based on your orientation).
4.  **Navigate & Preview:**
    * Use the **< Previous** and **Next >** buttons to flip pages.
    * Enter a specific number in the **"Go"** input box to jump directly to that page.
5.  **Export:** Click **Export XTC** to save the final binary file.
   
## 📦 Dependencies

* `customtkinter` (GUI)
* `PyMuPDF` (Rendering)
* `Pillow` (Image Processing)
* `EbookLib` & `BeautifulSoup4` (Parsing)
* `Pyphen` (Hyphenation)

---

## 📄 License
MIT License

