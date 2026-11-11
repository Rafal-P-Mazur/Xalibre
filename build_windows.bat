@echo off
setlocal
echo ==============================================
echo        Building Xalibre for Windows
echo ==============================================

:: 1. Ensure dependencies are installed
echo [1/3] Checking dependencies...
python -m pip install -r requirements.txt pyinstaller

:: 2. Clean previous build artifacts
echo [2/3] Cleaning previous build artifacts...
if exist build rmdir /s /q build
if exist dist\Xalibre.exe del /f /q dist\Xalibre.exe

:: 3. Run PyInstaller
echo [3/3] Compiling Xalibre.exe with PyInstaller...
pyinstaller Xalibre_windows.spec --clean --noconfirm

echo.
if exist dist\Xalibre.exe (
    echo ==============================================
    echo  Build Successful!
    echo  Executable created at: dist\Xalibre.exe
    echo ==============================================
) else (
    echo [ERROR] Build failed. Please inspect the log messages above.
)
endlocal
pause
