"""HTTP client wrapping the task API endpoints.

Provides GifAgentApiClient for the Control tab and other UI components
to interact with the FastAPI task engine endpoints.
"""

from __future__ import annotations

import httpx

API_BASE = "http://127.0.0.1:8000"


class GifAgentApiClient:
    """An HTTP client wrapping the task API endpoints using httpx.

    All methods return either the successful JSON payload (a dict or list
    depending on the endpoint) or a dict with an ``"error"`` key on failure.
    The caller should check for ``"error"`` in the return value.
    """

    def __init__(self, base_url: str = API_BASE):
        self._base = base_url.rstrip("/")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> dict | list:
        try:
            timeout = kwargs.pop("timeout", 10)
            resp = httpx.request(
                method, f"{self._base}{path}", timeout=timeout, **kwargs
            )
        except httpx.RequestError as e:
            return {"error": f"Connection failed: {e}"}

        if resp.status_code == 409:
            try:
                body = resp.json()
            except Exception:
                body = {"detail": resp.text}
            # FastAPI wraps the error detail in {"detail": original_dict}
            detail = body.get("detail", body) if isinstance(body, dict) else body
            return {
                "error": "active job exists for directory",
                "detail": detail,
            }

        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            try:
                detail = e.response.json()
            except Exception:
                detail = str(e)
            return {"error": f"HTTP {e.response.status_code}", "detail": detail}

        return resp.json()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_task(self, directory: str, limit: int, extensions: str) -> dict:
        """POST /api/tasks/jobs — create a new task job."""
        return self._request(
            "POST",
            "/api/tasks/jobs",
            json={
                "directory": directory,
                "limit": limit,
                "extensions": extensions,
            },
        )

    def cancel_task(self, job_id: str) -> dict:
        """POST /api/tasks/jobs/{job_id}/cancel — cancel a running job."""
        return self._request("POST", f"/api/tasks/jobs/{job_id}/cancel")

    def retry_task(self, job_id: str) -> dict:
        """POST /api/tasks/jobs/{job_id}/retry — retry a failed job."""
        return self._request("POST", f"/api/tasks/jobs/{job_id}/retry")

    def list_tasks(self) -> dict:
        """GET /api/tasks/jobs — list all task jobs.

        Returns a list on success (wrapped for compatibility with the
        ``-> dict`` interface, or ``{"error": ...}`` on failure).
        """
        return self._request("GET", "/api/tasks/jobs")

    def get_attention(self, limit: int = 100) -> dict:
        """GET /api/workbench/attention — return the attention inbox."""
        return self._request(
            "GET",
            "/api/workbench/attention",
            params={"limit": limit},
        )

    def task_events(self, after_id: int = 0) -> dict:
        """GET /api/tasks/events — return task events.

        Returns a list on success (wrapped for compatibility with the
        ``-> dict`` interface, or ``{"error": ...}`` on failure).
        """
        return self._request(
            "GET",
            "/api/tasks/events",
            params={"after_id": after_id, "limit": 200},
        )
