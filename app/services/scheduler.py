"""Model Scheduler — manage ollama model lifecycle for VLM <-> text model swaps."""
import time
import subprocess
from typing import Optional, List, Dict, Any, Callable

import httpx

from app.config import get


class ModelScheduler:
    """Manages ollama model lifecycle for VLM <-> text model swaps."""

    def __init__(self):
        self.vlm_model = get("vlm.model", "llava:13b")
        self.llm_model = get("llm.model")
        self.base_url = get("vlm.base_url", "http://localhost:11434")
        self.switch_wait = get("scheduler.model_switch_wait", 10)
        self.max_retries = get("scheduler.max_retries", 3)

    def _run_ollama(self, cmd: List[str]) -> (int, str, str):
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return result.returncode, result.stdout, result.stderr

    def _wait_for_model(self, model: str, timeout: int = 30) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                resp = httpx.post(
                    f"{self.base_url}/api/generate",
                    json={"model": model, "prompt": "ping", "stream": False},
                    timeout=10,
                )
                if resp.status_code == 200:
                    return True
            except Exception:
                pass
            time.sleep(2)
        return False

    def stop_model(self, model: str) -> bool:
        ret, _, _ = self._run_ollama(["ollama", "stop", model])
        time.sleep(self.switch_wait)
        return ret == 0

    def switch_to_vlm(self) -> bool:
        """Stop LLM, wait for VLM to be ready."""
        self.stop_model(self.llm_model)
        for attempt in range(self.max_retries):
            if self._wait_for_model(self.vlm_model, timeout=10):
                return True
            time.sleep(5)
        return False

    def switch_to_llm(self) -> bool:
        """Stop VLM, wait for LLM to be ready."""
        self.stop_model(self.vlm_model)
        for attempt in range(self.max_retries):
            if self._wait_for_model(self.llm_model, timeout=10):
                return True
            time.sleep(5)
        return False

    def process_pending_frames(
        self, progress_callback: Optional[Callable[[int, int, str], None]] = None
    ) -> Dict[str, int]:
        """Process all pending frames through the VLM->LLM pipeline."""
        from app.db import get_connection

        conn = get_connection()
        rows = conn.execute(
            "SELECT f.frame_id, f.frame_path, f.media_id FROM frames f WHERE f.vlm_status='pending'"
        ).fetchall()

        frames = [
            {"frame_id": r["frame_id"], "frame_path": r["frame_path"], "media_id": r["media_id"]}
            for r in rows
        ]
        total = len(frames)

        if total == 0:
            return {"total": 0, "processed": 0, "failed": 0}

        # Phase 1: Switch to VLM, analyze frames in batches
        if not self.switch_to_vlm():
            raise RuntimeError("Failed to start VLM model")

        batch_size = get("vlm.batch_size", 50)
        from app.services.vision import analyze_frame

        vlm_results: List[Dict[str, Any]] = []
        for i in range(0, len(frames), batch_size):
            chunk = frames[i : i + batch_size]
            for f in chunk:
                try:
                    annotation = analyze_frame(f["frame_id"], f["frame_path"], f["media_id"])
                    vlm_results.append(
                        {
                            "frame_id": f["frame_id"],
                            "media_id": f["media_id"],
                            "status": "done",
                            "annotation": annotation,
                        }
                    )
                except Exception as e:
                    vlm_results.append(
                        {
                            "frame_id": f["frame_id"],
                            "media_id": f["media_id"],
                            "status": "failed",
                            "error": str(e),
                        }
                    )
            if progress_callback:
                progress_callback(min(i + batch_size, total), total, "vlm")

        # Phase 2: Switch to LLM, synthesize media annotations
        if not self.switch_to_llm():
            raise RuntimeError("Failed to start LLM model")

        from app.services.llm import synthesize_media_annotation

        # Group done results by media_id
        media_groups: Dict[str, List[Dict[str, Any]]] = {}
        for r in vlm_results:
            if r["status"] == "done":
                mid = r["media_id"]
                if mid not in media_groups:
                    media_groups[mid] = []
                media_groups[mid].append(r)

        processed = 0
        failed = 0
        for idx, (mid, frame_results) in enumerate(media_groups.items()):
            try:
                synthesize_media_annotation(mid, frame_results)
                processed += len(frame_results)
            except Exception:
                failed += len(frame_results)
            if progress_callback:
                progress_callback(idx + 1, len(media_groups), "llm_synthesis")

        return {"total": total, "processed": processed, "failed": failed}
