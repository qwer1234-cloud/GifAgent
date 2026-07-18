from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from app.quality_lab import (
    BenchmarkItem,
    BenchmarkManifest,
    freeze_manifest,
    load_manifest,
)
from app.quality_lab.manifests import assign_splits

# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------

TAGS = ("action", "dialogue")


def make_item(
    item_id: str,
    *,
    fingerprint: str = "",
    duration: str = "short",
    resolution: str = "hd",
    pace: str = "medium",
    split: str = "tune",
) -> BenchmarkItem:
    return BenchmarkItem(
        item_id=item_id,
        source_path=f"C:/videos/{item_id}.mp4",
        video_fingerprint=fingerprint or f"fp_{item_id}",
        duration_bucket=duration,
        resolution_bucket=resolution,
        pace_bucket=pace,
        difficulty_tags=TAGS,
        split=split,  # type: ignore[arg-type]
    )


def twenty_four_items() -> list[BenchmarkItem]:
    """Produce 24 items with unique fingerprints, matching -- exactly 24."""
    return [
        make_item(f"vid_{i:03d}", fingerprint=f"fp_{i:04d}")
        for i in range(24)
    ]


def make_source_files(items: list[BenchmarkItem], root: Path) -> None:
    """Touch every source_path referenced by the items under *root*."""
    for item in items:
        src = root / f"{item.item_id}.mp4"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("fake video content")
        # Replace the item's source_path to point under tmp_path
        item.__dict__["source_path"] = str(src)


def rewrite_source_paths(
    items: list[BenchmarkItem], root: Path
) -> list[BenchmarkItem]:
    """Return new items whose source_path points into *root*."""
    result = []
    for item in items:
        src = root / f"{item.item_id}.mp4"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("fake video content")
        result.append(
            BenchmarkItem(
                item_id=item.item_id,
                source_path=str(src),
                video_fingerprint=item.video_fingerprint,
                duration_bucket=item.duration_bucket,
                resolution_bucket=item.resolution_bucket,
                pace_bucket=item.pace_bucket,
                difficulty_tags=item.difficulty_tags,
                split=item.split,
            )
        )
    return result


# ===================================================================
# Tests
# ===================================================================


class TestFreezeManifest:
    """Tests for ``freeze_manifest`` and ``load_manifest``."""

    def test_deterministic_manifest_ids(self, tmp_path: Path) -> None:
        """Same items always produce the same manifest_id."""
        items = assign_splits(twenty_four_items())
        p1 = tmp_path / "m1.json"
        p2 = tmp_path / "m2.json"

        id1 = freeze_manifest(items, p1)
        id2 = freeze_manifest(items, p2)

        assert id1 == id2, "manifest_id must be deterministic"
        assert p1.read_text() == p2.read_text(), "file content must match"

    def test_exactly_24_unique_fingerprints(self, tmp_path: Path) -> None:
        """Manifest must contain exactly 24 items, each with a unique fingerprint."""
        items = assign_splits(twenty_four_items())
        dest = tmp_path / "manifest.json"
        freeze_manifest(items, dest)

        loaded = load_manifest(dest, verify_files=False)
        assert len(loaded.items) == 24

        fingerprints = {i.video_fingerprint for i in loaded.items}
        assert len(fingerprints) == 24

    def test_stratification_fields_preserved(self, tmp_path: Path) -> None:
        """All stratification buckets and tags survive round-trip."""
        items = assign_splits(twenty_four_items())
        dest = tmp_path / "manifest.json"
        freeze_manifest(items, dest)

        loaded = load_manifest(dest, verify_files=False)
        for orig, loaded_item in zip(items, loaded.items):
            assert loaded_item.duration_bucket == orig.duration_bucket
            assert loaded_item.resolution_bucket == orig.resolution_bucket
            assert loaded_item.pace_bucket == orig.pace_bucket
            assert loaded_item.difficulty_tags == orig.difficulty_tags

    def test_tune_holdout_split_distribution(self, tmp_path: Path) -> None:
        """Roughly 70/30 split with deterministic assignment."""
        items = assign_splits(twenty_four_items())
        dest = tmp_path / "manifest.json"
        freeze_manifest(items, dest)

        loaded = load_manifest(dest, verify_files=False)
        tune_count = sum(1 for i in loaded.items if i.split == "tune")
        holdout_count = sum(1 for i in loaded.items if i.split == "holdout")

        assert tune_count + holdout_count == 24
        # With 24 items, expect roughly 16-18 tune and 6-8 holdout
        assert 14 <= tune_count <= 20
        assert 4 <= holdout_count <= 10

    def test_same_fingerprint_same_split(self, tmp_path: Path) -> None:
        """Items sharing a video_fingerprint always land in the same split."""
        items = [
            make_item("dup_a1", fingerprint="fp_shared"),
            make_item("dup_a2", fingerprint="fp_shared"),
            make_item("unique_b", fingerprint="fp_unique"),
        ]
        assigned = assign_splits(items)
        splits = {
            i.video_fingerprint: i.split
            for i in assigned
        }
        assert splits["fp_shared"] == splits["fp_shared"]  # trivially true
        # Both dup items must have the same split
        s1 = assigned[0].split
        s2 = assigned[1].split
        assert s1 == s2, "duplicate fingerprint items must share the same split"

    def test_moved_file_detection(self, tmp_path: Path) -> None:
        """``verify_files=True`` raises when a source file is missing."""
        items = assign_splits(rewrite_source_paths(twenty_four_items(), tmp_path))
        dest = tmp_path / "manifest.json"
        freeze_manifest(items, dest)

        # Move one source file away.
        missing = Path(items[0].source_path)
        missing.rename(tmp_path / "gone.mp4")

        with pytest.raises(FileNotFoundError, match="Source file not found"):
            load_manifest(dest, verify_files=True)

    def test_load_with_verify_files_false_skips_check(
        self, tmp_path: Path
    ) -> None:
        """``verify_files=False`` should not raise even when files are missing."""
        items = assign_splits(rewrite_source_paths(twenty_four_items(), tmp_path))
        dest = tmp_path / "manifest.json"
        freeze_manifest(items, dest)

        # Remove source files.
        for item in items:
            Path(item.source_path).unlink()

        # Should not raise.
        loaded = load_manifest(dest, verify_files=False)
        assert len(loaded.items) == 24

    def test_manifest_id_integrity_check(self, tmp_path: Path) -> None:
        """Loading a tampered manifest raises ValueError."""
        items = assign_splits(twenty_four_items())
        dest = tmp_path / "manifest.json"
        freeze_manifest(items, dest)

        # Tamper with the JSON
        data = json.loads(dest.read_text())
        data["version"] = 99
        dest.write_text(json.dumps(data, sort_keys=True))

        with pytest.raises(ValueError, match="Manifest ID mismatch"):
            load_manifest(dest, verify_files=False)

    def test_fingerprint_duplicate_rejected(self, tmp_path: Path) -> None:
        """Creating a manifest with duplicate fingerprints raises."""
        items = [
            make_item("a", fingerprint="fp_same"),
            make_item("b", fingerprint="fp_same"),
        ]
        with pytest.raises(ValueError, match="Duplicate video_fingerprint"):
            freeze_manifest(items, tmp_path / "bad.json")

    def test_sorted_keys_in_json(self, tmp_path: Path) -> None:
        """Output JSON must use sorted keys for deterministic hashing."""
        items = assign_splits(twenty_four_items())
        dest = tmp_path / "manifest.json"
        freeze_manifest(items, dest)

        raw = dest.read_text()
        # The canonical_json uses sort_keys=True so keys should be sorted.
        parsed = json.loads(raw)
        keys = list(parsed.keys())
        assert keys == sorted(keys), "top-level keys must be sorted"

    def test_freeze_and_load_round_trip(self, tmp_path: Path) -> None:
        """A frozen manifest loads back as a BenchmarkManifest with all fields."""
        items = assign_splits(rewrite_source_paths(twenty_four_items(), tmp_path))
        dest = tmp_path / "manifest.json"
        mid = freeze_manifest(items, dest)

        loaded = load_manifest(dest, verify_files=True)
        assert isinstance(loaded, BenchmarkManifest)
        assert loaded.manifest_id == mid
        assert loaded.version == 1
        assert len(loaded.items) == 24


class TestAssignSplits:
    """Tests for ``assign_splits``."""

    def test_split_deterministic(self) -> None:
        """Same items produce the same split assignment."""
        items = twenty_four_items()
        a = assign_splits(items)
        b = assign_splits(items)
        for ia, ib in zip(a, b):
            assert ia.split == ib.split

    def test_all_items_have_valid_split(self) -> None:
        """Every assigned item must be 'tune' or 'holdout'."""
        items = assign_splits(twenty_four_items())
        for item in items:
            assert item.split in ("tune", "holdout")

    def test_split_order_matches_input(self) -> None:
        """assign_splits preserves input order."""
        items = twenty_four_items()
        assigned = assign_splits(items)
        for orig, new in zip(items, assigned):
            assert orig.item_id == new.item_id
            assert orig.video_fingerprint == new.video_fingerprint


class TestCLIIntegration:
    """Integration-level checks for the CLI script behaviour."""

    CLI_SCRIPT = "scripts/create_benchmark_manifest.py"

    def test_cli_help_exits_ok(self) -> None:
        """``--help`` must exit with code 0."""
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, self.CLI_SCRIPT, "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "usage" in result.stdout.lower() or "usage" in result.stderr.lower()

    def test_cli_refuses_overwrite_without_flag(
        self, tmp_path: Path
    ) -> None:
        """Writing to an existing path must fail unless ``--new-version`` is given."""
        import os
        import subprocess
        import sys

        items = twenty_four_items()
        dest = tmp_path / "benchmark.json"

        # Write CSV
        csv_path = tmp_path / "items.csv"
        with open(csv_path, "w", newline="") as f:
            f.write(
                "source_path,duration_bucket,resolution_bucket,pace_bucket,"
                "difficulty_tags,video_fingerprint\n"
            )
            for item in items:
                fake_src = tmp_path / "videos" / f"{item.item_id}.mp4"
                fake_src.parent.mkdir(parents=True, exist_ok=True)
                fake_src.write_text("content")
                f.write(
                    f"{fake_src},{item.duration_bucket},{item.resolution_bucket},"
                    f"{item.pace_bucket},action|dialogue,{item.video_fingerprint}\n"
                )

        # First run should succeed
        env = os.environ.copy()
        env["PYTHONPATH"] = str(
            Path(__file__).resolve().parents[2]
        )
        result1 = subprocess.run(
            [sys.executable, self.CLI_SCRIPT, str(csv_path), str(dest)],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result1.returncode == 0, f"First run failed: {result1.stderr}"

        # Second run without --new-version must fail
        result2 = subprocess.run(
            [sys.executable, self.CLI_SCRIPT, str(csv_path), str(dest)],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result2.returncode != 0
        assert "already exists" in result2.stderr.lower() or "new-version" in result2.stderr.lower()

    def test_cli_new_version_flag_allows_overwrite(
        self, tmp_path: Path
    ) -> None:
        """``--new-version`` should allow writing to an existing path."""
        import os
        import subprocess
        import sys

        items = twenty_four_items()
        dest = tmp_path / "benchmark.json"

        csv_path = tmp_path / "items.csv"
        with open(csv_path, "w", newline="") as f:
            f.write(
                "source_path,duration_bucket,resolution_bucket,pace_bucket,"
                "difficulty_tags,video_fingerprint\n"
            )
            for item in items:
                fake_src = tmp_path / "videos" / f"{item.item_id}.mp4"
                fake_src.parent.mkdir(parents=True, exist_ok=True)
                fake_src.write_text("content")
                f.write(
                    f"{fake_src},{item.duration_bucket},{item.resolution_bucket},"
                    f"{item.pace_bucket},action|dialogue,{item.video_fingerprint}\n"
                )

        env = os.environ.copy()
        env["PYTHONPATH"] = str(
            Path(__file__).resolve().parents[2]
        )

        # First run
        subprocess.run(
            [sys.executable, self.CLI_SCRIPT, str(csv_path), str(dest)],
            capture_output=True, text=True, env=env, check=True,
        )

        # Second run with --new-version should succeed
        result = subprocess.run(
            [
                sys.executable, self.CLI_SCRIPT,
                str(csv_path), str(dest), "--new-version",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, f"Second run failed: {result.stderr}"
