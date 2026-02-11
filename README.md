# EPUB to XTC Converter for Xteink X4
**Comprehensive EPUB to XTC/XTH conversion with AI-Powered Reading Aids**

A powerful GUI-based tool designed to convert standard `.epub` files into the binary `.xtc` (1-bit) or `.xth` (2-bit) formats required by the **Xteink X4** e-reader. This tool renders HTML content into paginated, bitmapped images optimized specifically for E-Ink displays, ensuring a premium reading experience.

[![Web Version](https://img.shields.io/badge/Web_Version-Live-green)](https://epub2xtc.streamlit.app/) 
*(Note: Web version is currently a legacy release and does not yet support 2-bit rendering or Spectra AI)*

## 🚀 Main Features

* **Visual & Native Navigation:** Automatically renders a formatted **Visual Table of Contents** into the book's first pages. Simultaneously injects Title, Author, and Chapter metadata into the binary header, enabling the device's native menus and physical chapter-skip buttons.
* **Spectra AI Reading Aide (Experimental):** An AI-powered overlay that scans text for difficult vocabulary and prints context-aware definitions or translations directly above the words.
* **Smart Vocabulary Filtering:** Uses Zipf frequency and Age of Acquisition (AoA) databases to target only the words that match your specific reading level (A2–C1). 
* **Advanced Rendering Engine:** * **1-Bit (XTG) & 2-Bit (XTH):** Support for 4-level grayscale rendering for smoother anti-aliasing and better illustration depth.
* **Modern Interface:** A sleek, dark-themed UI with collapsible settings cards for Typography, Layout, and Rendering.
* **Deep Typography Control:** Full support for custom `.ttf`/`.otf` fonts with adjustable weight, size, line height, and `pyphen`-based hyphenation.
* **Inline Footnotes:** Automatically renders internal references directly below the text block, eliminating the need for page-jumping.
* **Cover Optimization:** Built-in utility to resize, crop, and dither covers into high-quality e-ink bitmaps.

![App Screenshot 1](images/xtc.png)
![App Screenshot 2](images/xtc2.png)

## 📥 Installation

### Option 1: Run from Source
1. **Install the dependencies:**
    ```bash
    pip install pymupdf Pillow EbookLib beautifulsoup4 pyphen customtkinter wordfreq openai
    ```
2. **Clone the repository:**
    ```bash
    git clone [https://github.com/Rafal-P-Mazur/EPUB2XTC.git](https://github.com/Rafal-P-Mazur/EPUB2XTC.git)
    cd EPUB2XTC
    ```
3. **Run the App:**
    ```bash
    python EPUB2XTC.py
    ```

### Option 2: Standalone Executable (.exe)
Download the latest [Release version](https://github.com/Rafal-P-Mazur/EPUB2XTC/releases). No Python installation required.

## 🧠 Spectra AI Setup (Cloud vs Local)

To use the **experimental** Spectra AI feature, you need to connect the app to an LLM:

### Local (Recommended - Free & Private)
*Hardware Context: Processing a 200-page book takes ~15 minutes on an RTX 3090.*
1.  Run **LM Studio** and load a fast model (Recommended: **Gemma-3-4B** for English or **Bielik-4.5** for Polish).
2.  Start the **Local Server** in LM Studio.
3.  In EPUB2XTC, set the Base URL to `http://localhost:1234/v1`.
4.  Set the **Model** field to match the exact identifier loaded in LM Studio.

### Cloud
1.  Enter your **OpenAI API Key** (Note: Usage is connected to your provider's standard costs).
2.  Set Base URL to `https://api.openai.com/v1`.
3.  Set Model to `gpt-4o-mini`.

## 📖 Quick Start

1.  **Import:** Click **＋ Import EPUB**.
2.  **TOC Setup:** Select which chapters to include in the navigation via the popup dialog.
3.  **Tweak:** Use the sidebar cards to adjust Typography and Layout.
4.  **Rendering:** Choose **2-bit XTH** for smoother graphics or **1-bit XTG** for classic high contrast.
5.  **AI (Optional):** Use the Spectra card to **Analyze & Generate Definitions**. (Note: Experimental; accuracy depends on the model).
6.  **Export:** Click **⚡ Save .XTC** to generate your file.

---

## 📄 License
MIT License
