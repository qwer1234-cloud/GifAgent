"""Keep local preview filenames safe for Gradio's unescaped file URLs."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import uuid


def preview_path(path_value: str | None) -> str | None:
    """Alias URL-sensitive filenames while leaving feedback paths untouched.

    Gradio retains the basename when caching images and directly appends it to
    ``/file=``. A literal percent escape or fragment marker then addresses a
    different file. Use an ASCII basename for display only, including preloads.
    """
    if not path_value:
        return path_value
    source = Path(path_value)
    if not any(character in source.name for character in "%#?"):
        return path_value
    try:
        stat = source.stat()
    except OSError:
        return path_value

    identity = f"{source.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    cache = Path("data/previews/candidates")
    target = cache / f"{key}{source.suffix.lower()}"
    if not target.is_file():
        cache.mkdir(parents=True, exist_ok=True)
        temporary = cache / f"{key}.{uuid.uuid4().hex}.tmp"
        try:
            try:
                os.link(source, temporary)
            except OSError:
                # Cross-volume exports and filesystems without hard links.
                shutil.copyfile(source, temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return str(target.resolve())
