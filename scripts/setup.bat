@echo off
setlocal enabledelayedexpansion
echo === GifAgent Setup ===

echo [1/4] Creating virtual environment...
py -3 -m venv venv 2>NUL || python -m venv venv 2>NUL || python3 -m venv venv 2>NUL
if %errorlevel% neq 0 (
    echo Failed to create virtual environment. Make sure Python 3.11+ is installed.
    exit /b 1
)
call venv\Scripts\activate.bat

echo [2/4] Installing Python dependencies...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo pip install failed! Check your network connection and Python version.
    exit /b 1
)

echo [3/4] Verifying ffmpeg...
where ffmpeg >NUL 2>NUL
if %errorlevel% neq 0 (
    echo ffmpeg not found! Install from https://ffmpeg.org/download.html
    echo Make sure ffmpeg.exe is in PATH
    exit /b 1
)
echo ffmpeg found.

echo [4/4] Pulling llava:13b for visual understanding...
where ollama >NUL 2>NUL
if %errorlevel% neq 0 (
    echo Ollama not found! Install from https://ollama.com
    exit /b 1
)
ollama pull llava:13b
if %errorlevel% neq 0 (
    echo Failed to pull llava:13b. Check Ollama is running and your internet connection.
    exit /b 1
)

echo === Setup complete ===
echo Run: venv\Scripts\activate
echo Then: python app/main.py
