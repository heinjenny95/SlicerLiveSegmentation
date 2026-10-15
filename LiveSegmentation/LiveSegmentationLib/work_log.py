"""Durable study timing, deliberately independent of the voxel-sync hot path.

Each writer owns an append-only journal. A separate report lock protects the
derived TXT only, never edits, chat or presence. Journals survive compaction and
are the source of truth if replacing the human-readable report fails.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from collections import defaultdict
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

FLUSH_SECONDS = 5.0
REPORT_SECONDS = 30.0
HEARTBEAT_SECONDS = 15.0
UNOBSERVED_GAP_SECONDS = 60.0
DETAIL_KEYS = frozenset({
    "sequence", "segment_id", "segment_name", "changed_voxels", "operation_kind",
    "metadata_update", "segment_deleted", "system_snapshot", "undo_of_sequence", "color_hex",
    "conflict_count", "locked", "lock_mode", "lock_holder", "owner", "state",
    "target_user", "role", "resolution", "has_anchor", "created", "name",
})


def utc_stamp(epoch):
    return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat(timespec="milliseconds")


def merged_seconds(intervals):
    """Union, not sum: duplicate windows of one person never double-count."""
    result = 0.0
    end = None
    for low, high in sorted(intervals):
        high = max(low, high)
        result += max(0.0, high - max(low, end if end is not None else low))
        end = max(high, end if end is not None else high)
    return result


def read_records(room_path):
    records = {}
    issues = []
    for path in sorted(Path(room_path).joinpath("work-log-data").glob("*.jsonl")):
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    if (not isinstance(record, dict) or record.get("schema") != 1
                            or not isinstance(record.get("epoch"), (int, float))
                            or not isinstance(record.get("event"), str)
                            or not isinstance(record.get("user"), str)
                            or not isinstance(record.get("session_id"), str)
                            or not isinstance(record.get("details"), dict)
                            or not record.get("id")):
                        raise ValueError("invalid work-log record")
                    utc_stamp(record["epoch"])
                    records[str(record["id"])] = record
                except (ValueError, TypeError, OverflowError, OSError):
                    issues.append(f"Unreadable journal line {path.name}:{number}; not counted.")
    return sorted(records.values(), key=lambda item: (item["epoch"], item["id"])), issues


def render_report(records, issues=(), generated_at=None):
    """Explain observational limits next to all calculated publication metrics."""
    sessions = {}
    actions = []
    issues = list(issues)
    for item in records:
        key = (item.get("user", ""), item.get("session_id", ""))
        kind = item.get("event")
        if key[1] and kind in {"room.join", "room.heartbeat", "room.leave"}:
            session = sessions.setdefault(key, {"start": item["epoch"],
                                                "last": item["epoch"], "end": None,
                                                "join_observed": False, "observations": []})
            session["observations"].append(item["epoch"])
            session["start"] = min(session["start"], item["epoch"])
            session["last"] = max(session["last"], item["epoch"])
            if kind == "room.join":
                session["join_observed"] = True
            elif kind == "room.leave":
                session["end"] = item["epoch"]
        if kind != "room.heartbeat":
            actions.append(item)
    for action in actions:
        session = sessions.get((action.get("user", ""), action.get("session_id", "")))
        if session and session["start"] <= action["epoch"] <= (session["end"] or float("inf")):
            session["observations"].append(action["epoch"])
            session["last"] = max(session["last"], action["epoch"])
    intervals = defaultdict(list)
    lines = [
        "Live Segmentation - work and attendance log (schema 1)",
        f"Report generated: {utc_stamp(time.time() if generated_at is None else generated_at)} UTC",
        "Automatic since this feature was enabled; earlier work is NOT reconstructed.",
        "All times are UTC, recorded at the collaboration backend on acceptance.",
        "Shared-folder PCs must have synchronized clocks. SSH uses the server clock.",
        "Attendance is NOT active editing time. Reading, waiting and breaks are included.",
        "Activity timestamps are sync operations, NOT exact brush-stroke durations.",
        "One action can produce several voxel operations. System checkpoints are excluded.",
        "No automatic assertion that segmentation is complete: choose study start/end explicitly.",
        "Open/abruptly ended sessions count only to the last saved heartbeat; end is UNKNOWN.",
        "Confirmed actions also establish presence. Observation gaps >60 s are excluded and flagged.",
        "Journal flush <=5 s, heartbeat samples <=15 s, TXT refresh <=30 s on healthy storage.",
        "These are targets, not deadlines during storage outages; inspect warnings/gaps.",
        "Duplicate transport retries are counted once. Names are self-reported display names.",
        "Use a distinct, consistent name for each person (and distinct names for test windows).",
        "", "SESSIONS (UTC)",
    ]
    for (user, session_id), session in sorted(sessions.items(), key=lambda pair: pair[1]["start"]):
        end = session["end"] if session["end"] is not None else session["last"]
        spans = []
        low = previous = session["start"]
        for seen in sorted(set(session["observations"])):
            if seen > end:
                continue
            if seen - previous > UNOBSERVED_GAP_SECONDS:
                spans.append((low, previous))
                issues.append(f"Unobserved gap in session {session_id}: {utc_stamp(previous)} -> "
                              f"{utc_stamp(seen)}; excluded from attendance.")
                low = seen
            previous = seen
        spans.append((low, end))
        intervals[user].extend(spans)
        status = "closed" if session["end"] is not None else "open / end unconfirmed"
        if not session["join_observed"]:
            status += " / join not observed"
        lines.append(f"SESSION {json.dumps(user, ensure_ascii=False)} {session_id} | "
                     f"{utc_stamp(session['start'])} -> {utc_stamp(end)} | "
                     f"{merged_seconds(spans):.3f} observed seconds | {status}")
    lines.extend(["", "SUMMARY (attendance only; seconds)"])
    for user, spans in sorted(intervals.items()):
        count = sum(1 for key in sessions if key[0] == user)
        lines.append(f"PERSON {json.dumps(user, ensure_ascii=False)} | "
                     f"{merged_seconds(spans):.3f} seconds | {count} session(s)")
    all_spans = [span for spans in intervals.values() for span in spans]
    lines.append(f"Person-seconds (sum of per-person unions): "
                 f"{sum(merged_seconds(spans) for spans in intervals.values()):.3f}")
    lines.append(f"Team room-seconds (union across all people, parallel time counted once): "
                 f"{merged_seconds(all_spans):.3f}")
    if all_spans:
        elapsed = max(high for _, high in all_spans) - min(low for low, _ in all_spans)
        lines.append(f"Calendar span (includes overnight gaps): {max(0, elapsed):.3f}")
    edits = [a for a in actions if a["event"] == "segmentation.operation"
             and (a.get("details", {}).get("changed_voxels") or 0) > 0
             and not a.get("details", {}).get("segment_deleted")]
    if edits:
        lines.append(f"First confirmed voxel edit: {utc_stamp(edits[0]['epoch'])}")
        lines.append(f"Last confirmed voxel edit: {utc_stamp(edits[-1]['epoch'])}")
        lines.append(f"Voxel-edit timestamp span (NOT active work): "
                     f"{edits[-1]['epoch'] - edits[0]['epoch']:.3f}")
    lines.extend(["", "WARNINGS"])
    lines.extend(issues or ["None detected in saved journals; this does not prove no offline work occurred."])
    lines.extend(["", "ACTIVITY (UTC; confirmed backend actions)"])
    for action in actions:
        lines.append(f"{utc_stamp(action['epoch'])} | "
                     f"{json.dumps(action.get('user', ''), ensure_ascii=False)} | "
                     f"{action['event']} | session={action.get('session_id', '')} | "
                     f"{json.dumps(action.get('details', {}), ensure_ascii=False, sort_keys=True)}")
    return "\n".join(lines) + "\n"


class RoomWorkLog:
    """Non-blocking submission; fsync and report work live in a private lane."""

    def __init__(self, room_path, report_lock=None, create_room=False):
        self.room_path = Path(room_path)
        self.report_lock = report_lock or nullcontext
        self.create_room = create_room
        self.writer_id = uuid.uuid4().hex
        self._guard = threading.Condition()
        self._pending = []
        self._heartbeats = {}
        self._last_heartbeat = {}
        self._worker = None
        self._busy = False
        self._completed_batches = 0
        self._urgent = False
        self._last_report = 0.0
        self._dirty_report = False
        self.error = ""
        self.saved_at = ""

    def _record(self, event, user, session_id, details=None, event_id=None, epoch=None):
        record = {"schema": 1, "id": str(event_id or uuid.uuid4().hex),
                  "epoch": time.time() if epoch is None else float(epoch),
                  "event": str(event), "user": str(user), "session_id": str(session_id),
                  "details": {key: value for key, value in (details or {}).items()
                              if key in DETAIL_KEYS and isinstance(value, (str, int, float, bool, type(None)))}}
        # No masks, chat text, paths, credentials or arbitrary nested structures.
        record["details"] = {key: value[:1000] if isinstance(value, str) else value
                             for key, value in record["details"].items()}
        return record

    def submit(self, event, user, session_id, details=None, event_id=None, epoch=None):
        record = self._record(event, user, session_id, details, event_id, epoch)
        with self._guard:
            self._pending.append(record)
            self._urgent |= event in {"room.join", "room.leave"}
            self._start_worker()
            self._guard.notify_all()

    def heartbeat(self, user, session_id, epoch=None):
        key = (str(user), str(session_id))
        now = time.monotonic()
        with self._guard:
            if now - self._last_heartbeat.get(key, -HEARTBEAT_SECONDS) < HEARTBEAT_SECONDS:
                return
            self._last_heartbeat[key] = now
            self._heartbeats[key] = self._record("room.heartbeat", *key, epoch=epoch)
            self._start_worker()
            self._guard.notify_all()

    def _start_worker(self):
        # Called under the condition lock, so enqueue/idle-exit cannot race.
        if self._worker is None:
            self._worker = threading.Thread(target=self._run, name="LiveSegmentation-work-log", daemon=True)
            self._worker.start()

    def _run(self):
        deadline = time.monotonic() + FLUSH_SECONDS
        while True:
            with self._guard:
                while not self._urgent and time.monotonic() < deadline:
                    self._guard.wait(max(0, deadline - time.monotonic()))
                records = self._pending + list(self._heartbeats.values())
                self._pending = []
                self._heartbeats = {}
                urgent = self._urgent
                self._urgent = False
                if not records and not urgent:
                    if self._dirty_report:
                        report_due = self._last_report + REPORT_SECONDS
                        if time.monotonic() < report_due:
                            deadline = min(time.monotonic() + FLUSH_SECONDS, report_due)
                            continue
                        urgent = True
                    else:
                        self._worker = None
                        self._guard.notify_all()
                        return
                self._busy = True
            try:
                self._persist(records, force_report=urgent)
                self.error = ""
            except Exception as exc:
                self.error = f"Study log not fully saved: {type(exc).__name__}: {exc}"
                logging.warning(self.error)
                with self._guard:
                    # An uncertain append can repeat IDs; readback deduplicates.
                    self._pending = records + self._pending
            with self._guard:
                self._busy = False
                self._completed_batches += 1
                if self.error:
                    # Retry only when new work/heartbeat or flush wakes the lane.
                    # A failed NAS must not spawn unbounded retry threads.
                    self._worker = None
                    self._guard.notify_all()
                    return
                self._guard.notify_all()
            deadline = time.monotonic() + FLUSH_SECONDS

    def _persist(self, records, force_report=False):
        if self.create_room:
            self.room_path.mkdir(parents=True, exist_ok=True)
        folder = self.room_path / "work-log-data"
        folder.mkdir(exist_ok=True)
        if records:
            path = folder / f"{self.writer_id}.jsonl"
            data = "\n" + "".join(json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n" for item in records)
            with path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self._dirty_report = True
        self.saved_at = utc_stamp(time.time())
        if not force_report and time.monotonic() - self._last_report < REPORT_SECONDS:
            return
        with self.report_lock() if callable(self.report_lock) else self.report_lock:
            # Independent journal files are always durable before report replace.
            records, issues = read_records(self.room_path)
            text = render_report(records, issues)
            destination = self.room_path / "work-log.txt"
            temporary = self.room_path / f"work-log-{self.writer_id}.tmp"
            try:
                with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                    stream.write(text)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        self._last_report = time.monotonic()
        self._dirty_report = False

    def flush(self, timeout=5.0):
        """Explicit shutdown/test barrier. NEVER call from the Slicer GUI thread."""
        deadline = time.monotonic() + timeout
        with self._guard:
            completed_before = self._completed_batches
            self._urgent = True
            self._start_worker()
            self._guard.notify_all()
            while self._urgent or self._pending or self._heartbeats or self._busy:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or (self.error and self._completed_batches > completed_before):
                    return False
                self._guard.wait(remaining)
            return not self.error

    def status(self):
        return {"path": str(self.room_path / "work-log.txt"),
                "saved_at": self.saved_at, "error": self.error}


def operation_details(operation):
    return {key: value for key, value in operation.items() if key in DETAIL_KEYS}


def operation_event_id(sequence):
    return "operation-" + hashlib.sha256(str(sequence).encode()).hexdigest()
