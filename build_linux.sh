#!/bin/bash
set -e

echo "=============================================="
echo "       Building Xalibre for Linux"
echo "=============================================="

# Detect virtual environment (.venv or venv) or fallback to system python3
PYTHON_EXE=""
if [ -x ".venv/bin/python" ]; then
    PYTHON_EXE=".venv/bin/python"
    echo "[*] Detected project virtual environment (.venv)"
elif [ -x "venv/bin/python" ]; then
    PYTHON_EXE="venv/bin/python"
    echo "[*] Detected project virtual environment (venv)"
else
    PYTHON_EXE="python3"
    echo "[*] Using system python3"
fi

# 1. Compile C dither acceleration library if gcc/clang available
if command -v gcc >/dev/null 2>&1; then
    echo "[1/4] Compiling dither_accel.so..."
    gcc -O3 -shared -fPIC dither_accel.c -o dither_accel.so -lm
    echo "      dither_accel.so compiled successfully."
elif command -v clang >/dev/null 2>&1; then
    echo "[1/4] Compiling dither_accel.so..."
    clang -O3 -shared -fPIC dither_accel.c -o dither_accel.so -lm
    echo "      dither_accel.so compiled successfully."
else
    echo "[1/4] No C compiler found; skipping dither_accel.so (pure Python fallback will be used)."
fi

# 2. Ensure dependencies and PyInstaller are installed
echo "[2/4] Ensuring dependencies are up-to-date..."
"$PYTHON_EXE" -m pip install -r requirements.txt pyinstaller

# 3. Clean previous build artifacts
echo "[3/4] Cleaning previous build artifacts..."
rm -rf build dist/Xalibre

# 4. Run PyInstaller
echo "[4/4] Compiling Xalibre with PyInstaller..."
"$PYTHON_EXE" -m PyInstaller Xalibre_linux.spec --clean --noconfirm

if [ -f "dist/Xalibre" ]; then
    chmod +x dist/Xalibre
    echo "=============================================="
    echo " Build Successful!"
    echo " Executable created at: dist/Xalibre"
    echo "=============================================="
else
    echo "[ERROR] Build failed. Please inspect the log messages above."
    exit 1
fi
