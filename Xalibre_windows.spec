# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Xalibre on Windows
#
# Build with:
#   pyinstaller Xalibre_windows.spec --clean
#
# Output: dist/Xalibre.exe

import os
import sys
import importlib.util
from PyInstaller.utils.hooks import collect_all

block_cipher = None

# --- Core App Data & Resources ---
datas = []

# AoA Database
if os.path.exists('AoA_51715_words.csv'):
    datas.append(('AoA_51715_words.csv', '.'))

# Bundled Fonts: always map to 'Fonts' in runtime bundle (_MEIPASS/Fonts)
for font_dir in ('Fonts', 'fonts'):
    if os.path.isdir(font_dir):
        datas.append((font_dir, 'Fonts'))
        break

# Presets folder if present
for preset_dir in ('presets', 'Presets'):
    if os.path.isdir(preset_dir) and os.listdir(preset_dir):
        datas.append((preset_dir, 'presets'))
        break

binaries = []
for lib_name in ('dither_accel.dll', 'dither_accel.so', 'dither_accel.dylib'):
    if os.path.exists(lib_name):
        binaries.append((lib_name, '.'))
hiddenimports = [
    'pymupdf',
    'pymupdf._extra',
    'fitz',
    'pyphen.dictionaries',
    'ebooklib',
    'ebooklib.epub',
    'bs4',
    'PIL',
    'PIL.Image',
    'PIL.ImageOps',
    'PIL.ImageFilter',
    'PIL.ImageEnhance',
    'requests',
    'sqlite3',
    'json',
    'numpy',
    'lxml',
    'lxml.etree',
    'rarfile',
    'mobi',
]

# Optional dependencies (include if available in environment)
for opt_mod in ('scipy', 'scipy.ndimage', 'openai', 'fonttools', 'wordfreq'):
    try:
        __import__(opt_mod)
        hiddenimports.append(opt_mod)
    except ImportError:
        pass

# --- Collect packages with data/binary assets ---
for pkg in ('customtkinter', 'tkinterdnd2', 'pyphen', 'fitz', 'wordfreq'):
    try:
        tmp_ret = collect_all(pkg)
        datas += tmp_ret[0]
        binaries += tmp_ret[1]
        hiddenimports += tmp_ret[2]
    except Exception as e:
        print(f"[Warning] collect_all({pkg}) failed: {e}")

# Explicit tkinterdnd2 tkdnd package directory fallback
try:
    spec = importlib.util.find_spec('tkinterdnd2')
    if spec and spec.submodule_search_locations:
        pkg_dir = list(spec.submodule_search_locations)[0]
        tkdnd_dir = os.path.join(pkg_dir, 'tkdnd')
        if os.path.isdir(tkdnd_dir):
            datas.append((tkdnd_dir, 'tkinterdnd2/tkdnd'))
except Exception as e:
    print(f"[Warning] could not locate tkinterdnd2 tkdnd folder: {e}")

# Deduplicate hidden imports
hiddenimports = sorted(list(set(hiddenimports)))

a = Analysis(
    ['Xalibre.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter.test', 'unittest', 'test'],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Xalibre',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='icon.ico' if os.path.exists('icon.ico') else None,
)
