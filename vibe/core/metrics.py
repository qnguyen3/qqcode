from __future__ import annotations

import atexit
from collections import Counter
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vibe.core.config import DebugConfig, TelemetryConfig

logger = logging.getLogger(__name__)

COUNTER_TURNS = "turns"
COUNTER_COMPACTIONS = "compactions"
COUNTER_OVERFLOWS = "overflows"
COUNTER_TOOL_FAILURES = "tool_failures"
COUNTER_NAMES = (
    COUNTER_TURNS,
    COUNTER_COMPACTIONS,
    COUNTER_OVERFLOWS,
    COUNTER_TOOL_FAILURES,
)
DEFAULT_FLUSH_INTERVAL = 5.0
METRICS_VERSION = 1


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically replace ``path`` with ``payload`` serialized as JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


class MetricsRecorder:
    """In-memory local counters flushed atomically to a JSON file.

    Nothing leaves the machine: this is the ``telemetry.mode = "local"``
    implementation. Counters are flushed at most once per
    ``flush_interval`` seconds (and unconditionally on :meth:`flush` and at
    interpreter exit).
    """

    def __init__(
        self, path: Path, *, flush_interval: float = DEFAULT_FLUSH_INTERVAL
    ) -> None:
        self.path = Path(path).expanduser()
        self.flush_interval = max(0.0, flush_interval)
        self._counters: Counter[str] = Counter()
        self._lock = threading.Lock()
        self._last_flush = time.monotonic()
        atexit.register(self.flush)

    def increment(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] += amount
        self.maybe_flush()

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counters)

    def maybe_flush(self, *, force: bool = False) -> None:
        with self._lock:
            now = time.monotonic()
            if not force and now - self._last_flush < self.flush_interval:
                return
            self._last_flush = now
            counters = dict(self._counters)

        payload = {
            "version": METRICS_VERSION,
            "updated_at": _now_iso(),
            "counters": counters,
        }
        try:
            atomic_write_json(self.path, payload)
        except OSError as exc:
            logger.warning("Could not write metrics file %s: %s", self.path, exc)

    def flush(self) -> None:
        self.maybe_flush(force=True)


class DebugLogger:
    """Opt-in structured debug log recording request metadata only.

    Message contents are never written: records carry the model alias, message
    count, estimated context tokens and request duration.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.Lock()

    def log_request(
        self,
        *,
        model: str,
        message_count: int,
        estimated_tokens: int,
        duration: float,
        tool_count: int = 0,
    ) -> None:
        entry = {
            "ts": _now_iso(),
            "event": "request",
            "model": model,
            "message_count": message_count,
            "estimated_tokens": estimated_tokens,
            "duration": round(duration, 6),
            "tool_count": tool_count,
        }
        line = json.dumps(entry, sort_keys=True)
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(f"{line}\n")
                    handle.flush()
            except OSError as exc:
                logger.warning("Could not write debug log %s: %s", self.path, exc)


def build_metrics_recorder(config: TelemetryConfig) -> MetricsRecorder | None:
    """Create a recorder when local telemetry is enabled, else ``None``."""
    if config.mode != "local":
        return None
    return MetricsRecorder(
        Path(config.metrics_path), flush_interval=config.flush_interval
    )


def build_debug_logger(config: DebugConfig) -> DebugLogger | None:
    """Create a debug logger when ``debug.enabled`` is set, else ``None``."""
    if not config.enabled:
        return None
    return DebugLogger(Path(config.log_path))
