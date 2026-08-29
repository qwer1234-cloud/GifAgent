"""Tests for the versioned prompt registry (``scripts/token_sprint/prompts.py``).

Locks: every known pass ships a v1 template with correct front matter; the
SHA-256 matches the exact file bytes; changing a single byte changes the hash;
missing templates raise.
"""

from __future__ import annotations

import hashlib

import pytest

from scripts.token_sprint.prompts import KNOWN_PASSES, prompt_path, prompt_sha256

ALL_PASSES = ("p1a", "p1b", "p2", "p3", "p4", "p5")


def _read(path) -> bytes:
    return path.read_bytes()


def _front_matter(text: str) -> dict[str, str]:
    assert text.startswith("---"), "template must start with YAML front matter"
    block = text.split("---", 2)[1]
    meta: dict[str, str] = {}
    for line in block.strip().splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta


class TestPromptRegistry:
    def test_all_passes_have_v1_templates(self):
        assert ALL_PASSES == tuple(KNOWN_PASSES)
        for pass_id in ALL_PASSES:
            path = prompt_path(pass_id, "v1")
            assert path.is_file(), f"missing template: {path}"

    def test_front_matter_pins_pass_and_version(self):
        for pass_id in ALL_PASSES:
            text = prompt_path(pass_id, "v1").read_text(encoding="utf-8")
            meta = _front_matter(text)
            assert meta.get("pass_id") == pass_id
            assert meta.get("prompt_version") == "v1"

    def test_templates_state_hard_rules(self):
        for pass_id in ALL_PASSES:
            text = prompt_path(pass_id, "v1").read_text(encoding="utf-8").lower()
            assert "refused" in text, f"{pass_id} must define the refusal line"
            assert "never" in text and "media" in text, (
                f"{pass_id} must forbid reading media files"
            )
            assert "sanitiz" in text, f"{pass_id} must forbid sanitization"

    def test_prompt_sha256_matches_file_bytes(self):
        for pass_id in ALL_PASSES:
            path = prompt_path(pass_id, "v1")
            expected = hashlib.sha256(_read(path)).hexdigest()
            assert prompt_sha256(pass_id, "v1") == expected

    def test_single_byte_change_changes_sha(self, tmp_path):
        base = tmp_path / "prompts"
        target = base / "p3"
        target.mkdir(parents=True)
        original = b"keywords: nude beach scene\n"
        (target / "v9.md").write_bytes(original)

        before = prompt_sha256("p3", "v9", base_dir=base)
        (target / "v9.md").write_bytes(original.replace(b"beach", b"BEach"))
        after = prompt_sha256("p3", "v9", base_dir=base)

        assert before != after

    def test_missing_template_raises(self):
        with pytest.raises(FileNotFoundError):
            prompt_sha256("p3", "v999")
        with pytest.raises(FileNotFoundError):
            prompt_sha256("p999", "v1")
