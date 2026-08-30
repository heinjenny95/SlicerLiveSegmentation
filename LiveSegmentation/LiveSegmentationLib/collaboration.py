"""Live collaboration helpers and Slicer controller for standard segmentation nodes.

The transport uses ordered, idempotent voxel operations.  Each operation contains
only voxels that changed since the client's last synchronized mask.  Applying the
shared order in sequence preserves independent edits and gives overlapping voxels
a deterministic last-operation-wins result.  Rooms may use either the HTTP server
or a shared SMB/network folder without a custom server process.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import json
import os
import queue
import re
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

try:
    from .features import (
        REVIEW_STATES,
        ROOM_ROLES,
        build_invitation,
        operation_overlap_count,
        operation_summary,
        parse_invitation,
        reconstruct_snapshot_operations,
        sha256_file,
        stable_user_color,
        validate_material_template,
    )
except ImportError:  # regular-Python transport tests import this file directly
    from features import (  # type: ignore
        REVIEW_STATES,
        ROOM_ROLES,
        build_invitation,
        operation_overlap_count,
        operation_summary,
        parse_invitation,
        reconstruct_snapshot_operations,
        sha256_file,
        stable_user_color,
        validate_material_template,
    )

LIVE_ENCODING = "zlib-packbits-v1"
SHARED_FOLDER_SCHEMA_VERSION = 1


class LiveCollaborationError(RuntimeError):
    """A user-facing live-room transport or validation error."""


def normalize_server_url(value):
    """Return a normalized HTTP(S) server root or raise a clear error."""
    value = str(value or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Enter a complete server URL such as http://127.0.0.1:8000")
    return value


def normalize_shared_folder(value):
    """Return an absolute shared-folder path or raise a clear error."""
    value = os.path.expandvars(os.path.expanduser(str(value or "").strip()))
    if not value:
        raise ValueError("Choose a shared/network folder")
    return Path(os.path.abspath(value))


def _utc_iso():
    return datetime.now(timezone.utc).isoformat()


def _safe_file_component(value, fallback="item", max_length=48):
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip())
    text = text.strip(" .-_") or fallback
    return text[: int(max_length)]


def _read_json_file(path):
    try:
        with Path(path).open("r", encoding="utf-8") as source:
            return json.load(source)
    except (OSError, ValueError) as exc:
        raise LiveCollaborationError(f"Could not read shared file {path}: {exc}") from exc


def _atomic_temporary_path(destination):
    """Return a collision-resistant same-directory path without repeating a long name."""
    destination = Path(destination)
    token = f"{os.getpid():x}-{uuid.uuid4().hex[:12]}"
    return destination.with_name(f".tmp-{token}")


def _write_json_atomic(path, payload):
    """Publish one complete JSON document using a same-directory rename."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = _atomic_temporary_path(destination)
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as output:
            json.dump(payload, output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    except OSError as exc:
        raise LiveCollaborationError(
            f"Could not write to the shared folder {destination.parent}: {exc}"
        ) from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return destination


def _write_presence_json(path, payload):
    """Update ephemeral presence on shares that reject replace-over-existing.

    Some SMB deployments allow creating and updating a file but reject an
    atomic rename when the destination already exists. Presence is transient
    and readers already ignore incomplete JSON, so a direct-overwrite fallback
    is safe here. Room metadata and segmentation operations continue to use
    only atomic publication.
    """
    destination = Path(path)
    try:
        return _write_json_atomic(destination, payload)
    except LiveCollaborationError as atomic_error:
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("w", encoding="utf-8", newline="\n") as output:
                json.dump(
                    payload,
                    output,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
        except OSError as direct_error:
            raise LiveCollaborationError(
                "Could not update presence in the shared folder "
                f"{destination.parent}. Atomic replace failed: {atomic_error}. "
                f"Direct overwrite failed: {direct_error}"
            ) from direct_error
        return destination


def volume_signature(array, spacing=None, origin=None, ijk_to_ras=None, sample_count=4096):
    """Create a cheap, stable identity check without copying a complete volume."""
    array = np.asarray(array)
    if array.ndim != 3 or any(int(size) <= 0 for size in array.shape):
        raise ValueError("Live collaboration requires a non-empty 3D volume")
    flat = array.reshape(-1)
    count = min(max(1, int(sample_count)), int(flat.size))
    indexes = np.linspace(0, flat.size - 1, num=count, dtype=np.int64)
    sampled = np.ascontiguousarray(flat[indexes])
    metadata = {
        "shape": [int(value) for value in array.shape],
        "dtype": str(array.dtype),
        "spacing": [round(float(value), 8) for value in (spacing or ())],
        "origin": [round(float(value), 8) for value in (origin or ())],
        "ijk_to_ras": [round(float(value), 8) for value in (ijk_to_ras or ())],
    }
    digest = hashlib.sha256(
        json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    digest.update(sampled.tobytes())
    return digest.hexdigest()


def _delta_bounds(changed):
    coordinates = np.nonzero(changed)
    if not coordinates[0].size:
        return None
    return [
        int(coordinates[0].min()),
        int(coordinates[0].max()) + 1,
        int(coordinates[1].min()),
        int(coordinates[1].max()) + 1,
        int(coordinates[2].min()),
        int(coordinates[2].max()) + 1,
    ]


def encode_mask_delta(previous, current, replace=False):
    """Encode changed boolean voxels in a compact, deterministic operation payload."""
    current = np.asarray(current, dtype=np.uint8)
    if current.ndim != 3:
        raise ValueError("A live mask must be three-dimensional")
    if replace:
        changed = np.ones(current.shape, dtype=bool)
    else:
        previous = np.asarray(previous, dtype=np.uint8)
        if previous.shape != current.shape:
            raise ValueError("Previous and current masks must have identical shapes")
        changed = previous != current
    bounds = _delta_bounds(changed)
    if bounds is None:
        return None
    z0, z1, y0, y1, x0, x1 = bounds
    slices = (slice(z0, z1), slice(y0, y1), slice(x0, x1))
    changed_crop = changed[slices].reshape(-1)
    value_crop = (current[slices].reshape(-1) != 0)
    raw = np.packbits(changed_crop, bitorder="little").tobytes()
    raw += np.packbits(value_crop, bitorder="little").tobytes()
    return {
        "operation_kind": "snapshot" if replace else "patch",
        "volume_shape": [int(value) for value in current.shape],
        "voxel_bbox": bounds,
        "encoding": LIVE_ENCODING,
        "payload": base64.b64encode(zlib.compress(raw, level=6)).decode("ascii"),
    }


def decode_mask_delta(operation):
    """Return crop-local changed and value arrays for one encoded operation."""
    if operation.get("encoding") != LIVE_ENCODING:
        raise ValueError("Unsupported live-operation encoding")
    bounds = [int(value) for value in operation["voxel_bbox"]]
    z0, z1, y0, y1, x0, x1 = bounds
    crop_shape = (z1 - z0, y1 - y0, x1 - x0)
    if any(size <= 0 for size in crop_shape):
        raise ValueError("Live-operation bounds are empty")
    voxel_count = int(np.prod(crop_shape, dtype=np.int64))
    packed_bytes = (voxel_count + 7) // 8
    raw = zlib.decompress(base64.b64decode(operation["payload"], validate=True))
    if len(raw) != packed_bytes * 2:
        raise ValueError("Live-operation payload length does not match its bounds")
    changed = np.unpackbits(
        np.frombuffer(raw[:packed_bytes], dtype=np.uint8), bitorder="little"
    )[:voxel_count]
    values = np.unpackbits(
        np.frombuffer(raw[packed_bytes:], dtype=np.uint8), bitorder="little"
    )[:voxel_count]
    return changed.reshape(crop_shape).astype(bool), values.reshape(crop_shape).astype(np.uint8)


def apply_mask_delta(mask, operation):
    """Apply one operation using explicit set/unset values inside its changed voxels."""
    mask = np.asarray(mask, dtype=np.uint8)
    expected_shape = tuple(int(value) for value in operation["volume_shape"])
    if mask.shape != expected_shape:
        raise ValueError("Live operation belongs to a different volume geometry")
    result = np.zeros_like(mask) if operation.get("operation_kind") == "snapshot" else mask.copy()
    z0, z1, y0, y1, x0, x1 = [int(value) for value in operation["voxel_bbox"]]
    if z1 > result.shape[0] or y1 > result.shape[1] or x1 > result.shape[2]:
        raise ValueError("Live-operation bounds exceed the volume")
    changed, values = decode_mask_delta(operation)
    crop = result[z0:z1, y0:y1, x0:x1]
    crop[changed] = values[changed]
    return result


class LiveRoomClient:
    """Small dependency-free HTTP client usable inside Slicer's Python runtime."""

    def __init__(self, server_url, user_name, api_key="", timeout_seconds=3.0):
        self.server_url = normalize_server_url(server_url)
        self.user_name = str(user_name or "").strip()
        if not self.user_name:
            raise ValueError("Enter your display name")
        self.api_key = str(api_key or "").strip()
        self.timeout_seconds = float(timeout_seconds)

    def _request(self, method, path, payload=None):
        body = None
        headers = {"Accept": "application/json", "X-LiveSeg-User": self.user_name}
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            f"{self.server_url}{path}", data=body, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                content = response.read()
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("detail")
            except Exception:
                detail = str(exc)
            if isinstance(detail, dict):
                detail = detail.get("message") or json.dumps(detail, ensure_ascii=False)
            raise LiveCollaborationError(str(detail or exc)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LiveCollaborationError(f"Server connection failed: {exc}") from exc
        return json.loads(content.decode("utf-8")) if content else None

    def join(self, room_name, signature):
        return self._request(
            "POST",
            "/api/live/rooms/join",
            {"room_name": room_name, "volume_signature": signature},
        )

    def push_operation(self, room_id, operation):
        return self._request("POST", f"/api/live/rooms/{room_id}/operations", operation)

    def operations(self, room_id, after_sequence, limit=500):
        query = urllib.parse.urlencode({"after": int(after_sequence), "limit": int(limit)})
        return self._request("GET", f"/api/live/rooms/{room_id}/operations?{query}")

    def presence(self, room_id, details):
        return self._request("POST", f"/api/live/rooms/{room_id}/presence", details)

    def health_check(self, room_id=None):
        del room_id
        result = self._request("GET", "/health")
        if not isinstance(result, dict) or result.get("status") != "ok":
            raise LiveCollaborationError("The collaboration server returned an invalid health status")
        return result

    def leave(self, room_id):
        return self._request("DELETE", f"/api/live/rooms/{room_id}/presence")

    def send_chat(self, room_id, text, client_message_id, anchor=None):
        payload = {"text": text, "client_message_id": client_message_id}
        if isinstance(anchor, dict) and anchor:
            payload["anchor"] = anchor
        return self._request(
            "POST",
            f"/api/live/rooms/{room_id}/messages",
            payload,
        )

    def chat_messages(self, room_id, after_sequence, limit=500):
        query = urllib.parse.urlencode({"after": int(after_sequence), "limit": int(limit)})
        return self._request("GET", f"/api/live/rooms/{room_id}/messages?{query}")

    def segment_locks(self, room_id):
        return self._request("GET", f"/api/live/rooms/{room_id}/locks")

    def set_segment_lock(self, room_id, segment_id, locked, expires_minutes=0):
        encoded_segment_id = urllib.parse.quote(str(segment_id), safe="")
        return self._request(
            "PUT",
            f"/api/live/rooms/{room_id}/locks/{encoded_segment_id}",
            {"locked": bool(locked), "expires_minutes": int(expires_minutes or 0)},
        )

    def room_history(self, room_id, limit=500):
        operations = []
        after = 0
        while True:
            page = self.operations(room_id, after, 500)
            if not page:
                break
            operations.extend(page)
            after = int(page[-1]["sequence"])
            if len(page) < 500:
                break
        return [
            operation_summary(item, decode_mask_delta)
            for item in operations[-max(1, int(limit)) :]
        ]

    def snapshot_manifests(self, room_id):
        history = self.room_history(room_id, 5000)
        grouped = {}
        for item in history:
            group_id = item.get("snapshot_group_id")
            if not group_id:
                continue
            state = grouped.setdefault(
                group_id,
                {
                    "id": group_id,
                    "first_sequence": int(item["sequence"]),
                    "last_sequence": int(item["sequence"]),
                    "created_at": item.get("created_at"),
                    "created_by": item.get("author"),
                    "compacted": False,
                    "label": item.get("snapshot_label") or "",
                },
            )
            state["first_sequence"] = min(state["first_sequence"], int(item["sequence"]))
            state["last_sequence"] = max(state["last_sequence"], int(item["sequence"]))
        return sorted(grouped.values(), key=lambda item: item["last_sequence"])

    def state_at_sequence(self, room_id, sequence):
        operations = []
        after = 0
        target = int(sequence)
        while True:
            page = self.operations(room_id, after, 500)
            if not page:
                break
            operations.extend(page)
            after = int(page[-1]["sequence"])
            if after >= target or len(page) < 500:
                break
        return reconstruct_snapshot_operations(
            operations, target, apply_mask_delta, encode_mask_delta
        )

    def publish_room_snapshot(self, room_id, segment_operations, compact=True, label=""):
        del compact
        group_id = str(uuid.uuid4())
        results = []
        for index, operation in enumerate(segment_operations):
            stored = {
                **operation,
                "client_operation_id": f"snapshot-{group_id}-{index}",
                "operation_kind": "snapshot",
                "snapshot_group_id": group_id,
                "snapshot_group_index": index,
                "snapshot_group_count": len(segment_operations),
                "system_snapshot": True,
                "snapshot_label": str(label or "")[:200],
            }
            results.append(self.push_operation(room_id, stored))
        return {
            "id": group_id,
            "segment_count": len(results),
            "last_sequence": int(results[-1]["sequence"]) if results else 0,
            "compacted": False,
            "label": str(label or "")[:200],
        }

    def room_conflicts(self, room_id, unresolved_only=False):
        query = urllib.parse.urlencode({"unresolved_only": str(bool(unresolved_only)).lower()})
        return self._request("GET", f"/api/live/rooms/{room_id}/conflicts?{query}")

    def resolve_conflict(self, room_id, conflict_id, resolution):
        encoded = urllib.parse.quote(str(conflict_id), safe="")
        return self._request(
            "PUT", f"/api/live/rooms/{room_id}/conflicts/{encoded}", {"resolution": resolution}
        )

    def room_roles(self, room_id):
        return self._request("GET", f"/api/live/rooms/{room_id}/roles")

    def set_room_role(self, room_id, target_user, role):
        encoded = urllib.parse.quote(str(target_user), safe="")
        return self._request(
            "PUT", f"/api/live/rooms/{room_id}/roles/{encoded}", {"role": role}
        )

    def review_states(self, room_id):
        return self._request("GET", f"/api/live/rooms/{room_id}/reviews")

    def set_review_state(self, room_id, segment_id, state, note=""):
        encoded = urllib.parse.quote(str(segment_id), safe="")
        return self._request(
            "PUT", f"/api/live/rooms/{room_id}/reviews/{encoded}", {"state": state, "note": note}
        )

    def request_segment_access(self, room_id, segment_id, message=""):
        return self._request(
            "POST",
            f"/api/live/rooms/{room_id}/access-requests",
            {"segment_id": segment_id, "message": message},
        )

    def segment_access_requests(self, room_id, segment_id=None):
        query = urllib.parse.urlencode({"segment_id": segment_id}) if segment_id else ""
        return self._request(
            "GET", f"/api/live/rooms/{room_id}/access-requests" + (f"?{query}" if query else "")
        )

    def transfer_segment_owner(self, room_id, segment_id, target_user):
        encoded = urllib.parse.quote(str(segment_id), safe="")
        return self._request(
            "POST", f"/api/live/rooms/{room_id}/locks/{encoded}/transfer", {"target_user": target_user}
        )

    def get_material_template(self, room_id):
        return self._request("GET", f"/api/live/rooms/{room_id}/material-template")

    def set_material_template(self, room_id, payload):
        return self._request("PUT", f"/api/live/rooms/{room_id}/material-template", payload)

    def audit_events(self, room_id, limit=500):
        query = urllib.parse.urlencode({"limit": int(limit)})
        return self._request("GET", f"/api/live/rooms/{room_id}/audit?{query}")

    def diagnostics(self, room_id):
        started = time.monotonic()
        self.health_check(room_id)
        return {
            "status": "ok",
            "latency_seconds": round(time.monotonic() - started, 4),
            "transport": "server",
            "server_url": self.server_url,
        }

    def reserve_project_backup(self, room_id, interval_seconds):
        del room_id, interval_seconds
        return None


class SharedFolderRoomClient:
    """Ordered live-room transport backed only by a shared filesystem folder."""

    def __init__(
        self,
        shared_folder,
        user_name,
        lock_timeout_seconds=10.0,
        stale_lock_seconds=60.0,
        presence_ttl_seconds=10.0,
    ):
        self.shared_folder = normalize_shared_folder(shared_folder)
        self.user_name = str(user_name or "").strip()
        if not self.user_name:
            raise ValueError("Enter your display name")
        self.lock_timeout_seconds = float(lock_timeout_seconds)
        self.stale_lock_seconds = float(stale_lock_seconds)
        self.presence_ttl_seconds = float(presence_ttl_seconds)
        self.rooms_root = self.shared_folder / "LiveSegmentation" / "rooms"
        self._room_id = None
        self._room_path = None

    @staticmethod
    def _room_key(room_name):
        normalized = " ".join(str(room_name or "").split()).casefold()
        if not normalized:
            raise ValueError("Enter a room name that all collaborators will use")
        readable = _safe_file_component(normalized, fallback="room", max_length=36)
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
        return f"{readable}--{digest}"

    @staticmethod
    def _operation_sequence(path):
        match = re.match(r"^(\d{20})--[a-f0-9]+\.json$", Path(path).name)
        return int(match.group(1)) if match else None

    @staticmethod
    def _message_sequence(path):
        match = re.match(r"^(\d{20})--[a-f0-9]+\.json$", Path(path).name)
        return int(match.group(1)) if match else None

    def _require_room(self, room_id):
        if self._room_path is None or self._room_id != str(room_id):
            raise LiveCollaborationError("Join the shared room before synchronizing")
        return self._room_path

    def _latest_sequence(self, room_path):
        latest = 0
        operations_path = room_path / "operations"
        try:
            candidates = operations_path.glob("*.json")
            for path in candidates:
                sequence = self._operation_sequence(path)
                if sequence is not None:
                    latest = max(latest, sequence)
        except OSError as exc:
            raise LiveCollaborationError(
                f"Could not list shared operations in {operations_path}: {exc}"
            ) from exc
        return latest

    @contextmanager
    def _sequence_lock(self, room_path):
        lock_path = room_path / "sequence.lock"
        token = uuid.uuid4().hex
        deadline = time.monotonic() + self.lock_timeout_seconds
        while True:
            try:
                lock_path.mkdir()
            except FileExistsError:
                try:
                    age = time.time() - lock_path.stat().st_mtime
                except OSError:
                    age = 0.0
                if age > self.stale_lock_seconds:
                    stale_path = room_path / f"sequence.lock.stale-{uuid.uuid4().hex}"
                    try:
                        os.replace(lock_path, stale_path)
                        owner_path = stale_path / "owner.json"
                        owner_path.unlink(missing_ok=True)
                        stale_path.rmdir()
                    except OSError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise LiveCollaborationError(
                        "The shared folder is busy. Try joining or saving again in a moment."
                    )
                time.sleep(0.04)
                continue
            except PermissionError as exc:
                # Windows and some SMB clients may report a transient access-denied
                # error while another process is removing the lock directory.
                # Treat it like lock contention while the room itself is reachable.
                if time.monotonic() < deadline and room_path.is_dir():
                    time.sleep(0.04)
                    continue
                raise LiveCollaborationError(
                    f"Could not lock the shared folder {room_path}: {exc}"
                ) from exc
            except OSError as exc:
                raise LiveCollaborationError(
                    f"Could not lock the shared folder {room_path}: {exc}"
                ) from exc

            try:
                _write_json_atomic(
                    lock_path / "owner.json",
                    {"token": token, "user": self.user_name, "created_at": _utc_iso()},
                )
                break
            except Exception:
                try:
                    lock_path.joinpath("owner.json").unlink(missing_ok=True)
                    lock_path.rmdir()
                except OSError:
                    pass
                raise
        try:
            yield
        finally:
            try:
                owner_path = lock_path / "owner.json"
                owner = _read_json_file(owner_path) if owner_path.is_file() else {}
                if owner.get("token") == token:
                    owner_path.unlink(missing_ok=True)
                    lock_path.rmdir()
            except (OSError, LiveCollaborationError):
                pass

    def join(self, room_name, signature):
        room_key = self._room_key(room_name)
        room_path = self.rooms_root / room_key
        try:
            room_path.mkdir(parents=True, exist_ok=True)
            (room_path / "operations").mkdir(exist_ok=True)
            (room_path / "presence").mkdir(exist_ok=True)
            (room_path / "chat").mkdir(exist_ok=True)
            (room_path / "locks").mkdir(exist_ok=True)
            (room_path / "backups").mkdir(exist_ok=True)
            (room_path / "snapshots").mkdir(exist_ok=True)
            (room_path / "operation-archives").mkdir(exist_ok=True)
            (room_path / "conflicts").mkdir(exist_ok=True)
            (room_path / "roles").mkdir(exist_ok=True)
            (room_path / "reviews").mkdir(exist_ok=True)
            (room_path / "access-requests").mkdir(exist_ok=True)
            (room_path / "audit").mkdir(exist_ok=True)
        except OSError as exc:
            raise LiveCollaborationError(
                f"The shared folder is not writable: {self.shared_folder}: {exc}"
            ) from exc

        metadata_path = room_path / "room.json"
        created = False
        with self._sequence_lock(room_path):
            if metadata_path.is_file():
                metadata = _read_json_file(metadata_path)
                if metadata.get("volume_signature") != str(signature):
                    raise LiveCollaborationError(
                        "This room uses a different source volume. Choose another room "
                        "or load exactly the same dataset."
                    )
            else:
                metadata = {
                    "schema_version": SHARED_FOLDER_SCHEMA_VERSION,
                    "name": str(room_name).strip(),
                    "room_id": room_key,
                    "volume_signature": str(signature),
                    "created_by": self.user_name,
                    "created_at": _utc_iso(),
                }
                _write_json_atomic(metadata_path, metadata)
                created = True

        if int(metadata.get("schema_version", 0)) != SHARED_FOLDER_SCHEMA_VERSION:
            raise LiveCollaborationError("Unsupported shared-room format")
        self._room_id = room_key
        self._room_path = room_path
        self._append_audit(room_path, "room.join", {"created": created})
        return {
            "id": room_key,
            "name": metadata.get("name") or str(room_name).strip(),
            "created": created,
            "created_by": metadata.get("created_by"),
            "latest_sequence": self._latest_sequence(room_path),
            "presence": self._read_presence(room_path),
        }

    def push_operation(self, room_id, operation):
        room_path = self._require_room(room_id)
        operation_id = str(operation.get("client_operation_id") or "").strip()
        if not operation_id:
            raise LiveCollaborationError("Live operation is missing its client ID")
        operation_hash = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:20]
        operations_path = room_path / "operations"
        conflicts = []
        with self._sequence_lock(room_path):
            existing = sorted(operations_path.glob(f"*--{operation_hash}.json"))
            if existing:
                sequence = self._operation_sequence(existing[0])
                return {"sequence": sequence, "duplicate": True}
            segment_id = str(operation.get("segment_id") or "")
            if self._role_for(room_path, self.user_name) == "viewer":
                raise LiveCollaborationError("Viewers cannot edit labels")
            lock_path = self._segment_lock_path(room_path, segment_id)
            if lock_path.is_file():
                lock_state = _read_json_file(lock_path)
                expires_epoch = float(lock_state.get("expires_epoch", 0.0) or 0.0)
                active_lock = bool(lock_state.get("locked")) and not (
                    expires_epoch and time.time() >= expires_epoch
                )
                if active_lock and lock_state.get("owner") != self.user_name:
                    raise LiveCollaborationError(
                        f"Label is locked by {lock_state.get('owner') or 'another user'}"
                    )
            base_sequence = int(operation.get("base_sequence", 0) or 0)
            if base_sequence > 0 and operation.get("operation_kind") != "snapshot":
                for previous in self._all_operation_records(room_path):
                    if int(previous.get("sequence", 0)) <= base_sequence:
                        continue
                    if previous.get("author") == self.user_name:
                        continue
                    if previous.get("system_snapshot"):
                        continue
                    try:
                        overlap = operation_overlap_count(
                            previous, operation, decode_mask_delta
                        )
                    except Exception:
                        overlap = 0
                    if overlap:
                        conflicts.append(
                            {
                                "id": str(uuid.uuid4()),
                                "segment_id": segment_id,
                                "other_author": previous.get("author"),
                                "other_sequence": int(previous.get("sequence", 0)),
                                "overlap_voxels": overlap,
                            }
                        )
            sequence = self._latest_sequence(room_path) + 1
            stored = {
                **operation,
                "sequence": sequence,
                "author": self.user_name,
                "created_at": _utc_iso(),
            }
            try:
                stored["changed_voxels"] = int(
                    np.count_nonzero(decode_mask_delta(stored)[0])
                )
            except Exception:
                pass
            destination = operations_path / f"{sequence:020d}--{operation_hash}.json"
            _write_json_atomic(destination, stored)
            for conflict in conflicts:
                conflict.update(
                    {
                        "sequence": sequence,
                        "author": self.user_name,
                        "created_at": _utc_iso(),
                        "resolution": "unresolved",
                    }
                )
                _write_json_atomic(
                    room_path / "conflicts" / f"{sequence:020d}--{conflict['id']}.json",
                    conflict,
                )
        self._append_audit(
            room_path,
            "segmentation.operation",
            {
                "sequence": sequence,
                "segment_id": stored.get("segment_id"),
                "changed_voxels": stored.get("changed_voxels"),
                "conflict_count": len(conflicts),
            },
        )
        result = {"sequence": sequence, "duplicate": False}
        if conflicts:
            result["conflicts"] = conflicts
        return result

    def operations(self, room_id, after_sequence, limit=500):
        room_path = self._require_room(room_id)
        after_sequence = int(after_sequence)
        limit = max(1, min(int(limit), 5000))
        selected = []
        try:
            for path in room_path.joinpath("operations").glob("*.json"):
                sequence = self._operation_sequence(path)
                if sequence is not None and sequence > after_sequence:
                    selected.append((sequence, path))
        except OSError as exc:
            raise LiveCollaborationError(f"Could not list shared operations: {exc}") from exc
        operations = []
        for sequence, path in sorted(selected)[:limit]:
            operation = _read_json_file(path)
            operation["sequence"] = sequence
            operations.append(operation)
        return operations

    def _presence_path(self, room_path):
        readable = _safe_file_component(self.user_name, fallback="user", max_length=28)
        digest = hashlib.sha256(self.user_name.encode("utf-8")).hexdigest()[:12]
        return room_path / "presence" / f"{readable}--{digest}.json"

    def _read_presence(self, room_path):
        now = time.time()
        users = []
        try:
            paths = list(room_path.joinpath("presence").glob("*.json"))
        except OSError as exc:
            raise LiveCollaborationError(f"Could not list shared presence: {exc}") from exc
        for path in paths:
            try:
                data = _read_json_file(path)
                if now - float(data.get("last_seen_epoch", 0.0)) <= self.presence_ttl_seconds:
                    data.pop("last_seen_epoch", None)
                    users.append(data)
            except (LiveCollaborationError, TypeError, ValueError):
                continue
        return sorted(users, key=lambda item: str(item.get("user", "")).casefold())

    def presence(self, room_id, details):
        room_path = self._require_room(room_id)
        data = {
            "user": self.user_name,
            "last_seen": _utc_iso(),
            "last_seen_epoch": time.time(),
            **(details or {}),
        }
        _write_presence_json(self._presence_path(room_path), data)
        return self._read_presence(room_path)

    def health_check(self, room_id=None):
        room_path = self._require_room(room_id or self._room_id)
        metadata = _read_json_file(room_path / "room.json")
        if metadata.get("room_id") != self._room_id:
            raise LiveCollaborationError("The shared-room metadata no longer matches this session")
        try:
            room_path.joinpath("operations").stat()
            room_path.joinpath("presence").stat()
        except OSError as exc:
            raise LiveCollaborationError(
                f"The shared folder is unavailable or responding incorrectly: {exc}"
            ) from exc
        return {"status": "ok", "room_id": self._room_id}

    def leave(self, room_id):
        room_path = self._require_room(room_id)
        try:
            self._append_audit(room_path, "room.leave")
        except Exception:
            pass
        try:
            self._presence_path(room_path).unlink(missing_ok=True)
        except OSError:
            pass
        self._room_id = None
        self._room_path = None
        return {"left": True}

    def _latest_message_sequence(self, room_path):
        latest = 0
        try:
            paths = room_path.joinpath("chat").glob("*.json")
            for path in paths:
                sequence = self._message_sequence(path)
                if sequence is not None:
                    latest = max(latest, sequence)
        except OSError as exc:
            raise LiveCollaborationError(f"Could not list shared chat messages: {exc}") from exc
        return latest

    def send_chat(self, room_id, text, client_message_id, anchor=None):
        room_path = self._require_room(room_id)
        text = str(text or "").strip()
        if not text:
            raise LiveCollaborationError("Enter a chat message")
        if len(text) > 4000:
            raise LiveCollaborationError("Chat messages are limited to 4000 characters")
        message_id = str(client_message_id or "").strip()
        if not message_id:
            raise LiveCollaborationError("Chat message is missing its client ID")
        message_hash = hashlib.sha256(
            f"{self.user_name}\0{message_id}".encode()
        ).hexdigest()[:20]
        chat_path = room_path / "chat"
        with self._sequence_lock(room_path):
            existing = sorted(chat_path.glob(f"*--{message_hash}.json"))
            if existing:
                message = _read_json_file(existing[0])
                message["duplicate"] = True
                return message
            sequence = self._latest_message_sequence(room_path) + 1
            message = {
                "sequence": sequence,
                "client_message_id": message_id,
                "author": self.user_name,
                "text": text,
                "created_at": _utc_iso(),
            }
            if isinstance(anchor, dict) and anchor:
                message["anchor"] = anchor
            _write_json_atomic(
                chat_path / f"{sequence:020d}--{message_hash}.json", message
            )
        self._append_audit(
            room_path,
            "chat.message",
            {"message_sequence": sequence, "has_anchor": bool(message.get("anchor"))},
        )
        return {**message, "duplicate": False}

    def chat_messages(self, room_id, after_sequence, limit=500):
        room_path = self._require_room(room_id)
        after_sequence = int(after_sequence)
        selected = []
        try:
            for path in room_path.joinpath("chat").glob("*.json"):
                sequence = self._message_sequence(path)
                if sequence is not None and sequence > after_sequence:
                    selected.append((sequence, path))
        except OSError as exc:
            raise LiveCollaborationError(f"Could not list shared chat messages: {exc}") from exc
        messages = []
        for sequence, path in sorted(selected)[: max(1, min(int(limit), 5000))]:
            message = _read_json_file(path)
            message["sequence"] = sequence
            messages.append(message)
        return messages

    @staticmethod
    def _segment_lock_path(room_path, segment_id):
        readable = _safe_file_component(segment_id, fallback="segment", max_length=28)
        digest = hashlib.sha256(str(segment_id).encode("utf-8")).hexdigest()[:12]
        return room_path / "locks" / f"{readable}--{digest}.json"

    def _segment_creators(self, room_path):
        creators = {}
        selected = []
        try:
            for path in room_path.joinpath("operations").glob("*.json"):
                sequence = self._operation_sequence(path)
                if sequence is not None:
                    selected.append((sequence, path))
        except OSError as exc:
            raise LiveCollaborationError(f"Could not inspect segment ownership: {exc}") from exc
        for _, path in sorted(selected):
            operation = _read_json_file(path)
            segment_id = str(operation.get("segment_id") or "")
            if segment_id and segment_id not in creators:
                creators[segment_id] = str(operation.get("author") or "")
        return creators

    def segment_locks(self, room_id):
        room_path = self._require_room(room_id)
        creators = self._segment_creators(room_path)
        result = []
        for segment_id, owner in creators.items():
            lock_path = self._segment_lock_path(room_path, segment_id)
            lock_data = _read_json_file(lock_path) if lock_path.is_file() else {}
            expires_epoch = float(lock_data.get("expires_epoch", 0.0) or 0.0)
            locked = bool(lock_data.get("locked", False))
            if locked and expires_epoch and time.time() >= expires_epoch:
                locked = False
            result.append(
                {
                    "segment_id": segment_id,
                    "owner": str(lock_data.get("owner") or owner),
                    "locked": locked,
                    "updated_at": lock_data.get("updated_at"),
                    **(
                        {"expires_at": lock_data.get("expires_at")}
                        if locked and lock_data.get("expires_at")
                        else {}
                    ),
                }
            )
        return sorted(result, key=lambda item: item["segment_id"].casefold())

    def set_segment_lock(self, room_id, segment_id, locked, expires_minutes=0):
        room_path = self._require_room(room_id)
        segment_id = str(segment_id or "").strip()
        if not segment_id:
            raise LiveCollaborationError("Select a label first")
        with self._sequence_lock(room_path):
            owner = self._segment_creators(room_path).get(segment_id)
            existing_lock_path = self._segment_lock_path(room_path, segment_id)
            if existing_lock_path.is_file():
                owner = str(_read_json_file(existing_lock_path).get("owner") or owner)
            if not owner:
                raise LiveCollaborationError(
                    "The label must synchronize once before it can be locked"
                )
            role = self._role_for(room_path, self.user_name)
            if owner != self.user_name and role != "admin":
                raise LiveCollaborationError(
                    f"Only {owner}, who created this label, can change its lock"
                )
            expires_minutes = max(0, int(expires_minutes or 0))
            expires_epoch = (
                time.time() + expires_minutes * 60.0
                if bool(locked) and expires_minutes
                else 0.0
            )
            state = {
                "segment_id": segment_id,
                "owner": owner,
                "locked": bool(locked),
                "updated_by": self.user_name,
                "updated_at": _utc_iso(),
                "expires_epoch": expires_epoch,
                "expires_at": (
                    datetime.fromtimestamp(expires_epoch, timezone.utc).isoformat()
                    if expires_epoch
                    else None
                ),
            }
            _write_json_atomic(self._segment_lock_path(room_path, segment_id), state)
        self._append_audit(
            room_path,
            "label.lock",
            {
                "segment_id": segment_id,
                "locked": bool(locked),
                "expires_at": state.get("expires_at"),
            },
        )
        return state

    def _append_audit(self, room_path, event, details=None):
        stamp = time.time_ns()
        record = {
            "id": str(uuid.uuid4()),
            "event": str(event),
            "user": self.user_name,
            "created_at": _utc_iso(),
            "details": details or {},
        }
        _write_json_atomic(
            room_path / "audit" / f"{stamp:020d}--{record['id'][:12]}.json",
            record,
        )
        return record

    def audit_events(self, room_id, limit=500):
        room_path = self._require_room(room_id)
        records = []
        try:
            paths = sorted(room_path.joinpath("audit").glob("*.json"), reverse=True)
        except OSError as exc:
            raise LiveCollaborationError(f"Could not list audit events: {exc}") from exc
        for path in paths[: max(1, min(int(limit), 5000))]:
            records.append(_read_json_file(path))
        return records

    @staticmethod
    def _role_path(room_path, user_name):
        readable = _safe_file_component(user_name, fallback="user", max_length=28)
        digest = hashlib.sha256(str(user_name).encode("utf-8")).hexdigest()[:12]
        return room_path / "roles" / f"{readable}--{digest}.json"

    def _role_for(self, room_path, user_name):
        metadata = _read_json_file(room_path / "room.json")
        if str(metadata.get("created_by")) == str(user_name):
            return "admin"
        role_path = self._role_path(room_path, user_name)
        if role_path.is_file():
            role = str(_read_json_file(role_path).get("role") or "editor")
            return role if role in ROOM_ROLES else "editor"
        return "editor"

    def room_roles(self, room_id):
        room_path = self._require_room(room_id)
        metadata = _read_json_file(room_path / "room.json")
        roles = {str(metadata.get("created_by")): "admin"}
        for path in room_path.joinpath("roles").glob("*.json"):
            record = _read_json_file(path)
            user = str(record.get("user") or "")
            role = str(record.get("role") or "editor")
            if user and role in ROOM_ROLES and user not in roles:
                roles[user] = role
        return [
            {"user": user, "role": role}
            for user, role in sorted(roles.items(), key=lambda item: item[0].casefold())
        ]

    def set_room_role(self, room_id, target_user, role):
        room_path = self._require_room(room_id)
        target_user = str(target_user or "").strip()
        role = str(role or "").strip()
        if not target_user or role not in ROOM_ROLES:
            raise LiveCollaborationError("Choose a user and a valid room role")
        if self._role_for(room_path, self.user_name) != "admin":
            raise LiveCollaborationError("Only a room administrator can change roles")
        metadata = _read_json_file(room_path / "room.json")
        if target_user == metadata.get("created_by") and role != "admin":
            raise LiveCollaborationError("The room creator must remain an administrator")
        record = {
            "user": target_user,
            "role": role,
            "updated_by": self.user_name,
            "updated_at": _utc_iso(),
        }
        _write_json_atomic(self._role_path(room_path, target_user), record)
        self._append_audit(room_path, "room.role", record)
        return record

    @staticmethod
    def _review_path(room_path, segment_id):
        readable = _safe_file_component(segment_id, fallback="segment", max_length=28)
        digest = hashlib.sha256(str(segment_id).encode("utf-8")).hexdigest()[:12]
        return room_path / "reviews" / f"{readable}--{digest}.json"

    def review_states(self, room_id):
        room_path = self._require_room(room_id)
        result = []
        for path in room_path.joinpath("reviews").glob("*.json"):
            result.append(_read_json_file(path))
        return sorted(result, key=lambda item: str(item.get("segment_id", "")).casefold())

    def set_review_state(self, room_id, segment_id, state, note=""):
        room_path = self._require_room(room_id)
        state = str(state or "").strip()
        if state not in REVIEW_STATES:
            raise LiveCollaborationError("Choose a valid review state")
        role = self._role_for(room_path, self.user_name)
        if role == "viewer":
            raise LiveCollaborationError("Viewers cannot change review state")
        if state in {"approved", "changes_requested"} and role not in {"reviewer", "admin"}:
            raise LiveCollaborationError("Only a reviewer or administrator can finalize review")
        record = {
            "segment_id": str(segment_id),
            "state": state,
            "note": str(note or "")[:2000],
            "updated_by": self.user_name,
            "updated_at": _utc_iso(),
        }
        _write_json_atomic(self._review_path(room_path, segment_id), record)
        if state == "approved":
            lock_path = self._segment_lock_path(room_path, segment_id)
            lock_record = _read_json_file(lock_path) if lock_path.is_file() else {}
            lock_record.update(
                {
                    "segment_id": str(segment_id),
                    "owner": str(
                        lock_record.get("owner")
                        or self._segment_creators(room_path).get(str(segment_id))
                        or self.user_name
                    ),
                    "locked": True,
                    "updated_by": self.user_name,
                    "updated_at": _utc_iso(),
                }
            )
            _write_json_atomic(lock_path, lock_record)
        self._append_audit(room_path, "label.review", record)
        return record

    def request_segment_access(self, room_id, segment_id, message=""):
        room_path = self._require_room(room_id)
        request_id = str(uuid.uuid4())
        record = {
            "id": request_id,
            "segment_id": str(segment_id),
            "requester": self.user_name,
            "message": str(message or "")[:1000],
            "status": "pending",
            "created_at": _utc_iso(),
        }
        _write_json_atomic(
            room_path / "access-requests" / f"{time.time_ns():020d}--{request_id}.json",
            record,
        )
        self._append_audit(room_path, "label.access-request", record)
        return record

    def segment_access_requests(self, room_id, segment_id=None):
        room_path = self._require_room(room_id)
        result = []
        for path in room_path.joinpath("access-requests").glob("*.json"):
            record = _read_json_file(path)
            if segment_id is None or str(record.get("segment_id")) == str(segment_id):
                result.append(record)
        return sorted(result, key=lambda item: str(item.get("created_at", "")))

    def transfer_segment_owner(self, room_id, segment_id, target_user):
        room_path = self._require_room(room_id)
        target_user = str(target_user or "").strip()
        if not target_user:
            raise LiveCollaborationError("Choose a new label owner")
        creators = self._segment_creators(room_path)
        current_owner = creators.get(str(segment_id))
        lock_path = self._segment_lock_path(room_path, segment_id)
        if lock_path.is_file():
            current_owner = str(_read_json_file(lock_path).get("owner") or current_owner)
        if self.user_name != current_owner and self._role_for(room_path, self.user_name) != "admin":
            raise LiveCollaborationError("Only the label owner or an administrator can transfer it")
        state = _read_json_file(lock_path) if lock_path.is_file() else {}
        state.update(
            {
                "segment_id": str(segment_id),
                "owner": target_user,
                "locked": bool(state.get("locked", False)),
                "updated_by": self.user_name,
                "updated_at": _utc_iso(),
            }
        )
        _write_json_atomic(lock_path, state)
        for request_path in room_path.joinpath("access-requests").glob("*.json"):
            request = _read_json_file(request_path)
            if (
                str(request.get("segment_id")) == str(segment_id)
                and request.get("requester") == target_user
                and request.get("status") == "pending"
            ):
                request.update(
                    {
                        "status": "accepted",
                        "resolved_by": self.user_name,
                        "resolved_at": _utc_iso(),
                    }
                )
                _write_json_atomic(request_path, request)
        self._append_audit(
            room_path,
            "label.owner-transfer",
            {"segment_id": str(segment_id), "from": current_owner, "to": target_user},
        )
        return state

    def _all_operation_records(self, room_path):
        records = {}
        try:
            active_paths = list(room_path.joinpath("operations").glob("*.json"))
            archive_paths = list(room_path.joinpath("operation-archives").glob("*.zip"))
        except OSError as exc:
            raise LiveCollaborationError(f"Could not inspect room history: {exc}") from exc
        for path in active_paths:
            sequence = self._operation_sequence(path)
            if sequence is not None:
                records[sequence] = _read_json_file(path)
        for archive in archive_paths:
            try:
                with zipfile.ZipFile(archive, "r") as bundle:
                    for name in bundle.namelist():
                        sequence = self._operation_sequence(Path(name))
                        if sequence is None or sequence in records:
                            continue
                        records[sequence] = json.loads(bundle.read(name).decode("utf-8"))
            except (OSError, ValueError, zipfile.BadZipFile) as exc:
                raise LiveCollaborationError(
                    f"Could not read history archive {archive.name}: {exc}"
                ) from exc
        for sequence, operation in records.items():
            operation["sequence"] = int(sequence)
        return [records[key] for key in sorted(records)]

    def room_history(self, room_id, limit=500):
        room_path = self._require_room(room_id)
        records = self._all_operation_records(room_path)
        selected = records[-max(1, min(int(limit), 5000)) :]
        return [operation_summary(item, decode_mask_delta) for item in selected]

    def publish_room_snapshot(self, room_id, segment_operations, compact=True, label=""):
        """Append a compatible full-state batch and archive older loose files."""
        room_path = self._require_room(room_id)
        if not segment_operations:
            return None
        group_id = str(uuid.uuid4())
        created = []
        with self._sequence_lock(room_path):
            original_creators = self._segment_creators(room_path)
            for segment_id, owner in original_creators.items():
                lock_path = self._segment_lock_path(room_path, segment_id)
                if not lock_path.is_file():
                    _write_json_atomic(
                        lock_path,
                        {
                            "segment_id": segment_id,
                            "owner": owner,
                            "locked": False,
                            "updated_by": self.user_name,
                            "updated_at": _utc_iso(),
                        },
                    )
            before_sequence = self._latest_sequence(room_path)
            sequence = before_sequence
            count = len(segment_operations)
            for index, operation in enumerate(segment_operations):
                sequence += 1
                operation_id = f"snapshot-{group_id}-{index}"
                operation_hash = hashlib.sha256(operation_id.encode()).hexdigest()[:20]
                stored = {
                    **operation,
                    "client_operation_id": operation_id,
                    "operation_kind": "snapshot",
                    "sequence": sequence,
                    "author": self.user_name,
                    "created_at": _utc_iso(),
                    "snapshot_group_id": group_id,
                    "snapshot_group_index": index,
                    "snapshot_group_count": count,
                    "system_snapshot": True,
                    "snapshot_label": str(label or "")[:200],
                }
                try:
                    stored["changed_voxels"] = int(
                        np.count_nonzero(decode_mask_delta(stored)[0])
                    )
                except Exception:
                    pass
                destination = (
                    room_path
                    / "operations"
                    / f"{sequence:020d}--{operation_hash}.json"
                )
                _write_json_atomic(destination, stored)
                created.append(stored)
            manifest = {
                "id": group_id,
                "created_at": _utc_iso(),
                "created_by": self.user_name,
                "before_sequence": before_sequence,
                "first_sequence": created[0]["sequence"],
                "last_sequence": created[-1]["sequence"],
                "segment_count": len(created),
                "compacted": bool(compact),
                "label": str(label or "")[:200],
            }
            _write_json_atomic(
                room_path / "snapshots" / f"{sequence:020d}--{group_id}.json",
                manifest,
            )
            if compact and before_sequence > 0:
                candidates = []
                for path in room_path.joinpath("operations").glob("*.json"):
                    item_sequence = self._operation_sequence(path)
                    if item_sequence is not None and item_sequence <= before_sequence:
                        candidates.append((item_sequence, path))
                if candidates:
                    archive_name = (
                        f"{candidates[0][0]:020d}-{candidates[-1][0]:020d}--"
                        f"{group_id[:12]}.zip"
                    )
                    archive_path = room_path / "operation-archives" / archive_name
                    temporary = _atomic_temporary_path(archive_path)
                    try:
                        with zipfile.ZipFile(
                            temporary, "x", compression=zipfile.ZIP_DEFLATED
                        ) as bundle:
                            for _, path in candidates:
                                bundle.write(path, arcname=path.name)
                        os.replace(temporary, archive_path)
                        for _, path in candidates:
                            path.unlink(missing_ok=True)
                    finally:
                        temporary.unlink(missing_ok=True)
        self._append_audit(
            room_path,
            "history.snapshot",
            {
                "snapshot_id": group_id,
                "segments": len(created),
                "through_sequence": before_sequence,
                "compacted": bool(compact),
            },
        )
        return manifest

    def snapshot_manifests(self, room_id):
        room_path = self._require_room(room_id)
        result = []
        for path in room_path.joinpath("snapshots").glob("*.json"):
            result.append(_read_json_file(path))
        return sorted(result, key=lambda item: int(item.get("last_sequence", 0)))

    def state_at_sequence(self, room_id, sequence):
        room_path = self._require_room(room_id)
        operations = self._all_operation_records(room_path)
        return reconstruct_snapshot_operations(
            operations,
            int(sequence),
            apply_mask_delta,
            encode_mask_delta,
        )

    def room_conflicts(self, room_id, unresolved_only=False):
        room_path = self._require_room(room_id)
        result = []
        for path in room_path.joinpath("conflicts").glob("*.json"):
            record = _read_json_file(path)
            record["_path"] = str(path)
            if not unresolved_only or record.get("resolution") == "unresolved":
                result.append(record)
        return sorted(result, key=lambda item: int(item.get("sequence", 0)), reverse=True)

    def resolve_conflict(self, room_id, conflict_id, resolution):
        room_path = self._require_room(room_id)
        if resolution not in {"latest", "mine", "other", "union"}:
            raise LiveCollaborationError("Choose a valid conflict resolution")
        for path in room_path.joinpath("conflicts").glob(f"*--{conflict_id}.json"):
            record = _read_json_file(path)
            record.update(
                {
                    "resolution": resolution,
                    "resolved_by": self.user_name,
                    "resolved_at": _utc_iso(),
                }
            )
            _write_json_atomic(path, record)
            self._append_audit(room_path, "conflict.resolve", record)
            return record
        raise LiveCollaborationError("Conflict record no longer exists")

    def get_material_template(self, room_id):
        room_path = self._require_room(room_id)
        path = room_path / "material-template.json"
        return _read_json_file(path) if path.is_file() else None

    def set_material_template(self, room_id, payload):
        room_path = self._require_room(room_id)
        if self._role_for(room_path, self.user_name) != "admin":
            raise LiveCollaborationError("Only a room administrator can replace the template")
        normalized = validate_material_template(payload)
        normalized.update({"updated_by": self.user_name, "updated_at": _utc_iso()})
        _write_json_atomic(room_path / "material-template.json", normalized)
        self._append_audit(
            room_path,
            "room.material-template",
            {"segments": len(normalized["segments"])},
        )
        return normalized

    def list_project_backups(self, room_id):
        room_path = self._require_room(room_id)
        result = []
        for path in room_path.joinpath("backups").glob("*.mrb"):
            meta_path = path.with_suffix(path.suffix + ".json")
            meta = _read_json_file(meta_path) if meta_path.is_file() else {}
            result.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "size_bytes": path.stat().st_size,
                    "modified_epoch": path.stat().st_mtime,
                    "pinned": bool(meta.get("pinned", False)),
                    "sha256": meta.get("sha256"),
                    "created_by": meta.get("created_by"),
                }
            )
        return sorted(result, key=lambda item: item["modified_epoch"], reverse=True)

    def set_backup_pinned(self, room_id, backup_name, pinned):
        room_path = self._require_room(room_id)
        path = room_path / "backups" / Path(str(backup_name)).name
        if not path.is_file() or path.suffix.lower() != ".mrb":
            raise LiveCollaborationError("Selected backup no longer exists")
        meta_path = path.with_suffix(path.suffix + ".json")
        meta = _read_json_file(meta_path) if meta_path.is_file() else {}
        meta.update(
            {
                "name": path.name,
                "pinned": bool(pinned),
                "updated_by": self.user_name,
                "updated_at": _utc_iso(),
            }
        )
        _write_json_atomic(meta_path, meta)
        self._append_audit(
            room_path,
            "backup.pin",
            {"name": path.name, "pinned": bool(pinned)},
        )
        return meta

    def verify_project_backup(self, room_id, backup_name):
        room_path = self._require_room(room_id)
        path = room_path / "backups" / Path(str(backup_name)).name
        if not path.is_file():
            raise LiveCollaborationError("Selected backup no longer exists")
        actual = sha256_file(path)
        meta_path = path.with_suffix(path.suffix + ".json")
        meta = _read_json_file(meta_path) if meta_path.is_file() else {}
        expected = meta.get("sha256")
        return {
            "name": path.name,
            "sha256": actual,
            "expected_sha256": expected,
            "valid": expected in {None, actual},
        }

    def prune_project_backups(self, room_id, keep_unpinned=50):
        room_path = self._require_room(room_id)
        backups = [item for item in self.list_project_backups(room_id) if not item["pinned"]]
        removed = []
        for item in backups[max(1, int(keep_unpinned)) :]:
            path = Path(item["path"])
            path.unlink(missing_ok=True)
            path.with_suffix(path.suffix + ".json").unlink(missing_ok=True)
            removed.append(path.name)
        if removed:
            self._append_audit(room_path, "backup.retention", {"removed": removed})
        return removed

    def diagnostics(self, room_id):
        room_path = self._require_room(room_id)
        started = time.monotonic()
        self.health_check(room_id)
        probe = room_path / "presence" / f".diagnostic-{uuid.uuid4().hex}.tmp"
        try:
            probe.write_bytes(b"live-segmentation-diagnostic")
            if probe.read_bytes() != b"live-segmentation-diagnostic":
                raise LiveCollaborationError("Shared-folder read/write probe was inconsistent")
        finally:
            probe.unlink(missing_ok=True)
        usage = shutil.disk_usage(room_path)
        return {
            "status": "ok",
            "latency_seconds": round(time.monotonic() - started, 4),
            "free_bytes": int(usage.free),
            "active_operation_files": len(list(room_path.joinpath("operations").glob("*.json"))),
            "history_archives": len(list(room_path.joinpath("operation-archives").glob("*.zip"))),
            "snapshots": len(list(room_path.joinpath("snapshots").glob("*.json"))),
            "backups": len(list(room_path.joinpath("backups").glob("*.mrb"))),
            "room_path": str(room_path),
        }

    def reserve_project_backup(self, room_id, interval_seconds):
        room_path = self._require_room(room_id)
        now = time.time()
        state_path = room_path / "backup-state.json"
        with self._sequence_lock(room_path):
            state = _read_json_file(state_path) if state_path.is_file() else {}
            last_backup = float(state.get("last_backup_epoch", 0.0) or 0.0)
            if now - last_backup < max(60.0, float(interval_seconds)):
                return None
            token = uuid.uuid4().hex
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            file_name = (
                f"{stamp}--{_safe_file_component(self.user_name, 'user', 28)}"
                f"--{token[:8]}.mrb"
            )
            state = {
                "last_backup_epoch": now,
                "reserved_at": _utc_iso(),
                "reserved_by": self.user_name,
                "token": token,
                "file": file_name,
            }
            _write_json_atomic(state_path, state)
        return {
            "token": token,
            "path": str(room_path / "backups" / file_name),
            "state_path": str(state_path),
        }


class LiveCollaborationController:
    """Synchronize a standard Slicer segmentation node with a live room."""

    SETTINGS_PREFIX = "LiveSegmentation/collaboration/"

    def __init__(self, module_widget):
        self.owner = module_widget
        self.connected = False
        self.client = None
        self.room_id = None
        self.room_name = None
        self.user_name = None
        self.last_sequence = 0
        self.initial_sequence = 0
        self.initial_sync_complete = False
        self.segmentation_node_id = None
        self.volume_shape = None
        self.baselines = {}
        self.dirty_segments = set()
        self.force_snapshots = set()
        self.outgoing = []
        self.outgoing_keys = set()
        self._known_segment_ids = set()
        self._observed_node = None
        self._observed_segmentation = None
        self._observer_tags = []
        self._applying_remote = False
        self._worker = None
        self._worker_results = queue.Queue()
        self._last_presence_send = 0.0
        self._last_metadata_fetch = 0.0
        self._last_health_check = 0.0
        self._last_backup_check = 0.0
        self._last_error = None
        self._session_token = 0
        self.connection_healthy = False
        self._connection_error_popup_shown = False
        self._force_refresh = False
        self._last_sync_duration = None
        self.last_chat_sequence = 0
        self.pending_chat = []
        self.displayed_chat_sequences = set()
        self.segment_owners = {}
        self.segment_locks_state = {}
        self.pending_lock_changes = {}
        self.presence_by_user = {}
        self.room_roles_state = {}
        self.review_states_state = {}
        self.access_requests_state = []
        self.chat_anchors = {}
        self.history_records = []
        self.conflicts_state = []
        self.material_template_state = None
        self.backup_records = []
        self.last_diagnostics = None
        self.pending_actions = []
        self._last_advanced_fetch = 0.0
        self._last_snapshot_sequence = 0
        self._snapshot_requested = False
        self._snapshot_label = ""
        self._restoring_sequence = None

    def setup(self):
        import ctk
        import qt

        settings = qt.QSettings()
        default_user = str(settings.value(self.SETTINGS_PREFIX + "user", getpass.getuser()))
        default_room = str(settings.value(self.SETTINGS_PREFIX + "room", ""))
        default_transport = str(
            settings.value(self.SETTINGS_PREFIX + "transport", "shared-folder")
        )
        default_shared_folder = str(
            settings.value(self.SETTINGS_PREFIX + "sharedFolder", "")
        )
        default_server = str(
            settings.value(self.SETTINGS_PREFIX + "server", "http://127.0.0.1:8000")
        )

        self.group = qt.QGroupBox("Live collaboration")
        layout = qt.QVBoxLayout(self.group)
        explanation = qt.QLabel(
            "Join the same room to see each other's segmentation changes automatically. "
            "Use a shared/network folder without a server, or connect to a collaboration server."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        identity_layout = qt.QFormLayout()
        self.user_edit = qt.QLineEdit(default_user)
        self.user_edit.setPlaceholderText("Your name")
        self.room_edit = qt.QLineEdit(default_room)
        self.room_edit.setPlaceholderText("e.g. specimen-01")
        self.transport_combo = qt.QComboBox()
        self.transport_combo.addItem("Shared/network folder")
        self.transport_combo.addItem("Collaboration server")
        self.transport_combo.setCurrentIndex(1 if default_transport == "server" else 0)
        identity_layout.addRow("Name", self.user_edit)
        identity_layout.addRow("Room", self.room_edit)
        identity_layout.addRow("Connection", self.transport_combo)

        self.shared_folder_label = qt.QLabel("Shared folder")
        self.shared_folder_widget = qt.QWidget()
        shared_folder_layout = qt.QHBoxLayout(self.shared_folder_widget)
        shared_folder_layout.setContentsMargins(0, 0, 0, 0)
        self.shared_folder_edit = qt.QLineEdit(default_shared_folder)
        self.shared_folder_edit.setPlaceholderText(r"e.g. P:\LiveSegmentation or \\server\share")
        self.shared_folder_button = qt.QPushButton("Browse…")
        self.shared_folder_button.clicked.connect(self.choose_shared_folder)
        shared_folder_layout.addWidget(self.shared_folder_edit, 1)
        shared_folder_layout.addWidget(self.shared_folder_button)
        identity_layout.addRow(self.shared_folder_label, self.shared_folder_widget)
        layout.addLayout(identity_layout)

        self.join_button = qt.QPushButton("Join live room")
        self.join_button.setMinimumHeight(34)
        self.join_button.clicked.connect(self.toggle_connection)
        connection_actions = qt.QHBoxLayout()
        connection_actions.addWidget(self.join_button, 1)
        self.refresh_button = qt.QPushButton("Refresh now")
        self.refresh_button.setToolTip(
            "Immediately check the connection, participants, chat, locks, and new edits"
        )
        self.refresh_button.enabled = False
        self.refresh_button.clicked.connect(self.refresh_now)
        connection_actions.addWidget(self.refresh_button)
        layout.addLayout(connection_actions)

        self.status_label = qt.QLabel("● Offline")
        self.status_label.setStyleSheet("color: #777;")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.users_label = qt.QLabel("Nobody else is connected")
        self.users_label.setWordWrap(True)
        layout.addWidget(self.users_label)
        presence_actions = qt.QHBoxLayout()
        self.collaborator_combo = qt.QComboBox()
        self.collaborator_combo.setToolTip("Select an online collaborator")
        self.follow_checkbox = qt.QCheckBox("Follow view")
        self.follow_checkbox.setToolTip(
            "Keep slice offsets and crosshair aligned with the selected collaborator"
        )
        self.jump_to_user_button = qt.QPushButton("Jump to user")
        self.jump_to_user_button.enabled = False
        self.jump_to_user_button.clicked.connect(self.jump_to_selected_user)
        presence_actions.addWidget(self.collaborator_combo, 1)
        presence_actions.addWidget(self.follow_checkbox)
        presence_actions.addWidget(self.jump_to_user_button)
        layout.addLayout(presence_actions)

        self.lock_status_label = qt.QLabel("Select a label after joining to manage its lock")
        self.lock_status_label.setWordWrap(True)
        layout.addWidget(self.lock_status_label)
        self.lock_button = qt.QPushButton("Lock selected label")
        self.lock_button.enabled = False
        self.lock_button.clicked.connect(self.toggle_selected_segment_lock)
        layout.addWidget(self.lock_button)
        lock_actions = qt.QHBoxLayout()
        self.lock_expiry_spin = qt.QSpinBox()
        self.lock_expiry_spin.minimum = 0
        self.lock_expiry_spin.maximum = 1440
        self.lock_expiry_spin.specialValueText = "No expiry"
        self.lock_expiry_spin.suffix = " min"
        self.request_access_button = qt.QPushButton("Request access")
        self.request_access_button.enabled = False
        self.request_access_button.clicked.connect(self.request_selected_segment_access)
        self.transfer_owner_button = qt.QPushButton("Transfer to selected user")
        self.transfer_owner_button.enabled = False
        self.transfer_owner_button.clicked.connect(self.transfer_selected_segment_owner)
        lock_actions.addWidget(qt.QLabel("Lock duration"))
        lock_actions.addWidget(self.lock_expiry_spin)
        lock_actions.addWidget(self.request_access_button)
        lock_actions.addWidget(self.transfer_owner_button)
        layout.addLayout(lock_actions)

        review_actions = qt.QHBoxLayout()
        self.review_state_combo = qt.QComboBox()
        for value, label in (
            ("draft", "Draft"),
            ("in_progress", "In progress"),
            ("ready_for_review", "Ready for review"),
            ("changes_requested", "Changes requested"),
            ("approved", "Approved"),
        ):
            self.review_state_combo.addItem(label, value)
        self.review_note_edit = qt.QLineEdit()
        self.review_note_edit.setPlaceholderText("Optional review note")
        self.set_review_button = qt.QPushButton("Set label review state")
        self.set_review_button.enabled = False
        self.set_review_button.clicked.connect(self.set_selected_segment_review_state)
        review_actions.addWidget(self.review_state_combo)
        review_actions.addWidget(self.review_note_edit, 1)
        review_actions.addWidget(self.set_review_button)
        layout.addLayout(review_actions)

        role_actions = qt.QHBoxLayout()
        self.role_combo = qt.QComboBox()
        for role in ROOM_ROLES:
            self.role_combo.addItem(role.title(), role)
        self.set_role_button = qt.QPushButton("Set selected user's role")
        self.set_role_button.enabled = False
        self.set_role_button.clicked.connect(self.set_selected_user_role)
        role_actions.addWidget(qt.QLabel("Room role"))
        role_actions.addWidget(self.role_combo)
        role_actions.addWidget(self.set_role_button)
        layout.addLayout(role_actions)

        chat_group = ctk.ctkCollapsibleButton()
        chat_group.text = "Room chat"
        chat_group.collapsed = False
        chat_layout = qt.QVBoxLayout(chat_group)
        self.chat_history = qt.QPlainTextEdit()
        self.chat_history.setReadOnly(True)
        self.chat_history.setPlaceholderText("Permanent room messages appear here")
        self.chat_history.setMaximumHeight(150)
        chat_layout.addWidget(self.chat_history)
        chat_entry_layout = qt.QHBoxLayout()
        self.chat_input = qt.QLineEdit()
        self.chat_input.setPlaceholderText("Write a message…")
        self.chat_input.enabled = False
        self.chat_input.returnPressed.connect(self.send_chat_message)
        self.chat_send_button = qt.QPushButton("Send")
        self.chat_send_button.enabled = False
        self.chat_send_button.clicked.connect(self.send_chat_message)
        chat_entry_layout.addWidget(self.chat_input, 1)
        chat_entry_layout.addWidget(self.chat_send_button)
        chat_layout.addLayout(chat_entry_layout)
        chat_location_layout = qt.QHBoxLayout()
        self.chat_anchor_checkbox = qt.QCheckBox("Attach current Slicer location")
        self.chat_anchor_checkbox.checked = True
        self.chat_location_combo = qt.QComboBox()
        self.chat_location_combo.setToolTip("Messages that contain a spatial location")
        self.jump_to_chat_button = qt.QPushButton("Jump to message location")
        self.jump_to_chat_button.enabled = False
        self.jump_to_chat_button.clicked.connect(self.jump_to_selected_chat_anchor)
        chat_location_layout.addWidget(self.chat_anchor_checkbox)
        chat_location_layout.addWidget(self.chat_location_combo, 1)
        chat_location_layout.addWidget(self.jump_to_chat_button)
        chat_layout.addLayout(chat_location_layout)
        layout.addWidget(chat_group)

        backup_group = ctk.ctkCollapsibleButton()
        backup_group.text = "Automatic project backups"
        backup_group.collapsed = True
        backup_form = qt.QFormLayout(backup_group)
        default_backups = str(
            settings.value(self.SETTINGS_PREFIX + "automaticBackups", "true")
        ).lower() not in {"0", "false", "no"}
        default_backup_minutes = int(
            settings.value(self.SETTINGS_PREFIX + "backupIntervalMinutes", 5)
        )
        self.backup_enabled_checkbox = qt.QCheckBox("Save complete Slicer project bundles")
        self.backup_enabled_checkbox.checked = default_backups
        self.backup_interval_spin = qt.QSpinBox()
        self.backup_interval_spin.minimum = 1
        self.backup_interval_spin.maximum = 1440
        self.backup_interval_spin.value = max(1, min(default_backup_minutes, 1440))
        self.backup_interval_spin.suffix = " min"
        self.backup_status_label = qt.QLabel(
            "Backups are retained in the joined shared room's backups folder."
        )
        self.backup_status_label.setWordWrap(True)
        backup_form.addRow(self.backup_enabled_checkbox)
        backup_form.addRow("Interval", self.backup_interval_spin)
        backup_form.addRow(self.backup_status_label)
        self.backup_retention_spin = qt.QSpinBox()
        self.backup_retention_spin.minimum = 1
        self.backup_retention_spin.maximum = 1000
        self.backup_retention_spin.value = int(
            settings.value(self.SETTINGS_PREFIX + "backupRetention", 50)
        )
        backup_form.addRow("Keep unpinned", self.backup_retention_spin)
        self.backup_tree = qt.QTreeWidget()
        self.backup_tree.setHeaderLabels(["Backup", "Size", "Pinned", "Checksum"])
        self.backup_tree.setMaximumHeight(135)
        backup_form.addRow(self.backup_tree)
        backup_buttons = qt.QHBoxLayout()
        self.refresh_backups_button = qt.QPushButton("Refresh backups")
        self.refresh_backups_button.clicked.connect(self.refresh_backup_list)
        self.pin_backup_button = qt.QPushButton("Pin / unpin")
        self.pin_backup_button.clicked.connect(self.toggle_selected_backup_pin)
        self.verify_backup_button = qt.QPushButton("Verify")
        self.verify_backup_button.clicked.connect(self.verify_selected_backup)
        self.restore_backup_button = qt.QPushButton("Restore")
        self.restore_backup_button.clicked.connect(self.restore_selected_backup)
        for button in (
            self.refresh_backups_button,
            self.pin_backup_button,
            self.verify_backup_button,
            self.restore_backup_button,
        ):
            button.enabled = False
            backup_buttons.addWidget(button)
        backup_form.addRow(backup_buttons)
        layout.addWidget(backup_group)
        self.backup_group = backup_group

        history_group = ctk.ctkCollapsibleButton()
        history_group.text = "Version history and conflicts"
        history_group.collapsed = True
        history_layout = qt.QVBoxLayout(history_group)
        self.history_filter_edit = qt.QLineEdit()
        self.history_filter_edit.setPlaceholderText("Filter timeline by user, label, type, or sequence")
        self.history_filter_edit.textChanged.connect(self.filter_history_tree)
        history_layout.addWidget(self.history_filter_edit)
        self.history_tree = qt.QTreeWidget()
        self.history_tree.setHeaderLabels(
            ["Seq", "User", "Label", "Changed voxels", "Type", "Time"]
        )
        self.history_tree.setMaximumHeight(180)
        history_layout.addWidget(self.history_tree)
        history_buttons = qt.QHBoxLayout()
        self.refresh_history_button = qt.QPushButton("Refresh timeline")
        self.refresh_history_button.clicked.connect(self.refresh_advanced_state)
        self.restore_revision_button = qt.QPushButton("Restore selected revision")
        self.restore_revision_button.clicked.connect(self.restore_selected_revision)
        self.create_snapshot_button = qt.QPushButton("Snapshot + compact now")
        self.create_snapshot_button.clicked.connect(self.request_room_snapshot)
        self.snapshot_label_edit = qt.QLineEdit()
        self.snapshot_label_edit.setPlaceholderText("Optional milestone name")
        history_buttons.addWidget(self.snapshot_label_edit, 1)
        for button in (
            self.refresh_history_button,
            self.restore_revision_button,
            self.create_snapshot_button,
        ):
            button.enabled = False
            history_buttons.addWidget(button)
        history_layout.addLayout(history_buttons)
        self.conflict_tree = qt.QTreeWidget()
        self.conflict_tree.setHeaderLabels(
            ["Seq", "Label", "Other user", "Overlap", "Resolution"]
        )
        self.conflict_tree.setMaximumHeight(120)
        history_layout.addWidget(self.conflict_tree)
        conflict_buttons = qt.QHBoxLayout()
        self.resolve_latest_button = qt.QPushButton("Accept latest")
        self.resolve_mine_button = qt.QPushButton("Reapply mine")
        self.resolve_other_button = qt.QPushButton("Use other version")
        self.resolve_union_button = qt.QPushButton("Merge union")
        self.resolve_latest_button.clicked.connect(lambda: self.resolve_selected_conflict("latest"))
        self.resolve_mine_button.clicked.connect(lambda: self.resolve_selected_conflict("mine"))
        self.resolve_other_button.clicked.connect(lambda: self.resolve_selected_conflict("other"))
        self.resolve_union_button.clicked.connect(lambda: self.resolve_selected_conflict("union"))
        for button in (
            self.resolve_latest_button,
            self.resolve_mine_button,
            self.resolve_other_button,
            self.resolve_union_button,
        ):
            button.enabled = False
            conflict_buttons.addWidget(button)
        history_layout.addLayout(conflict_buttons)
        layout.addWidget(history_group)

        project_group = ctk.ctkCollapsibleButton()
        project_group.text = "Diagnostics, templates, and invitations"
        project_group.collapsed = True
        project_layout = qt.QVBoxLayout(project_group)
        privacy = qt.QLabel(
            "Privacy: room names, chat, diagnostics, invitations, and complete MRB backups "
            "must not contain patient-identifying information unless your approved storage and "
            "governance explicitly permit it. Invitation files never contain API keys."
        )
        privacy.setWordWrap(True)
        privacy.setStyleSheet("color: #8a4b00;")
        project_layout.addWidget(privacy)
        self.diagnostics_text = qt.QPlainTextEdit()
        self.diagnostics_text.setReadOnly(True)
        self.diagnostics_text.setMaximumHeight(130)
        project_layout.addWidget(self.diagnostics_text)
        diagnostic_buttons = qt.QHBoxLayout()
        self.run_diagnostics_button = qt.QPushButton("Run diagnostics")
        self.run_diagnostics_button.clicked.connect(self.run_room_diagnostics)
        self.export_diagnostics_button = qt.QPushButton("Export sanitized report")
        self.export_diagnostics_button.clicked.connect(self.export_diagnostics)
        diagnostic_buttons.addWidget(self.run_diagnostics_button)
        diagnostic_buttons.addWidget(self.export_diagnostics_button)
        project_layout.addLayout(diagnostic_buttons)
        template_buttons = qt.QHBoxLayout()
        self.publish_template_button = qt.QPushButton("Publish labels as room template")
        self.publish_template_button.clicked.connect(self.publish_material_template)
        self.apply_template_button = qt.QPushButton("Apply room template")
        self.apply_template_button.clicked.connect(self.apply_material_template)
        template_buttons.addWidget(self.publish_template_button)
        template_buttons.addWidget(self.apply_template_button)
        project_layout.addLayout(template_buttons)
        invite_buttons = qt.QHBoxLayout()
        self.export_invite_button = qt.QPushButton("Export .liveseg invitation")
        self.export_invite_button.clicked.connect(self.export_invitation)
        self.import_invite_button = qt.QPushButton("Import invitation")
        self.import_invite_button.clicked.connect(self.import_invitation)
        invite_buttons.addWidget(self.export_invite_button)
        invite_buttons.addWidget(self.import_invite_button)
        project_layout.addLayout(invite_buttons)
        layout.addWidget(project_group)

        self.server_settings = ctk.ctkCollapsibleButton()
        self.server_settings.text = "Server settings"
        self.server_settings.collapsed = True
        advanced_layout = qt.QFormLayout(self.server_settings)
        self.server_edit = qt.QLineEdit(default_server)
        self.api_key_edit = qt.QLineEdit()
        self.api_key_edit.echoMode = qt.QLineEdit.Password
        advanced_layout.addRow("Collaboration server", self.server_edit)
        advanced_layout.addRow("API key (optional)", self.api_key_edit)
        layout.addWidget(self.server_settings)

        self.transport_combo.currentIndexChanged.connect(self._update_transport_fields)
        self._update_transport_fields()

        self.owner.layout.addWidget(self.group)

        self.timer = qt.QTimer()
        self.timer.setInterval(300)
        self.timer.timeout.connect(self.on_timer)

    @staticmethod
    def _text(widget):
        value = widget.text
        return str(value() if callable(value) else value).strip()

    def _transport_mode(self):
        index = self.transport_combo.currentIndex
        index = index() if callable(index) else index
        return "shared-folder" if int(index) == 0 else "server"

    def _live_status_text(self):
        transport = "shared folder" if isinstance(self.client, SharedFolderRoomClient) else "server"
        node = self._segmentation_node()
        node_name = node.GetName() if node is not None else "shared segmentation"
        return f"● Live via {transport} in room “{self.room_name}” — editing “{node_name}”"

    def _update_transport_fields(self, index=None):
        del index
        shared = self._transport_mode() == "shared-folder"
        self.shared_folder_label.setVisible(shared)
        self.shared_folder_widget.setVisible(shared)
        self.server_settings.setVisible(not shared)
        self.backup_group.setVisible(shared)

    def choose_shared_folder(self, checked=False):
        del checked
        import qt
        import slicer

        current = self._text(self.shared_folder_edit)
        selected = qt.QFileDialog.getExistingDirectory(
            slicer.util.mainWindow(),
            "Select shared/network folder",
            current,
        )
        if selected:
            self.shared_folder_edit.setText(str(selected))

    def toggle_connection(self, checked=False):
        del checked
        if self.connected:
            self.leave()
        else:
            self.join()

    def _show_error(self, message, popup=False):
        import slicer

        self._last_error = str(message)
        self.connection_healthy = False
        self.status_label.setText(f"● Connection problem: {message}")
        self.status_label.setStyleSheet("color: #b3261e;")
        slicer.util.showStatusMessage(f"Live collaboration: {message}", 5000)
        if popup and os.environ.get("LIVE_SEGMENTATION_NO_ERROR_POPUPS") != "1":
            if self.connected:
                detail = (
                    "Live Segmentation lost access to the collaboration location.\n\n"
                    f"{message}\n\n"
                    "Your room is still selected, but the status is offline until a "
                    "successful refresh. Check the server or shared folder connection."
                )
            else:
                detail = (
                    "Live Segmentation could not connect.\n\n"
                    f"{message}\n\n"
                    "Check the server or shared folder connection and try again."
                )
            slicer.util.errorDisplay(detail)

    def refresh_now(self, checked=False):
        del checked
        if not self.connected:
            self._show_error("Join a live room before refreshing", popup=True)
            return
        self._force_refresh = True
        self.status_label.setText("● Refreshing room…")
        self.status_label.setStyleSheet("color: #b26a00;")
        self.on_timer()

    def send_chat_message(self):
        if not self.connected:
            self._show_error("Join a live room before sending a message", popup=True)
            return
        text = self._text(self.chat_input)
        if not text:
            return
        anchor_enabled = self.chat_anchor_checkbox.checked
        anchor_enabled = anchor_enabled() if callable(anchor_enabled) else anchor_enabled
        message = {"client_message_id": str(uuid.uuid4()), "text": text}
        if anchor_enabled:
            message["anchor"] = self._current_location()
        self.pending_chat.append(message)
        self.chat_input.clear()
        self._force_refresh = True
        self.on_timer()

    def toggle_selected_segment_lock(self, checked=False):
        del checked
        if not self.connected:
            self._show_error("Join a live room before changing a label lock", popup=True)
            return
        _, segment_id = self.owner.get_selected_segmentation_node_and_segment_id()
        if not segment_id:
            self._show_error("Select a label first", popup=True)
            return
        owner = self.segment_owners.get(segment_id)
        if owner and owner != self.user_name and self._current_role() != "admin":
            self._show_error(
                f"Only {owner}, who created this label, or a room admin can change its lock",
                popup=True,
            )
            return
        self.segment_owners.setdefault(segment_id, self.user_name)
        current = bool(self.segment_locks_state.get(segment_id, {}).get("locked", False))
        expiry = self.lock_expiry_spin.value
        expiry = expiry() if callable(expiry) else expiry
        self.pending_lock_changes[segment_id] = {
            "locked": not current,
            "expires_minutes": int(expiry),
        }
        self._update_lock_controls()
        self._force_refresh = True
        self.on_timer()

    def _queue_action(self, action, **payload):
        record = {"id": str(uuid.uuid4()), "action": str(action), **payload}
        self.pending_actions.append(record)
        self._force_refresh = True
        return record

    def _combo_current_text(self, combo):
        value = combo.currentText
        return str(value() if callable(value) else value).strip()

    def _combo_current_data(self, combo):
        value = combo.currentData
        return value() if callable(value) else value

    def _current_location(self):
        details = {"layout": None, "slice_offsets": {}}
        try:
            import slicer

            details["layout"] = int(slicer.app.layoutManager().layout)
            crosshair = slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLCrosshairNode")
            if crosshair is not None:
                ras = [0.0, 0.0, 0.0]
                crosshair.GetCursorPositionRAS(ras)
                details["crosshair_ras"] = [round(float(value), 4) for value in ras]
            manager = slicer.app.layoutManager()
            for name in ("Red", "Yellow", "Green"):
                widget = manager.sliceWidget(name) if manager is not None else None
                if widget is not None:
                    details["slice_offsets"][name] = round(
                        float(widget.sliceLogic().GetSliceOffset()), 4
                    )
            editor = self.owner._standard_segment_editor_widget()
            effect = editor.activeEffect() if editor is not None else None
            if effect is not None:
                name = effect.name
                details["active_effect"] = str(name() if callable(name) else name)
            node, segment_id = self.owner.get_selected_segmentation_node_and_segment_id()
            segment = node.GetSegmentation().GetSegment(segment_id) if node and segment_id else None
            if segment is not None:
                details["segment_id"] = segment_id
                details["segment_name"] = segment.GetName() or segment_id
        except Exception:
            pass
        return details

    def _apply_location(self, details):
        if not isinstance(details, dict):
            return
        try:
            import slicer

            manager = slicer.app.layoutManager()
            for name, offset in (details.get("slice_offsets") or {}).items():
                widget = manager.sliceWidget(str(name)) if manager is not None else None
                if widget is not None:
                    widget.sliceLogic().SetSliceOffset(float(offset))
            ras = details.get("crosshair_ras")
            crosshair = slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLCrosshairNode")
            if crosshair is not None and isinstance(ras, (list, tuple)) and len(ras) == 3:
                crosshair.SetCursorPositionRAS([float(value) for value in ras])
                crosshair.SetCrosshairMode(crosshair.ShowBasic)
        except Exception as exc:
            self._show_error(f"Could not jump to collaborator location: {exc}")

    def jump_to_selected_user(self, checked=False):
        del checked
        user = self._combo_current_text(self.collaborator_combo)
        if user and user in self.presence_by_user:
            self._apply_location(self.presence_by_user[user])

    def jump_to_selected_chat_anchor(self, checked=False):
        del checked
        sequence = self._combo_current_data(self.chat_location_combo)
        anchor = self.chat_anchors.get(int(sequence)) if sequence is not None else None
        if anchor:
            self._apply_location(anchor)

    def request_selected_segment_access(self, checked=False):
        del checked
        _, segment_id = self.owner.get_selected_segmentation_node_and_segment_id()
        if segment_id:
            self._queue_action("request_access", segment_id=segment_id, message="")

    def transfer_selected_segment_owner(self, checked=False):
        del checked
        _, segment_id = self.owner.get_selected_segmentation_node_and_segment_id()
        target = self._combo_current_text(self.collaborator_combo)
        if segment_id and target:
            self._queue_action(
                "transfer_owner", segment_id=segment_id, target_user=target
            )

    def set_selected_segment_review_state(self, checked=False):
        del checked
        _, segment_id = self.owner.get_selected_segmentation_node_and_segment_id()
        if not segment_id:
            self._show_error("Select a label first", popup=True)
            return
        self._queue_action(
            "review_state",
            segment_id=segment_id,
            state=str(self._combo_current_data(self.review_state_combo)),
            note=self._text(self.review_note_edit),
        )

    def set_selected_user_role(self, checked=False):
        del checked
        target = self._combo_current_text(self.collaborator_combo)
        if target:
            self._queue_action(
                "set_role", target_user=target, role=str(self._combo_current_data(self.role_combo))
            )

    def refresh_advanced_state(self, checked=False):
        del checked
        self._force_refresh = True
        self._last_advanced_fetch = 0.0
        self.on_timer()

    def filter_history_tree(self, value=None):
        query = str(value if value is not None else self._text(self.history_filter_edit)).casefold()
        count = self.history_tree.topLevelItemCount
        count = count() if callable(count) else count
        for index in range(int(count)):
            item = self.history_tree.topLevelItem(index)
            haystack = " ".join(str(item.text(column)) for column in range(6)).casefold()
            item.setHidden(bool(query) and query not in haystack)

    def request_room_snapshot(self, checked=False):
        del checked
        if self.connected:
            self._snapshot_label = self._text(self.snapshot_label_edit)
            self._snapshot_requested = True
            self.on_timer()

    def _selected_history_sequence(self):
        item = self.history_tree.currentItem()
        if item is None:
            return None
        try:
            return int(item.data(0, 32))
        except Exception:
            try:
                return int(item.text(0))
            except Exception:
                return None

    def restore_selected_revision(self, checked=False):
        del checked
        sequence = self._selected_history_sequence()
        if sequence is None:
            self._show_error("Select a revision in the timeline first", popup=True)
            return
        self._restoring_sequence = sequence
        self._queue_action("restore_revision", sequence=sequence)

    def _selected_conflict(self):
        item = self.conflict_tree.currentItem()
        if item is None:
            return None
        conflict_id = item.data(0, 32)
        return next(
            (entry for entry in self.conflicts_state if entry.get("id") == conflict_id),
            None,
        )

    def resolve_selected_conflict(self, resolution):
        conflict = self._selected_conflict()
        if conflict is None:
            self._show_error("Select a conflict first", popup=True)
            return
        if resolution == "mine":
            node = self._segmentation_node()
            if node is not None:
                key = (node.GetID(), str(conflict.get("segment_id")))
                self.force_snapshots.add(key)
                self.dirty_segments.add(key)
        self._queue_action(
            "resolve_conflict",
            conflict_id=conflict["id"],
            resolution=resolution,
            segment_id=conflict.get("segment_id"),
            other_sequence=int(conflict.get("other_sequence", 0)),
        )

    def refresh_backup_list(self, checked=False):
        del checked
        self._queue_action("list_backups")

    def _selected_backup(self):
        item = self.backup_tree.currentItem()
        if item is None:
            return None
        name = item.data(0, 32)
        return next(
            (entry for entry in getattr(self, "backup_records", []) if entry.get("name") == name),
            None,
        )

    def toggle_selected_backup_pin(self, checked=False):
        del checked
        backup = self._selected_backup()
        if backup:
            self._queue_action(
                "pin_backup", name=backup["name"], pinned=not bool(backup.get("pinned"))
            )

    def verify_selected_backup(self, checked=False):
        del checked
        backup = self._selected_backup()
        if backup:
            self._queue_action("verify_backup", name=backup["name"])

    def restore_selected_backup(self, checked=False):
        del checked
        backup = self._selected_backup()
        if not backup:
            self._show_error("Select a backup first", popup=True)
            return
        import qt
        import slicer

        answer = qt.QMessageBox.question(
            slicer.util.mainWindow(),
            "Restore Slicer project backup",
            "This leaves the live room and replaces the current Slicer scene. Continue?",
            qt.QMessageBox.Yes | qt.QMessageBox.No,
        )
        if answer != qt.QMessageBox.Yes:
            return
        path = str(backup["path"])
        self.leave()
        if not slicer.util.loadScene(path):
            slicer.util.errorDisplay(f"Could not restore backup: {path}")

    def run_room_diagnostics(self, checked=False):
        del checked
        self._queue_action("diagnostics")

    def export_diagnostics(self, checked=False):
        del checked
        import qt
        import slicer

        report = getattr(self, "last_diagnostics", None)
        if not report:
            self._show_error("Run diagnostics before exporting a report", popup=True)
            return
        path = qt.QFileDialog.getSaveFileName(
            slicer.util.mainWindow(), "Export diagnostic report", "LiveSegmentation-diagnostics.json", "JSON (*.json)"
        )
        if not path:
            return
        sanitized = {
            key: value
            for key, value in report.items()
            if key not in {"room_path", "server_url", "user", "room_name"}
        }
        Path(str(path)).write_text(
            json.dumps(sanitized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def _material_template_from_scene(self):
        node = self._segmentation_node()
        if node is None:
            raise LiveCollaborationError("Join a room with a segmentation first")
        segments = []
        for segment_id in node.GetSegmentation().GetSegmentIDs():
            segment = node.GetSegmentation().GetSegment(segment_id)
            terminology = ""
            try:
                found, value = segment.GetTag("TerminologyEntry")
                terminology = value if found else ""
            except Exception:
                pass
            segments.append(
                {
                    "id": segment_id,
                    "name": segment.GetName() or segment_id,
                    "color": self._segment_color_hex(segment),
                    "terminology": terminology,
                }
            )
        return validate_material_template({"name": self.room_name, "segments": segments})

    def publish_material_template(self, checked=False):
        del checked
        try:
            self._queue_action("set_template", template=self._material_template_from_scene())
        except Exception as exc:
            self._show_error(exc, popup=True)

    def apply_material_template(self, checked=False):
        del checked
        template = getattr(self, "material_template_state", None)
        if not template:
            self._show_error("This room does not have a material template yet", popup=True)
            return
        try:
            self.owner.apply_material_template(template)
        except Exception as exc:
            self._show_error(f"Could not apply material template: {exc}", popup=True)

    def _current_volume_signature(self):
        import slicer
        import vtk

        node = self.owner.get_volume_node()
        if node is None:
            raise LiveCollaborationError("Select the source volume first")
        matrix = vtk.vtkMatrix4x4()
        node.GetIJKToRASMatrix(matrix)
        return volume_signature(
            slicer.util.arrayFromVolume(node),
            spacing=node.GetSpacing(),
            origin=node.GetOrigin(),
            ijk_to_ras=[matrix.GetElement(row, column) for row in range(4) for column in range(4)],
        )

    def export_invitation(self, checked=False):
        del checked
        import qt
        import slicer

        try:
            transport = self._transport_mode()
            location = (
                self._text(self.shared_folder_edit)
                if transport == "shared-folder"
                else self._text(self.server_edit)
            )
            payload = build_invitation(
                transport,
                self._text(self.room_edit),
                self._current_volume_signature(),
                location,
                getattr(self, "material_template_state", None),
            )
            path = qt.QFileDialog.getSaveFileName(
                slicer.util.mainWindow(), "Export room invitation", "LiveSegmentation-room.liveseg", "Live Segmentation (*.liveseg)"
            )
            if path:
                Path(str(path)).write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
        except Exception as exc:
            self._show_error(f"Could not export invitation: {exc}", popup=True)

    def import_invitation(self, checked=False):
        del checked
        import qt
        import slicer

        path = qt.QFileDialog.getOpenFileName(
            slicer.util.mainWindow(), "Import room invitation", "", "Live Segmentation (*.liveseg);;JSON (*.json)"
        )
        if not path:
            return
        try:
            invitation = parse_invitation(Path(str(path)).read_text(encoding="utf-8"))
            self.room_edit.setText(invitation["room_name"])
            if invitation["transport"] == "shared-folder":
                self.transport_combo.setCurrentIndex(0)
                self.shared_folder_edit.setText(invitation["location"])
            else:
                self.transport_combo.setCurrentIndex(1)
                self.server_edit.setText(invitation["location"])
            self.invitation_volume_signature = invitation["volume_signature"]
            self.material_template_state = invitation.get("material_template")
            self._update_transport_fields()
            slicer.util.showStatusMessage("Live Segmentation invitation loaded", 3000)
        except Exception as exc:
            self._show_error(f"Could not import invitation: {exc}", popup=True)

    def join(self):
        import qt
        import slicer

        client = None
        try:
            self._session_token += 1
            user_name = self._text(self.user_edit)
            room_name = self._text(self.room_edit)
            if not room_name:
                raise ValueError("Enter a room name that all collaborators will use")
            volume_node = self.owner.get_volume_node()
            if volume_node is None:
                raise ValueError("Select the source volume first")
            volume_array = slicer.util.arrayFromVolume(volume_node)
            signature = self._current_volume_signature()
            expected_signature = getattr(self, "invitation_volume_signature", None)
            if expected_signature and expected_signature != signature:
                raise ValueError(
                    "The selected source volume does not match the imported invitation"
                )
            transport_mode = self._transport_mode()
            if transport_mode == "shared-folder":
                client = SharedFolderRoomClient(
                    self._text(self.shared_folder_edit),
                    user_name,
                )
            else:
                client = LiveRoomClient(
                    self._text(self.server_edit),
                    user_name,
                    self._text(self.api_key_edit),
                )
            self.status_label.setText("● Connecting…")
            self.status_label.setStyleSheet("color: #b26a00;")
            slicer.app.processEvents()
            room = client.join(room_name, signature)
            client.health_check(room["id"])
            segmentation_node = self.owner.prepare_shared_segmentation(room)
            if segmentation_node is None:
                raise ValueError("Could not create the shared segmentation")

            self.client = client
            self.room_id = room["id"]
            self.room_name = str(room["name"])
            self.user_name = user_name
            self.segmentation_node_id = segmentation_node.GetID()
            self.volume_shape = tuple(int(value) for value in volume_array.shape)
            self.last_sequence = 0
            self.initial_sequence = int(room.get("latest_sequence", 0))
            self.initial_sync_complete = self.initial_sequence == 0
            self.baselines.clear()
            self.dirty_segments.clear()
            self.force_snapshots.clear()
            self.outgoing.clear()
            self.outgoing_keys.clear()
            self.last_chat_sequence = 0
            self.pending_chat.clear()
            self.displayed_chat_sequences.clear()
            self.chat_history.clear()
            self.segment_owners.clear()
            self.segment_locks_state.clear()
            self.pending_lock_changes.clear()
            self.presence_by_user.clear()
            self.room_roles_state.clear()
            self.review_states_state.clear()
            self.access_requests_state.clear()
            self.chat_anchors.clear()
            self.history_records.clear()
            self.conflicts_state.clear()
            self.backup_records.clear()
            self.last_diagnostics = None
            self.pending_actions.clear()
            self._last_advanced_fetch = 0.0
            self._last_snapshot_sequence = 0
            self._snapshot_requested = False
            self._snapshot_label = ""
            self._restoring_sequence = None
            self._worker_results = queue.Queue()
            self._last_presence_send = 0.0
            self._last_metadata_fetch = 0.0
            self._last_health_check = 0.0
            self._last_backup_check = 0.0
            self._force_refresh = True
            self._connection_error_popup_shown = False
            self.connection_healthy = True
            self.connected = True
            self._observe_segmentation(segmentation_node)
            self._known_segment_ids = set(segmentation_node.GetSegmentation().GetSegmentIDs())
            if self.initial_sync_complete:
                self._initialize_baselines_and_seed(
                    seed=bool(room.get("created"))
                )

            settings = qt.QSettings()
            settings.setValue(self.SETTINGS_PREFIX + "user", user_name)
            settings.setValue(self.SETTINGS_PREFIX + "room", room_name)
            settings.setValue(self.SETTINGS_PREFIX + "transport", transport_mode)
            settings.setValue(
                self.SETTINGS_PREFIX + "automaticBackups",
                bool(self.backup_enabled_checkbox.checked),
            )
            settings.setValue(
                self.SETTINGS_PREFIX + "backupIntervalMinutes",
                int(self.backup_interval_spin.value),
            )
            settings.setValue(
                self.SETTINGS_PREFIX + "backupRetention",
                int(self.backup_retention_spin.value),
            )
            if transport_mode == "shared-folder":
                settings.setValue(
                    self.SETTINGS_PREFIX + "sharedFolder", str(client.shared_folder)
                )
            else:
                settings.setValue(self.SETTINGS_PREFIX + "server", client.server_url)
            self.user_edit.enabled = False
            self.room_edit.enabled = False
            self.transport_combo.enabled = False
            self.shared_folder_edit.enabled = False
            self.shared_folder_button.enabled = False
            self.server_edit.enabled = False
            self.api_key_edit.enabled = False
            self.backup_enabled_checkbox.enabled = False
            self.backup_interval_spin.enabled = False
            self.backup_retention_spin.enabled = False
            self.owner.set_live_inputs_enabled(False)
            self.owner.set_live_session_active(True)
            self.join_button.setText("Leave live room")
            self.refresh_button.enabled = True
            self.chat_input.enabled = True
            self.chat_send_button.enabled = True
            self.lock_expiry_spin.enabled = True
            self.refresh_history_button.enabled = True
            self.restore_revision_button.enabled = True
            self.create_snapshot_button.enabled = True
            self.run_diagnostics_button.enabled = True
            self.export_invite_button.enabled = True
            self.publish_template_button.enabled = True
            self.apply_template_button.enabled = True
            if isinstance(client, SharedFolderRoomClient):
                for button in (
                    self.refresh_backups_button,
                    self.pin_backup_button,
                    self.verify_backup_button,
                    self.restore_backup_button,
                ):
                    button.enabled = True
            self.status_label.setText(self._live_status_text())
            self.status_label.setStyleSheet("color: #188038; font-weight: bold;")
            self._update_presence(room.get("presence") or [])
            self.timer.start()
            self.on_timer()
        except Exception as exc:
            self.connected = False
            self.connection_healthy = False
            if client is not None:
                try:
                    if getattr(client, "_room_id", None):
                        client.leave(getattr(client, "_room_id"))
                except Exception:
                    pass
            try:
                self.owner.clear_live_segmentation()
            except Exception:
                pass
            self.owner.set_live_inputs_enabled(True)
            self.owner.set_live_session_active(False)
            self._show_error(exc, popup=True)

    def leave(self):
        client = self.client
        room_id = self.room_id
        segmentation_node_id = self.segmentation_node_id
        self.connected = False
        self.connection_healthy = False
        self._session_token += 1
        if hasattr(self, "timer"):
            self.timer.stop()
        self._unobserve_segmentation()
        if client is not None and room_id:
            try:
                client.leave(room_id)
            except Exception:
                pass
        self.client = None
        self.room_id = None
        self.room_name = None
        self.user_name = None
        self.segmentation_node_id = None
        self.volume_shape = None
        self.last_sequence = 0
        self.initial_sequence = 0
        self.initial_sync_complete = False
        self.baselines.clear()
        self.dirty_segments.clear()
        self.force_snapshots.clear()
        self.outgoing.clear()
        self.outgoing_keys.clear()
        self._known_segment_ids.clear()
        self.pending_chat.clear()
        self.displayed_chat_sequences.clear()
        self.last_chat_sequence = 0
        self.segment_owners.clear()
        self.segment_locks_state.clear()
        self.pending_lock_changes.clear()
        self.presence_by_user.clear()
        self.room_roles_state.clear()
        self.review_states_state.clear()
        self.access_requests_state.clear()
        self.chat_anchors.clear()
        self.history_records.clear()
        self.conflicts_state.clear()
        self.material_template_state = None
        self.backup_records.clear()
        self.last_diagnostics = None
        self.pending_actions.clear()
        self._last_snapshot_sequence = 0
        self._snapshot_requested = False
        self._snapshot_label = ""
        self._restoring_sequence = None
        self._worker_results = queue.Queue()
        self._force_refresh = False
        self._connection_error_popup_shown = False
        try:
            self.owner.clear_live_segmentation(segmentation_node_id)
        except Exception:
            pass
        self.user_edit.enabled = True
        self.room_edit.enabled = True
        self.transport_combo.enabled = True
        self.shared_folder_edit.enabled = True
        self.shared_folder_button.enabled = True
        self.server_edit.enabled = True
        self.api_key_edit.enabled = True
        self.backup_enabled_checkbox.enabled = True
        self.backup_interval_spin.enabled = True
        self.backup_retention_spin.enabled = True
        self.owner.set_live_inputs_enabled(True)
        self.owner.set_live_session_active(False)
        self._update_transport_fields()
        self.join_button.setText("Join live room")
        self.refresh_button.enabled = False
        self.chat_input.enabled = False
        self.chat_send_button.enabled = False
        self.chat_history.clear()
        self.chat_input.clear()
        self.chat_location_combo.clear()
        self.collaborator_combo.clear()
        self.history_tree.clear()
        self.conflict_tree.clear()
        self.backup_tree.clear()
        self.diagnostics_text.clear()
        self.lock_button.enabled = False
        self.lock_button.setText("Lock selected label")
        self.lock_status_label.setText("Select a label after joining to manage its lock")
        self.request_access_button.enabled = False
        self.transfer_owner_button.enabled = False
        self.set_review_button.enabled = False
        self.set_role_button.enabled = False
        self.jump_to_user_button.enabled = False
        self.jump_to_chat_button.enabled = False
        for button in (
            self.refresh_history_button,
            self.restore_revision_button,
            self.create_snapshot_button,
            self.refresh_backups_button,
            self.pin_backup_button,
            self.verify_backup_button,
            self.restore_backup_button,
        ):
            button.enabled = False
        try:
            self.owner.clear_remote_highlights()
        except Exception:
            pass
        self.status_label.setText("● Offline")
        self.status_label.setStyleSheet("color: #777;")
        self.users_label.setText("Nobody else is connected")

    def cleanup(self):
        self.leave()

    def _segmentation_node(self):
        import slicer

        return slicer.mrmlScene.GetNodeByID(self.segmentation_node_id) if self.segmentation_node_id else None

    def _observe_segmentation(self, node):
        import vtkSegmentationCorePython as vtkSegmentationCore

        self._unobserve_segmentation()
        self._observed_node = node
        self._observed_segmentation = node.GetSegmentation()
        observed_events = (
            vtkSegmentationCore.vtkSegmentation.SegmentAdded,
            vtkSegmentationCore.vtkSegmentation.SegmentRemoved,
            vtkSegmentationCore.vtkSegmentation.SegmentModified,
            vtkSegmentationCore.vtkSegmentation.SourceRepresentationModified,
        )
        for event_id in observed_events:
            self._observer_tags.append(
                self._observed_segmentation.AddObserver(
                    event_id, self._on_segmentation_modified
                )
            )

    def _unobserve_segmentation(self):
        if self._observed_segmentation is not None:
            for observer_tag in self._observer_tags:
                try:
                    self._observed_segmentation.RemoveObserver(observer_tag)
                except Exception:
                    pass
        self._observed_node = None
        self._observed_segmentation = None
        self._observer_tags = []

    def _on_segmentation_modified(self, caller=None, event=None):
        del caller, event
        if self._applying_remote or not self.connected:
            return
        node = self._segmentation_node()
        if node is None:
            return
        current_ids = set(node.GetSegmentation().GetSegmentIDs())
        for segment_id in current_ids - self._known_segment_ids:
            self.force_snapshots.add((node.GetID(), segment_id))
            self.segment_owners.setdefault(segment_id, self.user_name)
        self._known_segment_ids = current_ids
        # Mark every segment as a candidate. Delta encoding below discards the
        # unchanged ones. This observes edits from Segment Editor and from any
        # separately installed module without coupling to that module's UI.
        for segment_id in current_ids:
            self.dirty_segments.add((node.GetID(), segment_id))
        self._update_lock_controls()

    def _initialize_baselines_and_seed(self, seed=False):
        node = self._segmentation_node()
        if node is None:
            return
        for segment_id in node.GetSegmentation().GetSegmentIDs():
            key = (node.GetID(), segment_id)
            mask = self._read_mask(node, segment_id)
            if seed:
                self.baselines[key] = np.zeros_like(mask)
                self.force_snapshots.add(key)
                self.dirty_segments.add(key)
            else:
                self.baselines[key] = mask.copy()

    def _read_mask(self, node, segment_id):
        volume_node = self.owner.get_volume_node()
        mask = self.owner.segment_mask_in_reference_geometry(
            node, segment_id, volume_node, self.volume_shape
        )
        return np.asarray(mask, dtype=np.uint8)

    @staticmethod
    def _segment_color_hex(segment):
        color = [0.29, 0.56, 0.89]
        try:
            returned = segment.GetColor()
            if returned is not None:
                color = list(returned)
        except Exception:
            try:
                segment.GetColor(color)
            except Exception:
                pass
        values = [max(0, min(255, int(round(float(item) * 255)))) for item in color[:3]]
        return "#" + "".join(f"{value:02X}" for value in values)

    def _locked_by_other(self, segment_id):
        state = self.segment_locks_state.get(segment_id) or {}
        return bool(state.get("locked")) and state.get("owner") != self.user_name

    def _current_role(self):
        return str(self.room_roles_state.get(self.user_name) or "editor")

    def _set_segment_collaboration_tags(self, segment_id):
        node = self._segmentation_node()
        segment = node.GetSegmentation().GetSegment(segment_id) if node is not None else None
        if segment is None:
            return
        owner = self.segment_owners.get(segment_id, "")
        state = self.segment_locks_state.get(segment_id) or {}
        try:
            segment.SetTag("LiveSegmentation.Owner", str(owner))
            segment.SetTag(
                "LiveSegmentation.LockedBy",
                str(owner) if state.get("locked") else "",
            )
        except Exception:
            pass

    def _restore_locked_segment(self, node, segment_id, baseline):
        import slicer

        if baseline is None:
            return
        self._applying_remote = True
        try:
            self.owner.update_segment_binary_labelmap_from_array(
                baseline, node, segment_id, self.owner.get_volume_node()
            )
            self.owner.refresh_segmentation_display(node, segment_id)
        finally:
            self._applying_remote = False
        owner = (self.segment_locks_state.get(segment_id) or {}).get("owner", "another user")
        slicer.util.showStatusMessage(
            f"Label is locked by {owner}; the local edit was reverted", 3500
        )

    def _prepare_outgoing(self):
        import slicer

        if not self.initial_sync_complete:
            return
        for key in list(self.dirty_segments):
            if key in self.outgoing_keys:
                continue
            node = slicer.mrmlScene.GetNodeByID(key[0])
            if node is None or node.GetSegmentation().GetSegment(key[1]) is None:
                self.dirty_segments.discard(key)
                continue
            current = self._read_mask(node, key[1])
            previous = self.baselines.get(key)
            if self._current_role() == "viewer":
                if previous is not None and np.any(current != previous):
                    self._restore_locked_segment(node, key[1], previous)
                self.dirty_segments.discard(key)
                self.force_snapshots.discard(key)
                continue
            if self._locked_by_other(key[1]):
                if previous is not None and np.any(current != previous):
                    self._restore_locked_segment(node, key[1], previous)
                self.dirty_segments.discard(key)
                self.force_snapshots.discard(key)
                continue
            replace = key in self.force_snapshots or previous is None
            if previous is None:
                previous = np.zeros_like(current)
            encoded = encode_mask_delta(previous, current, replace=replace)
            if encoded is None:
                self.dirty_segments.discard(key)
                self.force_snapshots.discard(key)
                continue
            segment = node.GetSegmentation().GetSegment(key[1])
            operation = {
                "client_operation_id": str(uuid.uuid4()),
                "segment_id": key[1],
                "segment_name": segment.GetName() or key[1],
                "color_hex": self._segment_color_hex(segment),
                "base_sequence": int(self.last_sequence),
                **encoded,
            }
            self.outgoing.append(operation)
            self.outgoing_keys.add(key)
            self.dirty_segments.discard(key)
            self.force_snapshots.discard(key)

    def _active_presence(self):
        details = self._current_location()
        details["color"] = [round(value, 4) for value in stable_user_color(self.user_name)]
        details["role"] = self._current_role()
        try:
            _, segment_id = self.owner.get_selected_segmentation_node_and_segment_id()
            node = self._segmentation_node()
            segment = node.GetSegmentation().GetSegment(segment_id) if node and segment_id else None
            if segment is not None:
                details["active_segment_id"] = segment_id
                details["active_segment_name"] = segment.GetName() or segment_id
        except Exception:
            pass
        return details

    def _snapshot_operations(self):
        node = self._segmentation_node()
        if node is None:
            return []
        result = []
        for segment_id in node.GetSegmentation().GetSegmentIDs():
            segment = node.GetSegmentation().GetSegment(segment_id)
            mask = self.baselines.get((node.GetID(), segment_id))
            if mask is None:
                mask = self._read_mask(node, segment_id)
            encoded = encode_mask_delta(np.zeros_like(mask), mask, replace=True)
            if encoded is not None:
                result.append(
                    {
                        "segment_id": segment_id,
                        "segment_name": segment.GetName() or segment_id,
                        "color_hex": self._segment_color_hex(segment),
                        **encoded,
                    }
                )
        return result

    def on_timer(self):
        if not self.connected:
            return
        self._drain_worker_results()
        if self._worker is not None and self._worker.is_alive():
            return
        self._prepare_outgoing()
        outgoing = list(self.outgoing)
        chat_outgoing = list(self.pending_chat)
        lock_changes = dict(self.pending_lock_changes)
        actions = list(self.pending_actions)
        after_sequence = int(self.last_sequence)
        now = time.time()
        presence = None
        if now - self._last_presence_send >= 1.0:
            presence = self._active_presence()
            self._last_presence_send = now

        fetch_metadata = self._force_refresh or now - self._last_metadata_fetch >= 1.0
        fetch_advanced = self._force_refresh or now - self._last_advanced_fetch >= 5.0
        health_check = self._force_refresh or now - self._last_health_check >= 2.0
        backup_check = False
        backup_interval_seconds = 300.0
        backup_value = self.backup_interval_spin.value
        backup_value = backup_value() if callable(backup_value) else backup_value
        backup_enabled = self.backup_enabled_checkbox.checked
        backup_enabled = backup_enabled() if callable(backup_enabled) else backup_enabled
        if (
            bool(backup_enabled)
            and isinstance(self.client, SharedFolderRoomClient)
            and self.initial_sync_complete
            and not outgoing
            and not self.dirty_segments
            and now - self._last_backup_check >= 10.0
        ):
            backup_check = True
            backup_interval_seconds = max(60.0, float(backup_value) * 60.0)
            self._last_backup_check = now
        if fetch_metadata:
            self._last_metadata_fetch = now
        if health_check:
            self._last_health_check = now
        if fetch_advanced:
            self._last_advanced_fetch = now
        snapshot_operations = []
        snapshot_label = ""
        should_snapshot = (
            self.initial_sync_complete
            and not outgoing
            and not self.dirty_segments
            and (
                self._snapshot_requested
                or self.last_sequence - self._last_snapshot_sequence >= 100
            )
        )
        if should_snapshot:
            snapshot_operations = self._snapshot_operations()
            snapshot_label = self._snapshot_label
            self._snapshot_requested = False
            self._snapshot_label = ""
        self._force_refresh = False

        self._worker = threading.Thread(
            target=self._sync_worker,
            args=(
                self._session_token,
                self.client,
                self.room_id,
                outgoing,
                after_sequence,
                presence,
                chat_outgoing,
                self.last_chat_sequence,
                lock_changes,
                actions,
                fetch_metadata,
                fetch_advanced,
                health_check,
                backup_check,
                backup_interval_seconds,
                snapshot_operations,
                snapshot_label,
            ),
            name="LiveSegmentation-sync",
            daemon=True,
        )
        self._worker.start()

    def _sync_worker(
        self,
        session_token,
        client,
        room_id,
        outgoing,
        after_sequence,
        presence,
        chat_outgoing,
        after_chat_sequence,
        lock_changes,
        actions,
        fetch_metadata,
        fetch_advanced,
        health_check,
        backup_check,
        backup_interval_seconds,
        snapshot_operations,
        snapshot_label,
    ):
        started = time.monotonic()
        try:
            outgoing_ids = []
            rejected_segments = []
            chat_ids = []
            lock_segment_ids = []
            action_ids = []
            action_results = []
            conflicts_detected = []
            command_errors = []
            if health_check:
                client.health_check(room_id)
            for operation in outgoing:
                try:
                    pushed = client.push_operation(room_id, operation)
                    outgoing_ids.append(operation["client_operation_id"])
                    conflicts_detected.extend((pushed or {}).get("conflicts") or [])
                except Exception as exc:
                    message = str(exc)
                    if "locked by" not in message:
                        raise
                    outgoing_ids.append(operation["client_operation_id"])
                    rejected_segments.append(operation["segment_id"])
                    command_errors.append(message)
            for message in chat_outgoing:
                try:
                    client.send_chat(
                        room_id,
                        message["text"],
                        message["client_message_id"],
                        message.get("anchor"),
                    )
                    chat_ids.append(message["client_message_id"])
                except Exception as exc:
                    message_text = str(exc)
                    if not any(
                        marker in message_text
                        for marker in ("4000 characters", "Enter a chat message")
                    ):
                        raise
                    chat_ids.append(message["client_message_id"])
                    command_errors.append(message_text)
            for segment_id, change in lock_changes.items():
                try:
                    if isinstance(change, dict):
                        client.set_segment_lock(
                            room_id,
                            segment_id,
                            bool(change.get("locked")),
                            int(change.get("expires_minutes", 0)),
                        )
                    else:
                        client.set_segment_lock(room_id, segment_id, bool(change))
                    lock_segment_ids.append(segment_id)
                except Exception as exc:
                    message = str(exc)
                    if not any(
                        marker in message
                        for marker in ("Only ", "must synchronize once")
                    ):
                        raise
                    lock_segment_ids.append(segment_id)
                    command_errors.append(message)
            for action in actions:
                try:
                    kind = action.get("action")
                    value = None
                    if kind == "request_access":
                        value = client.request_segment_access(
                            room_id, action["segment_id"], action.get("message", "")
                        )
                    elif kind == "transfer_owner":
                        value = client.transfer_segment_owner(
                            room_id, action["segment_id"], action["target_user"]
                        )
                    elif kind == "review_state":
                        value = client.set_review_state(
                            room_id,
                            action["segment_id"],
                            action["state"],
                            action.get("note", ""),
                        )
                    elif kind == "set_role":
                        value = client.set_room_role(
                            room_id, action["target_user"], action["role"]
                        )
                    elif kind == "list_backups":
                        value = (
                            client.list_project_backups(room_id)
                            if hasattr(client, "list_project_backups")
                            else []
                        )
                    elif kind == "pin_backup":
                        value = client.set_backup_pinned(
                            room_id, action["name"], action["pinned"]
                        )
                    elif kind == "verify_backup":
                        value = client.verify_project_backup(room_id, action["name"])
                    elif kind == "diagnostics":
                        value = client.diagnostics(room_id)
                    elif kind == "set_template":
                        value = client.set_material_template(room_id, action["template"])
                    elif kind == "restore_revision":
                        value = client.state_at_sequence(room_id, action["sequence"])
                    elif kind == "resolve_conflict":
                        state = None
                        if action["resolution"] in {"other", "union"}:
                            state = client.state_at_sequence(
                                room_id, action["other_sequence"]
                            )
                        value = {
                            "record": client.resolve_conflict(
                                room_id, action["conflict_id"], action["resolution"]
                            ),
                            "state": state,
                        }
                    action_results.append(
                        {"id": action["id"], "action": kind, "value": value, "request": action}
                    )
                    action_ids.append(action["id"])
                except Exception as exc:
                    action_ids.append(action["id"])
                    command_errors.append(str(exc))
            snapshot = None
            if snapshot_operations:
                snapshot = client.publish_room_snapshot(
                    room_id, snapshot_operations, compact=True, label=snapshot_label
                )
            operations = client.operations(room_id, after_sequence)
            users = client.presence(room_id, presence) if presence is not None else None
            messages = (
                client.chat_messages(room_id, after_chat_sequence)
                if fetch_metadata or chat_outgoing
                else None
            )
            locks = (
                client.segment_locks(room_id)
                if fetch_metadata or lock_changes
                else None
            )
            advanced = None
            if fetch_advanced:
                advanced = {
                    "history": client.room_history(room_id, 500),
                    "conflicts": client.room_conflicts(room_id, False),
                    "roles": client.room_roles(room_id),
                    "reviews": client.review_states(room_id),
                    "access_requests": client.segment_access_requests(room_id),
                    "material_template": client.get_material_template(room_id),
                    "audit": client.audit_events(room_id, 100),
                }
                if hasattr(client, "snapshot_manifests"):
                    advanced["snapshots"] = client.snapshot_manifests(room_id)
            backup = (
                client.reserve_project_backup(room_id, backup_interval_seconds)
                if backup_check
                else None
            )
            self._worker_results.put(
                {
                    "session_token": session_token,
                    "outgoing_ids": outgoing_ids,
                    "rejected_segments": rejected_segments,
                    "chat_ids": chat_ids,
                    "lock_segment_ids": lock_segment_ids,
                    "action_ids": action_ids,
                    "action_results": action_results,
                    "conflicts_detected": conflicts_detected,
                    "command_errors": command_errors,
                    "operations": operations,
                    "users": users,
                    "messages": messages,
                    "locks": locks,
                    "advanced": advanced,
                    "snapshot": snapshot,
                    "backup": backup,
                    "duration": time.monotonic() - started,
                }
            )
        except Exception as exc:
            self._worker_results.put(
                {
                    "session_token": session_token,
                    "error": str(exc),
                    "duration": time.monotonic() - started,
                }
            )

    def _drain_worker_results(self):
        while True:
            try:
                result = self._worker_results.get_nowait()
            except queue.Empty:
                return
            if (
                result.get("session_token") != self._session_token
                or not self.connected
            ):
                continue
            if "error" in result:
                show_popup = not self._connection_error_popup_shown
                self._connection_error_popup_shown = True
                self._show_error(result["error"], popup=show_popup)
                continue
            self.connection_healthy = True
            self._connection_error_popup_shown = False
            self._last_sync_duration = float(result.get("duration", 0.0))
            sent_ids = set(result.get("outgoing_ids") or [])
            if sent_ids:
                retained = []
                self.outgoing_keys.clear()
                for operation in self.outgoing:
                    if operation["client_operation_id"] not in sent_ids:
                        retained.append(operation)
                self.outgoing = retained
                for operation in retained:
                    self.outgoing_keys.add(
                        (self.segmentation_node_id, operation["segment_id"])
                    )
            sent_chat_ids = set(result.get("chat_ids") or [])
            if sent_chat_ids:
                self.pending_chat = [
                    item
                    for item in self.pending_chat
                    if item["client_message_id"] not in sent_chat_ids
                ]
            for segment_id in result.get("lock_segment_ids") or []:
                self.pending_lock_changes.pop(segment_id, None)
            completed_action_ids = set(result.get("action_ids") or [])
            if completed_action_ids:
                self.pending_actions = [
                    item for item in self.pending_actions if item["id"] not in completed_action_ids
                ]
            self._apply_operations(result.get("operations") or [])
            if result.get("users") is not None:
                self._update_presence(result["users"])
            if result.get("messages") is not None:
                self._append_chat_messages(result["messages"])
            if result.get("locks") is not None:
                self._update_segment_locks(result["locks"])
            if result.get("advanced") is not None:
                self._update_advanced_state(result["advanced"])
            self._handle_action_results(result.get("action_results") or [])
            if result.get("snapshot"):
                self._last_snapshot_sequence = max(
                    self._last_snapshot_sequence,
                    int(result["snapshot"].get("last_sequence", self.last_sequence)),
                )
                import slicer

                slicer.util.showStatusMessage("Live room snapshot and history compaction completed", 4000)
            if result.get("conflicts_detected"):
                import slicer

                overlap = sum(
                    int(item.get("overlap_voxels", 0))
                    for item in result["conflicts_detected"]
                )
                slicer.util.warningDisplay(
                    f"Concurrent edits overlapped in {overlap} voxels. Review the conflict panel."
                )
                self._last_advanced_fetch = 0.0
            for segment_id in result.get("rejected_segments") or []:
                if self.segmentation_node_id:
                    self.dirty_segments.add((self.segmentation_node_id, segment_id))
            if result.get("command_errors"):
                import slicer

                message = "; ".join(result["command_errors"])
                slicer.util.showStatusMessage(f"Live collaboration: {message}", 5000)
            if not self.initial_sync_complete and self.last_sequence >= self.initial_sequence:
                self.initial_sync_complete = True
                self._initialize_baselines_and_seed(seed=False)
            backup_ok = True
            if result.get("backup"):
                backup_ok = self._create_project_backup(result["backup"])
            if self.connected and backup_ok:
                if self._last_sync_duration >= 2.5:
                    self.status_label.setText(
                        f"● Slow connection ({self._last_sync_duration:.1f} s) — "
                        + self._live_status_text().lstrip("● ")
                    )
                    self.status_label.setStyleSheet(
                        "color: #b26a00; font-weight: bold;"
                    )
                else:
                    self.status_label.setText(self._live_status_text())
                    self.status_label.setStyleSheet(
                        "color: #188038; font-weight: bold;"
                    )
                self._last_error = None
            self._update_lock_controls()

    def _queue_snapshot_operations(self, operations):
        for operation in operations or []:
            segment_id = str(operation.get("segment_id") or "")
            if not segment_id:
                continue
            queued = {
                **operation,
                "client_operation_id": str(uuid.uuid4()),
                "operation_kind": "snapshot",
                "base_sequence": int(self.last_sequence),
            }
            self.outgoing.append(queued)

    def _handle_action_results(self, results):
        import slicer

        for result in results:
            action = result.get("action")
            value = result.get("value")
            request = result.get("request") or {}
            if action == "restore_revision":
                self._queue_snapshot_operations(value)
                self._restoring_sequence = None
                slicer.util.showStatusMessage(
                    f"Historical state queued as a new revision at sequence {request.get('sequence')}",
                    4500,
                )
            elif action == "resolve_conflict":
                resolution = request.get("resolution")
                state = (value or {}).get("state") if isinstance(value, dict) else None
                if resolution in {"other", "union"} and state:
                    selected = next(
                        (
                            item
                            for item in state
                            if str(item.get("segment_id")) == str(request.get("segment_id"))
                        ),
                        None,
                    )
                    if selected is not None and resolution == "other":
                        self._queue_snapshot_operations([selected])
                    elif selected is not None and resolution == "union":
                        node = self._segmentation_node()
                        segment_id = str(request.get("segment_id"))
                        current = (
                            self._read_mask(node, segment_id)
                            if node is not None
                            else np.zeros(tuple(selected["volume_shape"]), dtype=np.uint8)
                        )
                        other = apply_mask_delta(np.zeros_like(current), selected)
                        union = np.logical_or(current, other).astype(np.uint8)
                        encoded = encode_mask_delta(np.zeros_like(union), union, replace=True)
                        self._queue_snapshot_operations(
                            [
                                {
                                    "segment_id": segment_id,
                                    "segment_name": selected.get("segment_name") or segment_id,
                                    "color_hex": selected.get("color_hex") or "#4A90E2",
                                    **encoded,
                                }
                            ]
                        )
                self._last_advanced_fetch = 0.0
            elif action == "list_backups":
                self._update_backup_tree(value or [])
            elif action == "pin_backup":
                self._queue_action("list_backups")
            elif action == "verify_backup":
                valid = bool((value or {}).get("valid"))
                self.backup_status_label.setText(
                    f"Checksum {'valid' if valid else 'FAILED'}: {(value or {}).get('name', '')}"
                )
            elif action == "diagnostics":
                self.last_diagnostics = dict(value or {})
                self.last_diagnostics.update(
                    {
                        "pending_operations": len(self.outgoing),
                        "dirty_segments": len(self.dirty_segments),
                        "pending_chat_messages": len(self.pending_chat),
                        "connection_healthy": bool(self.connection_healthy),
                    }
                )
                self.diagnostics_text.setPlainText(
                    json.dumps(self.last_diagnostics, ensure_ascii=False, indent=2)
                )
            elif action == "set_template":
                self.material_template_state = value
                slicer.util.showStatusMessage("Room material template published", 3000)
            elif action in {
                "request_access",
                "transfer_owner",
                "review_state",
                "set_role",
            }:
                self._last_advanced_fetch = 0.0

    def _update_backup_tree(self, backups):
        import qt

        self.backup_records = list(backups)
        self.backup_tree.clear()
        for backup in self.backup_records:
            size_mb = float(backup.get("size_bytes", 0)) / (1024.0 * 1024.0)
            item = qt.QTreeWidgetItem(
                [
                    str(backup.get("name") or ""),
                    f"{size_mb:.1f} MB",
                    "yes" if backup.get("pinned") else "no",
                    "stored" if backup.get("sha256") else "legacy",
                ]
            )
            item.setData(0, 32, backup.get("name"))
            self.backup_tree.addTopLevelItem(item)

    def _update_advanced_state(self, advanced):
        import qt

        self.history_records = list(advanced.get("history") or [])
        self.history_tree.clear()
        for record in reversed(self.history_records):
            item = qt.QTreeWidgetItem(
                [
                    str(record.get("sequence", "")),
                    str(record.get("author") or ""),
                    str(record.get("segment_name") or record.get("segment_id") or ""),
                    str(record.get("changed_voxels", "")),
                    (
                        f"checkpoint: {record.get('snapshot_label')}"
                        if record.get("system_snapshot") and record.get("snapshot_label")
                        else "checkpoint"
                        if record.get("system_snapshot")
                        else str(record.get("operation_kind") or "patch")
                    ),
                    str(record.get("created_at") or "")[:19],
                ]
            )
            item.setData(0, 32, int(record.get("sequence", 0)))
            self.history_tree.addTopLevelItem(item)
        self.filter_history_tree()
        self.conflicts_state = list(advanced.get("conflicts") or [])
        self.conflict_tree.clear()
        for conflict in self.conflicts_state:
            item = qt.QTreeWidgetItem(
                [
                    str(conflict.get("sequence", "")),
                    str(conflict.get("segment_id") or ""),
                    str(conflict.get("other_author") or ""),
                    str(conflict.get("overlap_voxels", "")),
                    str(conflict.get("resolution") or "unresolved"),
                ]
            )
            item.setData(0, 32, conflict.get("id"))
            self.conflict_tree.addTopLevelItem(item)
        for button in (
            self.resolve_latest_button,
            self.resolve_mine_button,
            self.resolve_other_button,
            self.resolve_union_button,
        ):
            button.enabled = bool(self.conflicts_state)
        self.room_roles_state = {
            str(item.get("user")): str(item.get("role"))
            for item in advanced.get("roles") or []
        }
        self.review_states_state = {
            str(item.get("segment_id")): item for item in advanced.get("reviews") or []
        }
        self.access_requests_state = list(advanced.get("access_requests") or [])
        self.material_template_state = advanced.get("material_template")
        snapshots = advanced.get("snapshots") or []
        if snapshots:
            self._last_snapshot_sequence = max(
                int(item.get("last_sequence", 0)) for item in snapshots
            )
        self._update_lock_controls()

    def _append_chat_messages(self, messages):
        for message in sorted(messages, key=lambda item: int(item.get("sequence", 0))):
            sequence = int(message.get("sequence", 0))
            if sequence <= self.last_chat_sequence or sequence in self.displayed_chat_sequences:
                continue
            stamp = str(message.get("created_at") or "")
            try:
                clock = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone()
                clock_text = clock.strftime("%H:%M")
            except Exception:
                clock_text = "--:--"
            author = str(message.get("author") or "Unknown")
            text = str(message.get("text") or "")
            anchor = message.get("anchor")
            marker = " 📍" if isinstance(anchor, dict) and anchor else ""
            self.chat_history.appendPlainText(f"[{clock_text}] {author}: {text}{marker}")
            if isinstance(anchor, dict) and anchor:
                self.chat_anchors[sequence] = anchor
                self.chat_location_combo.addItem(
                    f"[{clock_text}] {author}: {text[:60]}", sequence
                )
                self.jump_to_chat_button.enabled = True
            self.displayed_chat_sequences.add(sequence)
            self.last_chat_sequence = max(self.last_chat_sequence, sequence)
        scroll_bar = self.chat_history.verticalScrollBar()
        maximum = scroll_bar.maximum
        maximum = maximum() if callable(maximum) else maximum
        scroll_bar.setValue(maximum)

    def _update_segment_locks(self, locks):
        states = {}
        for state in locks:
            segment_id = str(state.get("segment_id") or "")
            if not segment_id:
                continue
            owner = str(state.get("owner") or "")
            if owner:
                self.segment_owners[segment_id] = owner
            states[segment_id] = {
                "owner": owner,
                "locked": bool(state.get("locked", False)),
                "updated_at": state.get("updated_at"),
            }
            self._set_segment_collaboration_tags(segment_id)
        self.segment_locks_state = states
        for segment_id in states:
            self._set_segment_collaboration_tags(segment_id)
        self._update_lock_controls()

    def _update_lock_controls(self):
        if not self.connected:
            return
        _, segment_id = self.owner.get_selected_segmentation_node_and_segment_id()
        if not segment_id:
            self.lock_status_label.setText("Select a label to manage its lock")
            self.lock_button.enabled = False
            self.lock_button.setText("Lock selected label")
            return
        node = self._segmentation_node()
        segment = node.GetSegmentation().GetSegment(segment_id) if node is not None else None
        segment_name = segment.GetName() if segment is not None else segment_id
        owner = self.segment_owners.get(segment_id)
        state = self.segment_locks_state.get(segment_id) or {}
        locked = bool(state.get("locked", False))
        if segment_id in self.pending_lock_changes:
            self.lock_status_label.setText(f"{segment_name}: lock update pending…")
            self.lock_button.enabled = False
            return
        if owner is None:
            owner = self.user_name
            self.segment_owners[segment_id] = owner
        if locked:
            expiry = state.get("expires_at")
            suffix = f" until {str(expiry)[:16]}" if expiry else ""
            self.lock_status_label.setText(f"{segment_name}: locked by {owner}{suffix}")
        else:
            self.lock_status_label.setText(f"{segment_name}: unlocked · created by {owner}")
        pending_requests = [
            item
            for item in self.access_requests_state
            if str(item.get("segment_id")) == str(segment_id)
            and item.get("status") == "pending"
        ]
        if pending_requests and (owner == self.user_name or self._current_role() == "admin"):
            names = ", ".join(str(item.get("requester")) for item in pending_requests)
            current_status = self.lock_status_label.text
            current_status = (
                current_status() if callable(current_status) else current_status
            )
            self.lock_status_label.setText(
                str(current_status) + f" · access requested by {names}"
            )
        role = self._current_role()
        can_manage = owner == self.user_name or role == "admin"
        self.lock_button.enabled = can_manage
        self.request_access_button.enabled = locked and owner != self.user_name
        self.transfer_owner_button.enabled = can_manage and bool(self.presence_by_user)
        self.set_review_button.enabled = role != "viewer"
        self.set_role_button.enabled = role == "admin" and bool(self.presence_by_user)
        current_review = self.review_states_state.get(segment_id) or {}
        review_state = str(current_review.get("state") or "draft")
        for index in range(self.review_state_combo.count):
            if str(self.review_state_combo.itemData(index)) == review_state:
                self.review_state_combo.setCurrentIndex(index)
                break
        if can_manage:
            self.lock_button.setText(
                "Unlock selected label" if locked else "Lock selected label"
            )
        else:
            self.lock_button.setText(f"Only {owner} can change this lock")

    def _create_project_backup(self, reservation):
        import slicer

        destination = Path(str(reservation["path"]))
        temporary_destination = _atomic_temporary_path(destination)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="LiveSegmentation-backup-") as temp_dir:
                local_bundle = Path(temp_dir) / destination.name
                saved = slicer.util.saveScene(str(local_bundle))
                if saved is False or not local_bundle.is_file():
                    raise LiveCollaborationError("Slicer could not create the project bundle")
                shutil.copyfile(local_bundle, temporary_destination)
                os.replace(temporary_destination, destination)
            checksum = sha256_file(destination)
            _write_json_atomic(
                destination.with_suffix(destination.suffix + ".json"),
                {
                    "name": destination.name,
                    "created_at": _utc_iso(),
                    "created_by": self.user_name,
                    "size_bytes": destination.stat().st_size,
                    "sha256": checksum,
                    "pinned": False,
                },
            )
            _write_json_atomic(
                Path(str(reservation["state_path"])),
                {
                    "last_backup_epoch": time.time(),
                    "completed_at": _utc_iso(),
                    "completed_by": self.user_name,
                    "token": reservation["token"],
                    "file": destination.name,
                    "size_bytes": destination.stat().st_size,
                    "sha256": checksum,
                },
            )
            retention = self.backup_retention_spin.value
            retention = retention() if callable(retention) else retention
            if isinstance(self.client, SharedFolderRoomClient):
                self.client.prune_project_backups(self.room_id, int(retention))
                self._queue_action("list_backups")
            self.backup_status_label.setText(
                f"Last complete project backup: {destination.name}"
            )
            slicer.util.showStatusMessage(
                f"Live Segmentation backup saved: {destination.name}", 3500
            )
            return True
        except Exception as exc:
            try:
                temporary_destination.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                state_path = Path(str(reservation["state_path"]))
                state = _read_json_file(state_path) if state_path.is_file() else {}
                if state.get("token") == reservation.get("token"):
                    state["last_backup_epoch"] = 0.0
                    state["failed_at"] = _utc_iso()
                    state["error"] = str(exc)
                    _write_json_atomic(state_path, state)
            except Exception:
                pass
            self.backup_status_label.setText(f"Backup failed: {exc}")
            self._show_error(f"Could not save the automatic project backup: {exc}")
            return False

    def _ensure_segment(self, node, operation):
        import slicer

        segment_id = operation["segment_id"]
        author = str(operation.get("author") or "")
        if author:
            self.segment_owners.setdefault(segment_id, author)
        segmentation = node.GetSegmentation()
        segment = segmentation.GetSegment(segment_id)
        if segment is None:
            segment = slicer.vtkSegment()
            segment.SetName(operation.get("segment_name") or segment_id)
            segmentation.AddSegment(segment, segment_id)
        color = str(operation.get("color_hex") or "#4A90E2").lstrip("#")
        if len(color) == 6:
            try:
                segment.SetColor(*(int(color[index : index + 2], 16) / 255.0 for index in (0, 2, 4)))
            except Exception:
                pass
        return segment

    def _apply_operations(self, operations):
        import slicer

        node = self._segmentation_node()
        if node is None:
            self._show_error("The shared segmentation node no longer exists")
            return
        for operation in operations:
            sequence = int(operation["sequence"])
            if sequence <= self.last_sequence:
                continue
            try:
                segment_id = operation["segment_id"]
                key = (node.GetID(), segment_id)
                self._applying_remote = True
                self._ensure_segment(node, operation)
                current = self._read_mask(node, segment_id)
                server_before = self.baselines.get(key)
                if server_before is None:
                    server_before = np.zeros(self.volume_shape, dtype=np.uint8)

                # Local edits can continue while an earlier operation is in flight.
                # Keep that local overlay on top of the newly ordered server state;
                # it will be sent as the next operation and therefore wins later.
                local_overlay = None
                if self.initial_sync_complete:
                    local_overlay = encode_mask_delta(server_before, current)
                server_after = apply_mask_delta(server_before, operation)
                remote_highlight = None
                if operation.get("author") != self.user_name:
                    try:
                        changed, _ = decode_mask_delta(operation)
                        z0, z1, y0, y1, x0, x1 = [
                            int(value) for value in operation["voxel_bbox"]
                        ]
                        remote_highlight = np.zeros(self.volume_shape, dtype=np.uint8)
                        remote_highlight[z0:z1, y0:y1, x0:x1] = changed.astype(np.uint8)
                    except Exception:
                        remote_highlight = None
                visible_after = (
                    apply_mask_delta(server_after, local_overlay)
                    if local_overlay is not None
                    else server_after
                )
                self.owner.update_segment_binary_labelmap_from_array(
                    visible_after, node, segment_id, self.owner.get_volume_node()
                )
                node.Modified()
                try:
                    self.owner.refresh_segmentation_display(node, segment_id)
                except Exception:
                    pass
                self.baselines[key] = server_after.copy()
                if np.any(visible_after != server_after):
                    self.dirty_segments.add(key)
                self._known_segment_ids.add(segment_id)
                self._set_segment_collaboration_tags(segment_id)
                if operation.get("author") != self.user_name:
                    if remote_highlight is not None and np.any(remote_highlight):
                        try:
                            self.owner.show_remote_change_highlight(
                                remote_highlight,
                                str(operation.get("author") or "Collaborator"),
                            )
                        except Exception:
                            pass
                    slicer.util.showStatusMessage(
                        f"Live: {operation.get('author')} updated "
                        f"{operation.get('segment_name') or segment_id}",
                        1800,
                    )
                self.last_sequence = sequence
            except Exception as exc:
                self._show_error(f"Could not apply live edit {sequence}: {exc}")
                return
            finally:
                self._applying_remote = False

    def _update_presence(self, users):
        others = [entry for entry in users if entry.get("user") != self.user_name]
        selected = self._combo_current_text(self.collaborator_combo)
        self.presence_by_user = {
            str(entry.get("user")): dict(entry)
            for entry in others
            if entry.get("user")
        }
        self.collaborator_combo.blockSignals(True)
        self.collaborator_combo.clear()
        for user in sorted(self.presence_by_user, key=str.casefold):
            self.collaborator_combo.addItem(user)
        if selected in self.presence_by_user:
            self.collaborator_combo.setCurrentText(selected)
        self.collaborator_combo.blockSignals(False)
        self.jump_to_user_button.enabled = bool(others)
        if not others:
            self.users_label.setText("Nobody else is connected")
        else:
            labels = []
            for entry in others:
                target = entry.get("active_segment_name") or entry.get("active_segment_id")
                labels.append(f"{entry['user']} — {target}" if target else entry["user"])
            self.users_label.setText("Online: " + "  •  ".join(labels))
        follow = self.follow_checkbox.checked
        follow = follow() if callable(follow) else follow
        target = self._combo_current_text(self.collaborator_combo)
        if follow and target in self.presence_by_user:
            self._apply_location(self.presence_by_user[target])
        self._update_lock_controls()
