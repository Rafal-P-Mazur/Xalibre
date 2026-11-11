#!/bin/bash
set -e

echo "=== Building Xalibre for macOS ==="

# 1. Compile C dither acceleration library if clang/gcc available
if command -v clang >/dev/null 2>&1; then
    echo "--> Compiling dither_accel.dylib..."
    clang -O3 -shared -fPIC dither_accel.c -o dither_accel.dylib
    echo "    dither_accel.dylib compiled successfully."
elif command -v gcc >/dev/null 2>&1; then
    echo "--> Compiling dither_accel.dylib..."
    gcc -O3 -shared -fPIC dither_accel.c -o dither_accel.dylib
    echo "    dither_accel.dylib compiled successfully."
else
    echo "--> No C compiler found; skipping dither_accel.dylib compilation (pure Python fallback will be used)."
fi

# 2. Check / install dependencies
if [ -f "requirements.txt" ]; then
    echo "--> Ensuring dependencies are installed..."
    python3 -m pip install -r requirements.txt pyinstaller
fi

# 3. Clean previous builds
echo "--> Cleaning previous build artifacts..."
rm -rf build dist/Xalibre.app

# 4. Run PyInstaller
echo "--> Running PyInstaller..."
pyinstaller Xalibre_mac.spec --clean --noconfirm

# 5. Remove quarantine, touch bundle, and refresh LaunchServices cache
echo "--> Stripping quarantine attributes and refreshing LaunchServices cache..."
xattr -cr dist/Xalibre.app 2>/dev/null || true
touch dist/Xalibre.app

LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
if [ -f "$LSREGISTER" ]; then
    "$LSREGISTER" -f dist/Xalibre.app 2>/dev/null || true
fi

killall Finder 2>/dev/null || true

echo "=== Build Complete! ==="
echo "Application bundle created at: dist/Xalibre.app"

