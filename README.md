# GifAgent

## Quick Start

```bash
# Install uv if needed
powershell -Command "irm https://astral.sh/uv/install.ps1 | iex"

# Set up project (auto-downloads Python 3.11+, creates venv, installs deps)
uv sync

# Verify
uv run python -c "from app.main import app; print('OK')"
```

## Dependencies

Managed by [uv](https://docs.astral.sh/uv/). The `pyproject.toml` defines all dependencies with minimum version constraints.

For pip users, `requirements.txt` is also provided but requires Python 3.11+.

## Usage

```bash
# Start FastAPI server
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000

# Start Gradio UI
uv run python app/ui/review.py

# Run full indexing pipeline
uv run python scripts/index_library.py
```

## Configuration

Edit `configs/models.yaml` to configure:
- Media source directory
- Ollama model endpoints
- Scoring thresholds
- Frame extraction settings
