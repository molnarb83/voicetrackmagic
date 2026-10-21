@echo off
REM ============================================================
REM  VoiceTrack Magic - one-click EXE builder
REM  Result: dist\VoiceTrackMagic.exe  (single standalone file)
REM ============================================================
setlocal
cd /d "%~dp0"

echo.
echo === VoiceTrack Magic build ===
echo.

REM --- 1. Python check -----------------------------------------
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found in PATH.
    echo         Install Python 3.10+ from https://python.org
    pause & exit /b 1
)

REM --- 2. Dependencies -----------------------------------------
echo [1/4] Installing/updating dependencies...
python -m pip install --quiet --upgrade PyQt5 pydub sounddevice numpy pymysql pyinstaller Pillow
if errorlevel 1 (
    echo [ERROR] pip install failed.
    pause & exit /b 1
)

REM --- 3. Required files check ---------------------------------
echo [2/4] Checking required files...
if not exist "ffmpeg.exe" (
    echo.
    echo [WARNING] ffmpeg.exe is missing from this folder!
    echo           Audio filters will NOT work in the built EXE.
    echo           Download the "release-full" build from:
    echo           https://www.gyan.dev/ffmpeg/builds/
    echo           and copy bin\ffmpeg.exe next to this script.
    echo.
    choice /M "Continue building anyway"
    if errorlevel 2 exit /b 1
)
if not exist "icon.png" (
    echo [INFO] icon.png not found - EXE will use the default icon.
)

REM --- 4. Build ------------------------------------------------
echo [3/4] Building EXE (this takes a few minutes)...
python -m PyInstaller VoiceTrackMagic.spec --clean --noconfirm
if errorlevel 1 (
    echo [ERROR] PyInstaller build failed - see output above.
    pause & exit /b 1
)

REM --- 5. Copy ffmpeg next to the EXE (AV-safe location) -------
echo [4/4] Finalizing...
if exist "ffmpeg.exe" copy /y "ffmpeg.exe" "dist\ffmpeg.exe" >nul

echo.
echo ============================================================
echo  BUILD SUCCESSFUL
echo  Output: dist\VoiceTrackMagic.exe
if exist "dist\ffmpeg.exe" echo          dist\ffmpeg.exe  (keep them together!)
echo ============================================================
echo.
pause
