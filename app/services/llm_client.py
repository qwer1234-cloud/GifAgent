"""Shared text LLM client for local Ollama and cloud providers."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from app.config import get


@dataclass(frozen=True)
class LLMSettings:
    provider: str
    model: str
    base_url: str
    api_key_env: str
    timeout_s: float
    max_tokens: int
    temperature: float
    anthropic_version: str


def get_llm_settings() -> LLMSettings:
    provider = str(get("llm.provider", "ollama") or "ollama").lower()
    model = str(get("llm.model") or "")
    timeout_s = float(get("llm.timeout_s", 120))
    max_tokens = int(get("llm.max_tokens", 2048))
    temperature = float(get("llm.temperature", 0.3))
    anthropic_version = str(get("llm.anthropic_version", "2023-06-01"))

    if provider in {"anthropic", "anthropic_compatible"}:
        base_url = (
            str(get("llm.base_url", "") or "")
            or os.environ.get("ANTHROPIC_BASE_URL", "")
            or "https://api.anthropic.com"
        )
        api_key_env = str(get("llm.api_key_env", "ANTHROPIC_API_KEY"))
    elif provider in {"openai", "openai_compatible"}:
        base_url = (
            str(get("llm.base_url", "") or "")
            or os.environ.get("OPENAI_BASE_URL", "")
            or "https://api.openai.com/v1"
        )
        api_key_env = str(get("llm.api_key_env", "OPENAI_API_KEY"))
    else:
        base_url = str(get("llm.base_url", "http://localhost:11434"))
        api_key_env = str(get("llm.api_key_env", ""))

    return LLMSettings(
        provider=provider,
        model=model,
        base_url=base_url.rstrip("/"),
        api_key_env=api_key_env,
        timeout_s=timeout_s,
        max_tokens=max_tokens,
        temperature=temperature,
        anthropic_version=anthropic_version,
    )


def llm_model_name() -> str:
    return get_llm_settings().model


def is_local_llm() -> bool:
    return get_llm_settings().provider == "ollama"


def _require_api_key(settings: LLMSettings) -> str:
    api_key = os.environ.get(settings.api_key_env, "")
    if not api_key:
        raise RuntimeError(f"Missing API key environment variable: {settings.api_key_env}")
    return api_key


def _extract_anthropic_text(data: dict[str, Any]) -> str:
    content = data.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text") or block.get("content")
                if text:
                    parts.append(str(text))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(data.get("response", "") or data.get("thinking", ""))


def _anthropic_messages_url(base_url: str) -> str:
    if base_url.endswith("/v1/messages"):
        return base_url
    if base_url.endswith("/v1"):
        return f"{base_url}/messages"
    return f"{base_url}/v1/messages"


def _openai_chat_url(base_url: str) -> str:
    if base_url.endswith("/chat/completions"):
        return base_url
    return f"{base_url}/chat/completions"


def generate_llm_text(
    prompt: str,
    *,
    temperature: Optional[float] = None,
    timeout: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> str:
    settings = get_llm_settings()
    if not settings.model:
        raise RuntimeError("llm.model is not configured")

    temp = settings.temperature if temperature is None else temperature
    timeout_s = settings.timeout_s if timeout is None else timeout
    token_limit = settings.max_tokens if max_tokens is None else max_tokens

    if settings.provider == "ollama":
        resp = httpx.post(
            f"{settings.base_url}/api/generate",
            json={
                "model": settings.model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": temp, "num_think": 0},
            },
            timeout=timeout_s,
        )
        resp.raise_for_status()
        data = resp.json()
        return str(data.get("response", "") or data.get("thinking", ""))

    if settings.provider in {"anthropic", "anthropic_compatible"}:
        api_key = _require_api_key(settings)
        resp = httpx.post(
            _anthropic_messages_url(settings.base_url),
            headers={
                "content-type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": settings.anthropic_version,
            },
            json={
                "model": settings.model,
                "max_tokens": token_limit,
                "temperature": temp,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=timeout_s,
        )
        resp.raise_for_status()
        return _extract_anthropic_text(resp.json())

    if settings.provider in {"openai", "openai_compatible"}:
        api_key = _require_api_key(settings)
        resp = httpx.post(
            _openai_chat_url(settings.base_url),
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {api_key}",
            },
            json={
                "model": settings.model,
                "temperature": temp,
                "max_tokens": token_limit,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=timeout_s,
        )
        resp.raise_for_status()
        choices = resp.json().get("choices", [])
        if choices:
            return str(choices[0].get("message", {}).get("content", ""))
        return ""

    raise RuntimeError(f"Unsupported llm.provider: {settings.provider}")


def wait_for_llm(timeout_s: int = 30) -> bool:
    settings = get_llm_settings()
    if settings.provider != "ollama":
        return bool(os.environ.get(settings.api_key_env, ""))

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            generate_llm_text("ping", timeout=10, max_tokens=8)
            return True
        except Exception:
            time.sleep(2)
    return False
