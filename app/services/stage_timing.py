"""Lightweight timing collector for adaptive pipeline stages.

The collector is deliberately additive: stages attach its ``to_dict()``
payload under a ``timings`` manifest key, and every manifest validator
must keep accepting manifests that omit it.

Samples are guarded against non-finite values because ``json.dumps``
renders ``nan`` / ``inf`` as bare ``NaN`` / ``Infinity`` tokens, which are
not valid JSON and would break strict manifest readers.
"""

from __future__ import annotations

from collections import defaultdict
import math
import statistics
import threading
import time


class _Span:
    """Context manager that records one duration sample on exit.

    Implemented as a slotted class rather than ``contextlib.contextmanager``
    because it wraps every per-frame VLM call.
    """

    __slots__ = ("_timings", "_name", "_started")

    def __init__(self, timings: "StageTimings", name: str) -> None:
        self._timings = timings
        self._name = name
        self._started = 0.0

    def __enter__(self) -> "_Span":
        self._started = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        elapsed_ms = (time.perf_counter() - self._started) * 1000.0
        self._timings.record(self._name, elapsed_ms)
        # Never swallow the body's exception; a failed ffmpeg call still
        # consumed wall time and must show up in the totals.
        return False


class StageTimings:
    """Accumulates duration samples and VLM output-token counts."""

    def __init__(self) -> None:
        self._samples: dict[str, list[float]] = defaultdict(list)
        self._vlm_output_tokens = 0
        self._vlm_observations = 0
        self._lock = threading.Lock()

    def span(self, name: str) -> _Span:
        """Return a context manager timing one occurrence of *name*."""
        return _Span(self, name)

    def record(self, name: str, ms: float) -> None:
        """Record a single duration sample, in milliseconds."""
        try:
            value = float(ms)
        except (TypeError, ValueError):
            return
        if not math.isfinite(value) or value < 0.0:
            return
        with self._lock:
            self._samples[name].append(value)

    def observe_vlm(self, eval_count: object, total_ms: float) -> None:
        """Record one VLM call's latency and its generated token count.

        ``eval_count`` comes straight from the Ollama response body and is
        absent on some error paths, so a missing value only skips the token
        tally — the latency sample is still recorded.
        """
        self.record("vlm", total_ms)
        tokens = 0
        try:
            tokens = int(eval_count)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            tokens = 0
        if tokens < 0:
            tokens = 0
        with self._lock:
            self._vlm_output_tokens += tokens
            self._vlm_observations += 1

    def to_dict(self) -> dict:
        """Return a JSON-serializable, key-ordered summary.

        Metrics without samples are omitted entirely so manifests never
        carry ``null`` placeholders.
        """
        with self._lock:
            snapshot = {name: list(values) for name, values in self._samples.items()}
            vlm_tokens = self._vlm_output_tokens
            vlm_observations = self._vlm_observations

        totals: dict[str, float] = {}
        p50: dict[str, float] = {}
        counts: dict[str, int] = {}
        for name in sorted(snapshot):
            values = snapshot[name]
            if not values:
                continue
            totals[name] = round(math.fsum(values), 3)
            p50[name] = round(statistics.median(values), 3)
            counts[name] = len(values)

        payload: dict = {}
        if counts:
            payload["counts"] = counts
            payload["p50_ms"] = p50
            payload["totals_ms"] = totals
        if vlm_observations:
            payload["vlm_output_tokens"] = vlm_tokens
        return payload
