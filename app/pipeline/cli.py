"""CLI entry points for the adaptive pipeline script."""
from __future__ import annotations

import argparse
import sys

from app.pipeline.direct import run_direct_mode
from app.pipeline.stage_io import run_stage_mode


def parse_cli_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Adaptive GIF extraction from video")
    parser.add_argument("--video", default=None, help="Video file path")
    parser.add_argument(
        "--export-dir", default=None, help="Export directory for GIFs"
    )
    parser.add_argument(
        "--config",
        default=None,
        help="YAML config path (default: configs/models.yaml)",
    )
    parser.add_argument(
        "--frames-dir",
        default=None,
        help="Frame/checkpoint directory (default: data/frames/adaptive_test/<video>)",
    )
    # Stage-mode arguments
    parser.add_argument(
        "--task-stage",
        default=None,
        choices=[
            "discover",
            "sample",
            "vlm",
            "refine",
            "synthesize",
            "rank_dedup",
            "gif_clip",
            "materialize",
        ],
        help="Run in stage mode for the given stage",
    )
    parser.add_argument(
        "--task-work-dir", default=None, help="Working directory (stage mode)"
    )
    parser.add_argument(
        "--task-result",
        default=None,
        help="Path to write stage result JSON (stage mode)",
    )
    parser.add_argument(
        "--task-config",
        default=None,
        help="Path to config snapshot JSON (stage mode)",
    )
    parser.add_argument(
        "--task-input-manifest",
        default=None,
        help="Path to input manifest JSON describing upstream artifacts (stage mode)",
    )
    parser.add_argument(
        "--clip-id", default=None, help="Clip ID for gif_clip stage"
    )
    return parser.parse_args(args)


def main() -> None:
    args = parse_cli_args()

    if args.task_stage:
        # Stage mode
        if not args.video:
            print("ERROR: --video is required in stage mode", file=sys.stderr)
            sys.exit(1)
        if not args.task_work_dir or not args.task_result or not args.task_config:
            print(
                "ERROR: --task-work-dir, --task-result, and --task-config "
                "are required in stage mode",
                file=sys.stderr,
            )
            sys.exit(1)
        run_stage_mode(
            stage=args.task_stage,
            video_path=args.video,
            work_dir=args.task_work_dir,
            result_path=args.task_result,
            config_path=args.task_config,
            input_manifest_path=args.task_input_manifest,
            clip_id=args.clip_id,
        )
    else:
        # Direct mode (original behavior)
        if not args.video:
            args.video = "C:/Users/sunhao/Desktop/ToWatch/JUR-639.mp4"
        run_direct_mode(
            args.video,
            args.export_dir,
            config_path=args.config,
            frames_dir=args.frames_dir,
        )
