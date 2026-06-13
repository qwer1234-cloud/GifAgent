@echo off
setlocal enabledelayedexpansion
echo === GifAgent Setup ===

echo [1/3] Verifying uv...
where uv >NUL 2>NUL
if %errorlevel% neq 0 (
    echo uv not found! Installing...
    powershell -Command "irm https://astral.sh/uv/install.ps1 | iex"
)
echo uv found.

echo [2/3] Installing Python 3.11+ and dependencies...
uv sync
if %errorlevel% neq 0 (
    echo uv sync failed!
    exit /b 1
)

echo [3/3] Verifying external tools...
where ffmpeg >NUL 2>NUL
if %errorlevel% neq 0 (
    echo ffmpeg not found! Install from https://ffmpeg.org/download.html
    echo Make sure ffmpeg.exe is in PATH
    exit /b 1
)
echo ffmpeg found.

where ollama >NUL 2>NUL
if %errorlevel% neq 0 (
    echo Ollama not found! Install from https://ollama.com
    exit /b 1
)
echo Pulling llava:13b for visual understanding...
ollama pull llava:13b
if %errorlevel% neq 0 (
    echo Failed to pull llava:13b. Check Ollama is running and your internet connection.
    exit /b 1
)

echo === Setup complete ===
echo Run: uv run python app/main.py
