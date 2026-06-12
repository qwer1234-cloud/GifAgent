@echo off
echo === GifAgent Setup ===

echo [1/4] Creating virtual environment...
python -m venv venv
call venv\Scripts\activate.bat

echo [2/4] Installing Python dependencies...
pip install -r requirements.txt

echo [3/4] Verifying ffmpeg...
where ffmpeg >/dev/null 2>&1
if %errorlevel% neq 0 (
    echo ffmpeg not found! Install from https://ffmpeg.org/download.html
    echo Make sure ffmpeg.exe is in PATH
    exit /b 1
)
echo ffmpeg found.

echo [4/4] Pulling llava:13b for visual understanding...
ollama pull llava:13b

echo === Setup complete ===
echo Run: venv\Scripts\activate
echo Then: python app/main.py
