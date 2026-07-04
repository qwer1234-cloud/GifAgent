# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for GifAgent Web UI exe.

Build:
    uv run pip install pyinstaller
    uv run pyinstaller build_exe.spec

Output:
    dist/GifAgentUI/GifAgentUI.exe
"""
import os
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

block_cipher = None

# Gradio + Gradio Client need full collection (static assets, templates)
datas = []
binaries = []
hiddenimports = []

for pkg in ["gradio", "gradio_client", "gradio_templates", "safehttpx", "groovy",
            "ffmpy", "pydub", "marker", "pillow", "numpy", "pydantic", "pydantic_core",
            "fastapi", "starlette", "uvicorn", "httpx", "anyio", "h11", "yaml",
            "imagehash", "aiofiles", "sockio",
            # pywebview desktop window (Windows uses EdgeChromium/WinForms backend)
            "webview", "pythonnet", "clr_loader"]:
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# pywebview Windows backend — collect explicitly so PyInstaller finds them
hiddenimports += ["webview.platforms.winforms", "webview.platforms.edgechromium"]

# FastAPI / uvicorn / pydantic hidden imports
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("fastapi")
hiddenimports += collect_submodules("pydantic")
hiddenimports += ["httpx", "httpx._transports", "httpx._transports.default"]

# App modules — explicitly include script-only imports that PyInstaller's
# static analysis won't find (scripts/ are data files, not analyzed)
hiddenimports += collect_submodules("app")
hiddenimports += [
    "app.services.video_fingerprint",
    "app.services.json_guard",
    "app.services.llm_client",
    "app.services.quality",
    "app.services.embedding",
    "app.services.indexer",
    "app.services.scanner",
    "app.services.preprocess",
    "app.services.scheduler",
    "app.services.vision",
    "app.services.llm",
    "app.services.scorer",
    "app.services.preference_schema",
    "app.services.preference_events",
    "app.services.preference_memory",
    "app.services.preference_evaluation",
    "app.services.preference_types",
    "app.services.candidates",
    "app.services.reranker",
    "app.services.scenario",
    "app.routers.candidates",
    "app.routers.preference",
]

# Include config file, scripts, and app package data
datas += [("configs/models.yaml", "configs")]
datas += [("scripts", "scripts")]
datas += collect_data_files("app")

a = Analysis(
    ["app/ui/launcher.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="GifAgentUI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,  # keep console open so user sees logs
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="GifAgentUI",
)
