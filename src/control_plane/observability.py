"""Metrics, structured logging and the request guard rails every deployment shares.

Nothing here may log a secret value: header and payload keys that look like credentials
are reduced to their length, so an accidental ``api_key`` in a request body cannot leak.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from collections import defaultdict, deque

SECRET_KEY = re.compile(r"(api[_-]?key|password|passwd|secret|token|init[_-]?data|authorization|cookie)", re.I)
_LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)

_lock = threading.Lock()
_counters: dict[tuple[str, tuple], int] = defaultdict(int)
_kinds: dict[str, str] = {}
# path -> [bucket hits..., total milliseconds, request count]
_latency: dict[str, list] = {}
_started = time.monotonic()


def _key(labels: dict) -> tuple:
    return tuple(sorted(labels.items()))


def inc(name: str, labels: dict | None = None, value: int = 1) -> None:
    with _lock:
        _counters[(name, _key(labels or {}))] += value
        _kinds[name] = "counter"


def gauge(name: str, value, labels: dict | None = None) -> None:
    with _lock:
        _counters[(name, _key(labels or {}))] = value
        _kinds[name] = "gauge"


def observe_latency(method: str, path: str, seconds: float) -> None:
    """Record one request: buckets expose the tail, the sum exposes the mean."""
    with _lock:
        buckets = _latency.get(path)
        if buckets is None:
            buckets = _latency[path] = [0] * len(_LATENCY_BUCKETS) + [0, 0]
        index = next((i for i, limit in enumerate(_LATENCY_BUCKETS) if seconds <= limit), len(_LATENCY_BUCKETS) - 1)
        buckets[index] += 1
        buckets[-2] += int(seconds * 1000)
        buckets[-1] += 1
    inc("http_requests_total", {"method": method, "path": path})


def render() -> str:
    """Prometheus text exposition, kept dependency free on purpose."""
    lines: list[str] = []
    documented: set[str] = set()
    with _lock:
        for (name, labels), value in sorted(_counters.items(), key=lambda item: (item[0][0], str(item[0][1]))):
            if name not in documented:
                documented.add(name)
                lines.append(f"# TYPE {name} {_kinds.get(name, 'counter')}")
            rendered = ",".join(f'{label}="{label_value}"' for label, label_value in sorted(dict(labels).items()))
            lines.append(f"{name}{{{rendered}}} {value}" if rendered else f"{name} {value}")
        if _latency:
            lines.append("# TYPE http_request_duration_seconds histogram")
        for path, buckets in sorted(_latency.items()):
            cumulative = 0
            for index, limit in enumerate(_LATENCY_BUCKETS):
                cumulative += buckets[index]
                lines.append(f'http_request_duration_seconds_bucket{{path="{path}",le="{limit}"}} {cumulative}')
            count = buckets[-1]
            lines.append(f'http_request_duration_seconds_bucket{{path="{path}",le="+Inf"}} {count}')
            lines.append(f'http_request_duration_seconds_count{{path="{path}"}} {count}')
            lines.append(f'http_request_duration_seconds_sum{{path="{path}"}} {buckets[-2] / 1000:.3f}')
        lines.append("# TYPE process_uptime_seconds gauge")
        lines.append(f"process_uptime_seconds {int(time.time() - _started)}")
    return "\n".join(lines) + "\n"


class RateLimiter:
    """Fixed-window counter per caller. Each API worker owns its own budget."""

    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        if self.limit <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            if len(self._hits) > 50_000:  # never let a spoofed key space grow the process
                self._hits.clear()
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


WRITE_LIMIT = int(os.getenv("RATE_LIMIT_WRITE_PER_MINUTE", "120"))
ANON_LIMIT = int(os.getenv("RATE_LIMIT_PER_MINUTE", "300"))
write_limiter = RateLimiter(WRITE_LIMIT, 60)
anon_limiter = RateLimiter(ANON_LIMIT, 60)


class _Formatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in ("request_id", "actor", "path", "status", "duration_ms"):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging() -> logging.Logger:
    """Structured lines for this app only; other libraries keep their own handlers."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_Formatter())
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logger = logging.getLogger("control_plane")
    logger.handlers[:] = [handler]
    logger.setLevel(level)
    logger.propagate = False
    for noisy in ("httpx", "httpcore"):
        # Their request logs can contain the full panel URL, credentials included.
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logger


def log(level: str, message: str, **context) -> None:
    logging.getLogger("control_plane").log(getattr(logging, level.upper(), logging.INFO), message, extra=context)


def safe_log_context(values: dict) -> dict:
    return {key: (f"<{len(str(value))} chars>" if SECRET_KEY.search(key) else value) for key, value in values.items()}
