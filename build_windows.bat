@echo off
setlocal
echo ==============================================
echo        Building Xalibre for Windows
echo ==============================================

:: Detect virtual environment (.venv or venv) or fallback to system python
set "PYTHON_EXE="
if exist ".venv\Scripts\python.exe" (
    set "PYTHON_EXE=.venv\Scripts\python.exe"
    echo [*] Detected project virtual environment (.venv)
) else if exist "venv\Scripts\python.exe" (
    set "PYTHON_EXE=venv\Scripts\python.exe"
    echo [*] Detected project virtual environment (venv)
) else (
    set "PYTHON_EXE=python"
    echo [*] Using system python
)

:: 1. Ensure dependencies and PyInstaller are installed
echo [1/3] Ensuring dependencies are up-to-date...
"%PYTHON_EXE%" -m pip install -r requirements.txt pyinstaller

:: 2. Clean previous build artifacts
echo [2/3] Cleaning previous build artifacts...
if exist build rmdir /s /q build
if exist dist\Xalibre.exe del /f /q dist\Xalibre.exe

:: 3. Run PyInstaller explicitly through the project's Python
echo [3/3] Compiling Xalibre.exe with PyInstaller...
"%PYTHON_EXE%" -m PyInstaller Xalibre_windows.spec --clean --noconfirm

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
