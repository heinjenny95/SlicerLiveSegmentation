"""Local rotating JSONL diagnostics. GUI callers never perform filesystem I/O."""
from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

_ACTIVE_LOG = None


class ResultQueue(queue.Queue):
    def put(self, item, *args, **kwargs):
        if isinstance(item, dict):
            item = {**item, "_worker_finished_monotonic": time.monotonic()}
            if item.get("error") and sys.exc_info()[0] is not None:
                item["_traceback"] = safe_text(traceback.format_exc())
        return super().put(item, *args, **kwargs)

def safe_text(value):
    text = str(value)
    text = re.sub(r"(?i)(authorization\s*[:=]\s*)(?:bearer|basic)\s+[^\s,;]+", r"\1<redacted>", text)
    text = re.sub(r'''(?ix)(["']?(?:password|passwd|api[_-]?key|access[_-]?token|access[_-]?code|token|secret)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;"'}]+)''', r"\1<redacted>", text)
    text = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1<redacted>@", text)
    return text[:32000]


class DiagnosticLog:
    """One process-owned log with asynchronous writes and explicit rotation."""
    def __init__(self, directory=None, max_bytes=10*1024*1024, backups=5):
        global _ACTIVE_LOG
        if directory is None:
            directory = Path(os.environ.get("LOCALAPPDATA") or Path.home()/".local"/"share") / "SlicerLiveSegmentation" / "diagnostics"
        self.directory = Path(directory)
        self.session_id = uuid.uuid4().hex
        self.path = self.directory / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}-{self.session_id[:8]}.jsonl")
        self.max_bytes = max(256, int(max_bytes))
        self.backups = max(1, int(backups))
        self.error = None
        self._queue = queue.SimpleQueue()
        self._closed = False
        self._thread = threading.Thread(target=self._write, name="LiveSegmentation-diagnostics", daemon=True)
        self._thread.start()
        _ACTIVE_LOG = self
        self.event("diagnostics.started", process_id=os.getpid())

    def event(self, event, **details):
        if self._closed or self.error:
            return
        # Only primitives supplied by explicit callers are accepted. No raw
        # transport payloads, masks, credentials, chat bodies or MRML objects.
        record = {"at_utc": datetime.now(timezone.utc).isoformat(), "monotonic_s": time.monotonic(),
                  "session_id": self.session_id, "event": str(event)}
        for key, value in details.items():
            if re.search(r"(?i)password|secret|token|authorization|payload|chat_body|api.?key|access.?code|private.?key", key):
                continue
            if isinstance(value, str):
                record[key] = safe_text(value)
            elif value is None or isinstance(value, (bool, int, float)):
                record[key] = value
            elif isinstance(value, (tuple, list)) and all(isinstance(v, (str, bool, int, float)) for v in value):
                record[key] = [safe_text(v) if isinstance(v, str) else v for v in value[:64]]
        self._queue.put(record)

    def _rotate(self):
        for index in range(self.backups, 0, -1):
            source = self.path if index == 1 else self.path.with_suffix(f".jsonl.{index-1}")
            target = self.path.with_suffix(f".jsonl.{index}")
            if source.exists():
                os.replace(source, target)

    def _write(self):
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            while True:
                item = self._queue.get()
                if item is None:
                    return
                data = json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n"
                if self.path.exists() and self.path.stat().st_size + len(data.encode("utf-8")) > self.max_bytes:
                    self._rotate()
                with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(data)
                self.error = None
        except Exception as exc:
            # A full/unavailable local disk must not freeze annotation. The UI
            # exposes this failure instead of pretending the logger is healthy.
            self.error = safe_text(exc)

    def close(self, timeout=1.0):
        global _ACTIVE_LOG
        if not self._closed:
            self.event("diagnostics.stopped")
            self._closed = True
            self._queue.put(None)
        self._thread.join(timeout=timeout)
        if _ACTIVE_LOG is self:
            _ACTIVE_LOG = None
