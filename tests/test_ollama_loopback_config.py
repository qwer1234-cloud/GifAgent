from pathlib import Path


OLLAMA_HOST_PATTERN = "localhost:11434"


def test_ollama_defaults_use_ipv4_loopback():
    project_root = Path(__file__).resolve().parents[1]
    scanned_paths = [
        project_root / "configs" / "models.yaml",
        project_root / "app",
        project_root / "scripts",
    ]

    offenders = []
    for path in scanned_paths:
        files = [path] if path.is_file() else path.rglob("*")
        for file_path in files:
            if file_path.suffix not in {".py", ".yaml", ".yml"}:
                continue
            text = file_path.read_text(encoding="utf-8", errors="ignore")
            if OLLAMA_HOST_PATTERN in text:
                offenders.append(str(file_path.relative_to(project_root)))

    assert offenders == []
