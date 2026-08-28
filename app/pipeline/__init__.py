"""Adaptive GIF pipeline implementation behind scripts/test_video_adaptive.py."""
from app.pipeline.config import extract_config
from app.pipeline.direct import run_direct_mode, run_pipeline
from app.pipeline.stage_io import run_stage_mode

__all__ = ["extract_config", "run_direct_mode", "run_pipeline", "run_stage_mode"]
