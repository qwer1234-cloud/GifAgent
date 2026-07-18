"""Common UI utilities shared across workbench tabs."""

from __future__ import annotations

import httpx


def _format_api_error(resp: httpx.Response) -> str:
    """Format an API error response for display."""
    try:
        detail = resp.json().get("detail", resp.text)
        if isinstance(detail, dict):
            message = detail.get("message") or detail.get("error") or str(detail)
            count = detail.get("count")
            suffix = f" ({count} item(s))" if count else ""
            return f"{message}{suffix}"
        return str(detail)
    except Exception:
        return resp.text or f"HTTP {resp.status_code}"
