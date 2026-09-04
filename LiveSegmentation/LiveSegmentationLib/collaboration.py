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
import hmac
import http.server
import ipaddress
import json
import os
import queue
import re
import shutil
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

try:
    from .features import (
        REVIEW_STATES,
        ROOM_ROLES,
        PendingOperationJournal,
        SessionMetrics,
        build_invitation,
        operation_overlap_count,
        operation_summary,
        parse_invitation,
        reconstruct_snapshot_operations,
        segmentation_quality_report,
        sha256_file,
        stable_user_color,
        validate_material_template,
    )
    from .version import (
        COLLABORATION_PROTOCOL_VERSION,
        MINIMUM_COMPATIBLE_PLUGIN_VERSION,
        PLUGIN_VERSION,
    )
except ImportError:  # regular-Python transport tests import this file directly
    from features import (  # type: ignore
        REVIEW_STATES,
        ROOM_ROLES,
        PendingOperationJournal,
        SessionMetrics,
        build_invitation,
        operation_overlap_count,
        operation_summary,
        parse_invitation,
        reconstruct_snapshot_operations,
        segmentation_quality_report,
        sha256_file,
        stable_user_color,
        validate_material_template,
    )
    from version import (  # type: ignore
        COLLABORATION_PROTOCOL_VERSION,
        MINIMUM_COMPATIBLE_PLUGIN_VERSION,
        PLUGIN_VERSION,
    )

LIVE_ENCODING = "zlib-packbits-v1"
SHARED_FOLDER_SCHEMA_VERSION = 3
SHARED_FOLDER_MINIMUM_PLUGIN_VERSION = MINIMUM_COMPATIBLE_PLUGIN_VERSION
MAX_PARALLEL_IO_WORKERS = 8
RECENT_FEED_LIMIT = 1000
INLINE_OPERATION_LIMIT = 8
INLINE_OPERATION_BYTES_LIMIT = 256 * 1024
SHARED_JSON_READ_RETRY_DELAYS = (0.02, 0.04, 0.08, 0.12, 0.16)
# Institutional SMB shares may need several seconds for a cold DNS, VPN,
# authentication, or Windows redirector reconnect.  Both deadlines are hard
# watchdogs around background work, so increasing them never blocks Slicer's
# GUI thread.
SHARED_FOLDER_JOIN_TIMEOUT_SECONDS = 15.0
SHARED_FOLDER_SLOW_RESPONSE_SECONDS = 10.0
SHARED_FOLDER_RESPONSE_TIMEOUT_SECONDS = 30.0
RECENT_SHARED_FOLDER_LIMIT = 8
PREFLIGHT_PARTICIPANT_TTL_SECONDS = 120.0
PREFLIGHT_TIMEOUT_SECONDS = 20.0
PRESENCE_DELAY_WARNING_SECONDS = 3.0
PRESENCE_DISPLAY_GRACE_SECONDS = 20.0
INITIAL_SYNC_OPERATION_BATCH = 6
IMMUTABLE_RECOVERY_SCAN_INTERVAL_SECONDS = 1.0
PRESENCE_DIRECTORY_SCAN_INTERVAL_SECONDS = 2.0
LARGE_VOLUME_BYTES = 256 * 1024 * 1024
LARGE_VOLUME_AUTO_BACKUP_INTERVAL_SECONDS = 60 * 60.0
AUTO_BACKUP_IDLE_SECONDS = 30.0


def new_collaboration_segment_id():
    """Return a globally unique, opaque Slicer segment identifier.

    Slicer's default ``Segment_1`` identifiers are only unique within one
    local scene. Two collaborators can therefore create different labels with
    the same internal ID at the same time. Live rooms use UUID-backed IDs for
    every segment created after joining so voxel operations can never cross
    label boundaries because of a local-ID collision.
    """
    return f"LiveSeg-{uuid.uuid4().hex}"


def _version_tuple(value):
    """Return a comparable numeric prefix for a release version."""
    numbers = re.findall(r"\d+", str(value or ""))
    return tuple(int(item) for item in numbers[:3]) or (0,)


def validate_remote_server_url(value, allow_insecure_http=False):
    """Validate the publication-facing server transport security policy."""
    normalized = normalize_server_url(value)
    parsed = urllib.parse.urlparse(normalized)
    host = str(parsed.hostname or "").strip().casefold()
    loopback = host == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
    if parsed.scheme != "https" and not loopback and not bool(allow_insecure_http):
        raise ValueError(
            "Remote collaboration servers must use HTTPS. Enable insecure HTTP only "
            "for an explicitly trusted local test server."
        )
    return normalized


def _preflight_check(check_id, status, title, detail, action=""):
    return {
        "id": str(check_id),
        "status": str(status),
        "title": str(title),
        "detail": str(detail),
        "action": str(action or ""),
    }


def finalize_preflight_report(report):
    """Normalize checks and derive one pass/warning/fail result."""
    checks = [dict(item) for item in report.get("checks") or []]
    statuses = {str(item.get("status") or "warning") for item in checks}
    status = "fail" if "fail" in statuses else "warning" if "warning" in statuses else "pass"
    return {
        "format": "live-segmentation-preflight-v1",
        **report,
        "status": status,
        "checks": checks,
    }


def capability_preflight_report(raw, transport, latency_seconds, secure_transport=True):
    """Convert a server/relay capability response into actionable checks."""
    raw = dict(raw or {})
    checks = [
        _preflight_check(
            "connection",
            "pass",
            "Connection",
            f"The collaboration endpoint answered in {float(latency_seconds) * 1000.0:.0f} ms.",
        ),
        _preflight_check(
            "transport-security",
            "pass" if secure_transport else "warning",
            "Transport security",
            "HTTPS protects credentials and collaboration data in transit."
            if secure_transport
            else "This connection is unencrypted and suitable only for a trusted LAN or local test.",
            "Use the Remote HTTPS server mode for internet collaboration."
            if not secure_transport
            else "",
        ),
    ]
    remote_protocol = int(raw.get("protocol_version", 0) or 0)
    checks.append(
        _preflight_check(
            "protocol",
            "pass" if remote_protocol == COLLABORATION_PROTOCOL_VERSION else "fail",
            "Collaboration protocol",
            f"Local protocol {COLLABORATION_PROTOCOL_VERSION}; remote protocol "
            f"{remote_protocol or 'unknown'}.",
            "Install the same current Live Segmentation release on both computers."
            if remote_protocol != COLLABORATION_PROTOCOL_VERSION
            else "",
        )
    )
    remote_version = str(raw.get("server_version") or raw.get("version") or "")
    checks.append(
        _preflight_check(
            "endpoint-version",
            "pass" if remote_version == PLUGIN_VERSION else "warning",
            "Endpoint version",
            f"Plugin {PLUGIN_VERSION}; endpoint {remote_version or 'unknown'}.",
            "Update the server and plugin to the same current release."
            if remote_version != PLUGIN_VERSION
            else "",
        )
    )
    minimum = str(raw.get("minimum_plugin_version") or "")
    compatible = not minimum or _version_tuple(PLUGIN_VERSION) >= _version_tuple(minimum)
    checks.append(
        _preflight_check(
            "plugin-version",
            "pass" if compatible else "fail",
            "Plugin version",
            f"This computer uses {PLUGIN_VERSION}; the endpoint requires {minimum or 'no stated minimum'}.",
            "Update Live Segmentation before joining." if not compatible else "",
        )
    )
    room_exists = bool(raw.get("room_exists"))
    compatible_room = bool(raw.get("room_compatible", True))
    checks.append(
        _preflight_check(
            "source-volume",
            "pass" if compatible_room else "fail",
            "Source volume",
            "The room already exists and its source-volume signature matches."
            if room_exists and compatible_room
            else "The room does not exist yet; this dataset can initialize it."
            if not room_exists
            else "The existing room belongs to a different source volume.",
            "Load exactly the same source dataset or choose another room name."
            if not compatible_room
            else "",
        )
    )
    peers = [
        item
        for item in raw.get("preflight_participants") or []
        if str(item.get("user") or "") != str(raw.get("request_user") or "")
    ]
    incompatible_peers = [
        item
        for item in peers
        if int(item.get("protocol_version", 0) or 0) != COLLABORATION_PROTOCOL_VERSION
        or str(item.get("plugin_version") or "") != PLUGIN_VERSION
        or str(item.get("volume_signature") or "") != str(raw.get("requested_volume_signature") or "")
    ]
    checks.append(
        _preflight_check(
            "peer-computer",
            "fail" if incompatible_peers else "pass" if peers else "warning",
            "Second computer",
            f"Detected {len(peers)} other compatible preflight participant(s)."
            if peers and not incompatible_peers
            else f"Detected {len(incompatible_peers)} participant(s) with a protocol or dataset mismatch."
            if incompatible_peers
            else "No second computer has run this preflight for the same room in the last two minutes.",
            "Run Check connection on the other computer, then rerun it here."
            if not peers
            else "Install the same release and load the same source data on every computer."
            if incompatible_peers
            else "",
        )
    )
    server_time = raw.get("server_time_epoch")
    if server_time is not None:
        offset = float(server_time) - time.time()
        checks.append(
            _preflight_check(
                "clock",
                "warning" if abs(offset) > 5.0 else "pass",
                "Computer clock",
                f"Clock difference is approximately {offset:+.1f} seconds.",
                "Synchronize both computers with automatic internet time."
                if abs(offset) > 5.0
                else "",
            )
        )
    authentication = str(raw.get("authentication") or "")
    if authentication:
        checks.append(
            _preflight_check(
                "authentication",
                "warning" if authentication == "open-testing" else "pass",
                "Authentication",
                f"Server authentication mode: {authentication}.",
                "Configure per-user access tokens before exposing this server to the internet."
                if authentication == "open-testing"
                else "",
            )
        )
    return finalize_preflight_report(
        {
            "transport": str(transport),
            "latency_seconds": round(float(latency_seconds), 4),
            "remote_version": raw.get("server_version") or raw.get("version"),
            "checks": checks,
        }
    )


def shared_folder_response_state(elapsed_seconds):
    """Classify silence without treating a brief SMB stall as an outage."""
    elapsed = max(0.0, float(elapsed_seconds))
    if elapsed >= SHARED_FOLDER_RESPONSE_TIMEOUT_SECONDS:
        return "offline"
    if elapsed >= SHARED_FOLDER_SLOW_RESPONSE_SECONDS:
        return "slow"
    return "live"


def decode_recent_shared_folders(value, limit=RECENT_SHARED_FOLDER_LIMIT):
    """Return a bounded, de-duplicated path history without touching the paths."""
    if value is None:
        candidates = []
    elif isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            decoded = [value]
        candidates = decoded if isinstance(decoded, list) else []
    else:
        try:
            candidates = list(value)
        except TypeError:
            candidates = []

    result = []
    seen = set()
    for candidate in candidates:
        path = str(candidate or "").strip()
        if not path:
            continue
        windows_like = path.startswith("\\\\") or bool(re.match(r"^[A-Za-z]:[\\/]", path))
        key = path.replace("/", "\\").casefold() if windows_like else path
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
        if len(result) >= max(0, int(limit)):
            break
    return result


def update_recent_shared_folders(existing, selected, limit=RECENT_SHARED_FOLDER_LIMIT):
    """Move a successfully used path to the front of the local history."""
    selected = str(selected or "").strip()
    if not selected:
        return decode_recent_shared_folders(existing, limit)
    return decode_recent_shared_folders(
        [selected, *decode_recent_shared_folders(existing, limit)], limit
    )


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
    """Read shared JSON while tolerating short Windows/SMB visibility gaps.

    Atomic rename prevents readers from observing a partially written document,
    but a network share may still deny or temporarily hide the destination while
    its directory cache catches up. These sub-second conditions are recoverable
    and must not make an otherwise healthy live room flash offline.
    """
    shared_path = Path(path)
    last_error = None
    for attempt in range(len(SHARED_JSON_READ_RETRY_DELAYS) + 1):
        try:
            with shared_path.open("r", encoding="utf-8") as source:
                return json.load(source)
        except (OSError, ValueError) as exc:
            last_error = exc
            if attempt >= len(SHARED_JSON_READ_RETRY_DELAYS):
                break
            time.sleep(SHARED_JSON_READ_RETRY_DELAYS[attempt])
    raise LiveCollaborationError(
        f"Could not read shared file {shared_path}: {last_error}"
    ) from last_error


def _is_transient_shared_read_error(message):
    """Return whether a failed live lane should be confirmed by a health probe."""
    return str(message or "").startswith("Could not read shared file ")


def _parallel_map(function, items, max_workers=MAX_PARALLEL_IO_WORKERS):
    """Run independent blocking I/O concurrently while preserving input order."""
    items = list(items)
    if len(items) < 2:
        return [function(item) for item in items]
    workers = max(1, min(int(max_workers), len(items)))
    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="LiveSegmentation-io",
    ) as executor:
        return list(executor.map(function, items))


def _atomic_temporary_path(destination):
    """Return a collision-resistant same-directory path without repeating a long name."""
    destination = Path(destination)
    token = f"{os.getpid():x}-{uuid.uuid4().hex[:12]}"
    return destination.with_name(f".tmp-{token}")


def _write_json_atomic(path, payload, durable=True):
    """Publish one complete JSON document using a same-directory rename."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = _atomic_temporary_path(destination)
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as output:
            json.dump(payload, output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            if durable:
                os.fsync(output.fileno())
        # Windows/SMB may briefly deny replacement while another client has just
        # finished reading the old JSON file. Retry that short sharing window.
        for attempt in range(20):
            try:
                os.replace(temporary, destination)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.01)
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


def _bounded_inline_operations(operations):
    """Keep a small self-contained hot feed for one-read live synchronization."""
    selected = []
    total_bytes = 0
    for operation in reversed(list(operations or [])):
        try:
            encoded_size = len(
                json.dumps(
                    operation,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        except (TypeError, ValueError):
            continue
        if selected and (
            len(selected) >= INLINE_OPERATION_LIMIT
            or total_bytes + encoded_size > INLINE_OPERATION_BYTES_LIMIT
        ):
            break
        if encoded_size > INLINE_OPERATION_BYTES_LIMIT:
            continue
        selected.append(operation)
        total_bytes += encoded_size
    return list(reversed(selected))


def _write_presence_json(path, payload):
    """Update an ephemeral presence lease without an SMB rename.

    Presence is the only high-frequency, disposable room artifact.  Several
    NAS/SMB implementations can hold ``replace(existing)`` for a minute or
    longer even while ordinary reads and writes continue to work.  That left
    the one presence worker stuck and made two active collaborators disappear
    from each other's UI.  A per-user file has exactly one writer, therefore a
    direct truncate/write is preferable here. Readers already retry short or
    incomplete JSON. Durable room metadata and segmentation operations still
    use atomic publication.
    """
    destination = Path(path)
    if not destination.parent.is_dir():
        raise LiveCollaborationError(
            f"Presence folder is unavailable: {destination.parent}"
        )
    try:
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
    except OSError as exc:
        raise LiveCollaborationError(
            f"Could not update presence in the shared folder {destination.parent}: {exc}"
        ) from exc
    return destination


def _write_shared_hot_cache(path, payload, label="shared cache"):
    """Write a rebuildable high-frequency cache without replace-over-existing.

    The immutable operation/chat files are the source of truth.  These compact
    state files only accelerate polling and can be rebuilt from that journal,
    so avoiding a slow SMB rename is more important than crash durability.
    Readers retry transient partial JSON and fall back to the immutable files.
    """
    destination = Path(path)
    try:
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
    except OSError as exc:
        raise LiveCollaborationError(
            f"Could not update {label} in the shared folder {destination.parent}: {exc}"
        ) from exc
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


class ChunkedMaskBaseline:
    """Sparse uint8 mask stored as independently allocated fixed-size chunks."""

    def __init__(self, shape, chunk_size=64):
        self.shape = tuple(int(value) for value in shape)
        if len(self.shape) != 3 or any(value <= 0 for value in self.shape):
            raise ValueError("A chunked live baseline needs a valid 3D shape")
        self.chunk_size = int(chunk_size)
        if self.chunk_size <= 0:
            raise ValueError("Chunk size must be positive")
        self.chunks = {}

    @classmethod
    def from_crop(cls, shape, crop=None, bounds=None, chunk_size=64):
        baseline = cls(shape, chunk_size=chunk_size)
        if crop is not None and bounds is not None:
            values = np.asarray(crop, dtype=np.uint8)
            baseline.set_region(bounds, values != 0, values)
        return baseline

    @property
    def allocated_bytes(self):
        return sum(int(chunk.nbytes) for chunk in self.chunks.values())

    @property
    def chunk_count(self):
        return len(self.chunks)

    def clear(self):
        self.chunks.clear()

    def _chunk_shape(self, coordinate):
        starts = [int(value) * self.chunk_size for value in coordinate]
        return tuple(
            min(self.chunk_size, self.shape[axis] - starts[axis])
            for axis in range(3)
        )

    def _intersections(self, bounds):
        z0, z1, y0, y1, x0, x1 = [int(value) for value in bounds]
        if (
            z0 < 0
            or y0 < 0
            or x0 < 0
            or z1 > self.shape[0]
            or y1 > self.shape[1]
            or x1 > self.shape[2]
            or z0 >= z1
            or y0 >= y1
            or x0 >= x1
        ):
            raise ValueError("Chunked baseline bounds are outside the volume")
        for chunk_z in range(z0 // self.chunk_size, (z1 - 1) // self.chunk_size + 1):
            for chunk_y in range(y0 // self.chunk_size, (y1 - 1) // self.chunk_size + 1):
                for chunk_x in range(x0 // self.chunk_size, (x1 - 1) // self.chunk_size + 1):
                    coordinate = (chunk_z, chunk_y, chunk_x)
                    cz0, cy0, cx0 = (
                        chunk_z * self.chunk_size,
                        chunk_y * self.chunk_size,
                        chunk_x * self.chunk_size,
                    )
                    cz1 = min(cz0 + self.chunk_size, self.shape[0])
                    cy1 = min(cy0 + self.chunk_size, self.shape[1])
                    cx1 = min(cx0 + self.chunk_size, self.shape[2])
                    iz0, iz1 = max(z0, cz0), min(z1, cz1)
                    iy0, iy1 = max(y0, cy0), min(y1, cy1)
                    ix0, ix1 = max(x0, cx0), min(x1, cx1)
                    region_slices = (
                        slice(iz0 - z0, iz1 - z0),
                        slice(iy0 - y0, iy1 - y0),
                        slice(ix0 - x0, ix1 - x0),
                    )
                    chunk_slices = (
                        slice(iz0 - cz0, iz1 - cz0),
                        slice(iy0 - cy0, iy1 - cy0),
                        slice(ix0 - cx0, ix1 - cx0),
                    )
                    yield coordinate, region_slices, chunk_slices

    def region(self, bounds):
        z0, z1, y0, y1, x0, x1 = [int(value) for value in bounds]
        result = np.zeros((z1 - z0, y1 - y0, x1 - x0), dtype=np.uint8)
        for coordinate, region_slices, chunk_slices in self._intersections(bounds):
            chunk = self.chunks.get(coordinate)
            if chunk is not None:
                result[region_slices] = chunk[chunk_slices]
        return result

    def set_region(self, bounds, changed, values):
        changed = np.asarray(changed, dtype=bool)
        values = np.asarray(values, dtype=np.uint8)
        if changed.shape != values.shape:
            raise ValueError("Changed and value crops must have identical shapes")
        z0, z1, y0, y1, x0, x1 = [int(value) for value in bounds]
        expected = (z1 - z0, y1 - y0, x1 - x0)
        if changed.shape != expected:
            raise ValueError("Chunk update shape does not match its bounds")
        for coordinate, region_slices, chunk_slices in self._intersections(bounds):
            local_changed = changed[region_slices]
            if not np.any(local_changed):
                continue
            local_values = values[region_slices]
            chunk = self.chunks.get(coordinate)
            if chunk is None:
                if not np.any(local_values[local_changed]):
                    continue
                chunk = np.zeros(self._chunk_shape(coordinate), dtype=np.uint8)
                self.chunks[coordinate] = chunk
            target = chunk[chunk_slices]
            target[local_changed] = local_values[local_changed]
            if not np.any(chunk):
                self.chunks.pop(coordinate, None)

    def replace_crop(self, crop=None, bounds=None):
        self.clear()
        if crop is not None and bounds is not None:
            values = np.asarray(crop, dtype=np.uint8)
            self.set_region(bounds, values != 0, values)

    def to_dense(self):
        result = np.zeros(self.shape, dtype=np.uint8)
        for coordinate, chunk in self.chunks.items():
            z0, y0, x0 = (value * self.chunk_size for value in coordinate)
            z1, y1, x1 = (
                z0 + chunk.shape[0],
                y0 + chunk.shape[1],
                x0 + chunk.shape[2],
            )
            result[z0:z1, y0:y1, x0:x1] = chunk
        return result


def _baseline_shape(baseline):
    return tuple(int(value) for value in baseline.shape)


def _baseline_region(baseline, bounds):
    if isinstance(baseline, ChunkedMaskBaseline):
        return baseline.region(bounds)
    z0, z1, y0, y1, x0, x1 = [int(value) for value in bounds]
    return np.asarray(baseline, dtype=np.uint8)[z0:z1, y0:y1, x0:x1].copy()


def _baseline_dense(baseline):
    if isinstance(baseline, ChunkedMaskBaseline):
        return baseline.to_dense()
    return np.asarray(baseline, dtype=np.uint8).copy()


def encode_chunked_mask_snapshot(baseline):
    """Encode one full sparse mask as a clear operation plus chunk patches.

    Every operation remains compatible with existing clients: the first
    operation has regular snapshot semantics and therefore clears the old
    label, while later operations are ordinary patches. No temporary array is
    larger than one baseline chunk, even when components are far apart in a
    very large source volume.
    """
    if not isinstance(baseline, ChunkedMaskBaseline):
        raise TypeError("Chunked snapshot encoding requires a chunked baseline")
    if not baseline.chunks:
        return [encode_mask_crop_snapshot(None, None, baseline.shape)]

    operations = []
    for index, (coordinate, chunk) in enumerate(sorted(baseline.chunks.items())):
        encoded = encode_mask_delta(
            np.zeros_like(chunk),
            chunk,
            replace=index == 0,
        )
        if encoded is None:
            continue
        z0, y0, x0 = (
            int(value) * baseline.chunk_size for value in coordinate
        )
        ez0, ez1, ey0, ey1, ex0, ex1 = encoded["voxel_bbox"]
        encoded["voxel_bbox"] = [
            z0 + ez0,
            z0 + ez1,
            y0 + ey0,
            y0 + ey1,
            x0 + ex0,
            x0 + ex1,
        ]
        encoded["volume_shape"] = [int(value) for value in baseline.shape]
        operations.append(encoded)
    if operations:
        return operations
    return [encode_mask_crop_snapshot(None, None, baseline.shape)]


def encode_mask_delta(previous, current, replace=False):
    """Encode changed boolean voxels in a compact, deterministic operation payload."""
    current = np.asarray(current, dtype=np.uint8)
    if current.ndim != 3:
        raise ValueError("A live mask must be three-dimensional")
    if replace:
        # A snapshot starts from an all-zero mask when decoded, therefore zero
        # voxels do not have to be transmitted. Older releases marked the whole
        # reference volume as changed, turning tiny insect labels into enormous
        # payloads and full-volume imports.
        changed = current != 0
    else:
        previous = np.asarray(previous, dtype=np.uint8)
        if previous.shape != current.shape:
            raise ValueError("Previous and current masks must have identical shapes")
        changed = previous != current
    bounds = _delta_bounds(changed)
    if bounds is None and replace:
        # A zero-changed-voxel snapshot still has a tiny valid payload. Snapshot
        # semantics clear the previous mask before decoding, while the empty
        # changed bitset avoids fake voxel counts and conflict overlaps.
        bounds = [0, 1, 0, 1, 0, 1]
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


def encode_metadata_update(volume_shape):
    """Encode an ordered metadata change that touches zero voxels."""
    volume_shape = [int(value) for value in volume_shape]
    if len(volume_shape) != 3 or any(value <= 0 for value in volume_shape):
        raise ValueError("A metadata update requires a valid volume shape")
    raw = np.packbits(np.asarray([False]), bitorder="little").tobytes()
    raw += np.packbits(np.asarray([False]), bitorder="little").tobytes()
    return {
        "operation_kind": "patch",
        "volume_shape": volume_shape,
        "voxel_bbox": [0, 1, 0, 1, 0, 1],
        "encoding": LIVE_ENCODING,
        "payload": base64.b64encode(zlib.compress(raw, level=6)).decode("ascii"),
    }


def _bounds_union(first, second):
    if first is None:
        return list(second) if second is not None else None
    if second is None:
        return list(first)
    return [
        min(int(first[0]), int(second[0])),
        max(int(first[1]), int(second[1])),
        min(int(first[2]), int(second[2])),
        max(int(first[3]), int(second[3])),
        min(int(first[4]), int(second[4])),
        max(int(first[5]), int(second[5])),
    ]


def encode_mask_crop_delta(
    previous,
    current_crop,
    current_bounds,
    previous_bounds,
    volume_shape,
):
    """Encode a complete cropped segment against a reference-sized baseline.

    ``current_bounds`` encloses every non-zero voxel of the current segment.
    Comparing only its union with the previous effective bounds avoids scanning
    or exporting the complete reference volume for each brush stroke.
    """
    previous = np.asarray(previous, dtype=np.uint8)
    volume_shape = tuple(int(value) for value in volume_shape)
    if previous.shape != volume_shape:
        raise ValueError("Baseline and live volume geometry do not match")
    comparison_bounds = _bounds_union(current_bounds, previous_bounds)
    if comparison_bounds is None:
        return None
    z0, z1, y0, y1, x0, x1 = comparison_bounds
    if (
        z0 < 0
        or y0 < 0
        or x0 < 0
        or z1 > volume_shape[0]
        or y1 > volume_shape[1]
        or x1 > volume_shape[2]
    ):
        raise ValueError("Segment crop exceeds the live volume geometry")
    current_region = np.zeros((z1 - z0, y1 - y0, x1 - x0), dtype=np.uint8)
    if current_crop is not None and current_bounds is not None:
        cz0, cz1, cy0, cy1, cx0, cx1 = [int(value) for value in current_bounds]
        current_region[
            cz0 - z0 : cz1 - z0,
            cy0 - y0 : cy1 - y0,
            cx0 - x0 : cx1 - x0,
        ] = np.asarray(current_crop, dtype=np.uint8)
    previous_region = previous[z0:z1, y0:y1, x0:x1]
    encoded = encode_mask_delta(previous_region, current_region, replace=False)
    if encoded is None:
        return None
    ez0, ez1, ey0, ey1, ex0, ex1 = encoded["voxel_bbox"]
    encoded["voxel_bbox"] = [
        z0 + ez0,
        z0 + ez1,
        y0 + ey0,
        y0 + ey1,
        x0 + ex0,
        x0 + ex1,
    ]
    encoded["volume_shape"] = [int(value) for value in volume_shape]
    return encoded


def encode_mask_crop_delta_after_operations(
    previous,
    current_crop,
    current_bounds,
    previous_bounds,
    volume_shape,
    pending_operations,
):
    """Encode a crop after applying locally queued operations to its baseline.

    Paint can modify the same label again while an earlier patch is still being
    written to a network share.  Comparing the new crop only with the confirmed
    server baseline would either duplicate that earlier patch or force the new
    stroke to wait.  This helper overlays the small queued operation crops onto
    the baseline first, so every later stroke can be queued immediately and in
    order without copying the complete reference volume.
    """
    volume_shape = tuple(int(value) for value in volume_shape)
    if _baseline_shape(previous) != volume_shape:
        raise ValueError("Baseline and live volume geometry do not match")

    operations = list(pending_operations or [])
    comparison_bounds = _bounds_union(current_bounds, previous_bounds)
    for operation in operations:
        operation_shape = tuple(int(value) for value in operation["volume_shape"])
        if operation_shape != volume_shape:
            raise ValueError("Pending operation belongs to a different volume geometry")
        comparison_bounds = _bounds_union(
            comparison_bounds, operation.get("voxel_bbox")
        )
    if comparison_bounds is None:
        return None

    z0, z1, y0, y1, x0, x1 = [int(value) for value in comparison_bounds]
    if (
        z0 < 0
        or y0 < 0
        or x0 < 0
        or z1 > volume_shape[0]
        or y1 > volume_shape[1]
        or x1 > volume_shape[2]
    ):
        raise ValueError("Segment crop exceeds the live volume geometry")

    current_region = np.zeros((z1 - z0, y1 - y0, x1 - x0), dtype=np.uint8)
    if current_crop is not None and current_bounds is not None:
        cz0, cz1, cy0, cy1, cx0, cx1 = [int(value) for value in current_bounds]
        current_region[
            cz0 - z0 : cz1 - z0,
            cy0 - y0 : cy1 - y0,
            cx0 - x0 : cx1 - x0,
        ] = np.asarray(current_crop, dtype=np.uint8)

    queued_region = _baseline_region(previous, comparison_bounds)
    for operation in operations:
        if operation.get("operation_kind") == "snapshot":
            queued_region.fill(0)
        oz0, oz1, oy0, oy1, ox0, ox1 = [
            int(value) for value in operation["voxel_bbox"]
        ]
        changed, values = decode_mask_delta(operation)
        target = queued_region[
            oz0 - z0 : oz1 - z0,
            oy0 - y0 : oy1 - y0,
            ox0 - x0 : ox1 - x0,
        ]
        target[changed] = values[changed]

    encoded = encode_mask_delta(queued_region, current_region, replace=False)
    if encoded is None:
        return None
    ez0, ez1, ey0, ey1, ex0, ex1 = encoded["voxel_bbox"]
    encoded["voxel_bbox"] = [
        z0 + ez0,
        z0 + ez1,
        y0 + ey0,
        y0 + ey1,
        x0 + ex0,
        x0 + ex1,
    ]
    encoded["volume_shape"] = [int(value) for value in volume_shape]
    return encoded


def encode_mask_crop_snapshot(current_crop, current_bounds, volume_shape):
    """Encode a full segment state from its effective crop, including empty labels."""
    volume_shape = tuple(int(value) for value in volume_shape)
    if current_crop is None or current_bounds is None:
        crop = np.zeros((1, 1, 1), dtype=np.uint8)
        origin = (0, 0, 0)
    else:
        crop = np.asarray(current_crop, dtype=np.uint8)
        origin = (
            int(current_bounds[0]),
            int(current_bounds[2]),
            int(current_bounds[4]),
        )
    encoded = encode_mask_delta(np.zeros_like(crop), crop, replace=True)
    ez0, ez1, ey0, ey1, ex0, ex1 = encoded["voxel_bbox"]
    encoded["voxel_bbox"] = [
        origin[0] + ez0,
        origin[0] + ez1,
        origin[1] + ey0,
        origin[1] + ey1,
        origin[2] + ex0,
        origin[2] + ex1,
    ]
    encoded["volume_shape"] = [int(value) for value in volume_shape]
    return encoded


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
        self.presence_session_id = uuid.uuid4().hex

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

    def preflight(
        self,
        room_name,
        signature,
        plugin_version=PLUGIN_VERSION,
        protocol_version=COLLABORATION_PROTOCOL_VERSION,
    ):
        started = time.monotonic()
        result = self._request(
            "POST",
            "/api/live/preflight",
            {
                "room_name": room_name,
                "volume_signature": signature,
                "plugin_version": plugin_version,
                "protocol_version": int(protocol_version),
            },
        )
        result = dict(result or {})
        result["request_user"] = self.user_name
        result["requested_volume_signature"] = str(signature)
        return capability_preflight_report(
            result,
            "remote-https-server",
            time.monotonic() - started,
            secure_transport=urllib.parse.urlparse(self.server_url).scheme == "https",
        )

    def push_operation(self, room_id, operation):
        return self._request("POST", f"/api/live/rooms/{room_id}/operations", operation)

    def operations(self, room_id, after_sequence, limit=500):
        query = urllib.parse.urlencode({"after": int(after_sequence), "limit": int(limit)})
        return self._request("GET", f"/api/live/rooms/{room_id}/operations?{query}")

    def presence(self, room_id, details):
        return self._request(
            "POST",
            f"/api/live/rooms/{room_id}/presence",
            {**(details or {}), "presence_session_id": self.presence_session_id},
        )

    def health_check(self, room_id=None):
        del room_id
        result = self._request("GET", "/health")
        if not isinstance(result, dict) or result.get("status") != "ok":
            raise LiveCollaborationError("The collaboration server returned an invalid health status")
        return result

    def leave(self, room_id):
        query = urllib.parse.urlencode(
            {"presence_session_id": self.presence_session_id}
        )
        return self._request(
            "DELETE", f"/api/live/rooms/{room_id}/presence?{query}"
        )

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
                "operation_kind": str(
                    operation.get("operation_kind") or "snapshot"
                ),
                "snapshot_group_id": group_id,
                "snapshot_group_index": index,
                "snapshot_group_count": len(segment_operations),
                "system_snapshot": True,
                "snapshot_label": str(label or "")[:200],
            }
            results.append(self.push_operation(room_id, stored))
        return {
            "id": group_id,
            "segment_count": len(
                {
                    str(operation.get("segment_id") or "")
                    for operation in segment_operations
                }
            ),
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

    def reserve_project_backup(self, room_id, interval_seconds, force=False):
        del room_id, interval_seconds, force
        return None


class SharedFolderRoomClient:
    """Ordered live-room transport backed only by a shared filesystem folder."""

    def __init__(
        self,
        shared_folder,
        user_name,
        lock_timeout_seconds=10.0,
        stale_lock_seconds=60.0,
        presence_ttl_seconds=60.0,
    ):
        self.shared_folder = normalize_shared_folder(shared_folder)
        self.user_name = str(user_name or "").strip()
        if not self.user_name:
            raise ValueError("Enter your display name")
        self.lock_timeout_seconds = float(lock_timeout_seconds)
        self.stale_lock_seconds = float(stale_lock_seconds)
        self.presence_ttl_seconds = float(presence_ttl_seconds)
        self.presence_session_id = uuid.uuid4().hex
        self.rooms_root = self.shared_folder / "LiveSegmentation" / "rooms"
        self._room_id = None
        self._room_path = None
        self._segment_owners_cache = {}
        self._artifact_queue = queue.Queue()
        self._artifact_worker = None
        self._artifact_worker_lock = threading.Lock()
        self._known_presence_users = {self.user_name}
        self._presence_paths_cache = {}
        self._last_presence_directory_scan = 0.0
        self._last_operation_recovery_scan = 0.0
        self._last_chat_recovery_scan = 0.0

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
        state_path = room_path / "sequence-state.json"
        latest = 0
        if state_path.is_file():
            try:
                state = _read_json_file(state_path)
                latest = max(0, int(state.get("latest_sequence", 0)))
            except (LiveCollaborationError, TypeError, ValueError):
                pass
        # Never trust the replaceable hot-cache file as the sole sequence
        # authority.  SMB clients may keep an old generation cached long after
        # another computer has published an immutable operation file.
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

    def _duplicate_operation_sequences(self, room_path):
        counts = {}
        operations_path = room_path / "operations"
        try:
            for path in operations_path.glob("*.json"):
                sequence = self._operation_sequence(path)
                if sequence is not None:
                    counts[sequence] = counts.get(sequence, 0) + 1
        except OSError as exc:
            raise LiveCollaborationError(
                f"Could not validate shared operation ordering: {exc}"
            ) from exc
        return sorted(sequence for sequence, count in counts.items() if count > 1)

    @staticmethod
    def _recent_feed_state(state_path, latest_key, entries_key):
        if not Path(state_path).is_file():
            return 0, []
        try:
            state = _read_json_file(state_path)
            latest = max(0, int(state.get(latest_key, 0)))
            entries = [
                item
                for item in (state.get(entries_key) or [])
                if isinstance(item, dict)
                and int(item.get("sequence", 0) or 0) > 0
                and Path(str(item.get("file") or "")).name == str(item.get("file") or "")
            ]
            return latest, entries
        except (LiveCollaborationError, TypeError, ValueError):
            return 0, []

    @contextmanager
    def _named_lock(self, room_path, lock_name, timeout_seconds=None):
        lock_name = _safe_file_component(lock_name, fallback="room-lock", max_length=40)
        lock_path = room_path / f"{lock_name}.lock"
        token = uuid.uuid4().hex
        deadline = time.monotonic() + float(
            self.lock_timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        while True:
            try:
                lock_path.mkdir()
            except FileExistsError:
                try:
                    age = time.time() - lock_path.stat().st_mtime
                except OSError:
                    age = 0.0
                if age > self.stale_lock_seconds:
                    stale_path = room_path / f"{lock_name}.lock.stale-{uuid.uuid4().hex}"
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
                    durable=False,
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

    def _sequence_lock(self, room_path):
        return self._named_lock(room_path, "sequence")

    @staticmethod
    def _operation_paths(room_path, operation):
        operation_id = str(operation.get("client_operation_id") or "")
        operation_hash = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:20]
        sequence = int(operation.get("sequence", 0))
        destination = room_path / "operations" / f"{sequence:020d}--{operation_hash}.json"
        index_path = room_path / "operation-index" / f"{operation_hash}.json"
        return operation_hash, destination, index_path

    def _persist_operation_artifacts(self, room_path, operation):
        operation_hash, destination, index_path = self._operation_paths(
            room_path, operation
        )
        if not destination.is_file():
            _write_json_atomic(destination, operation, durable=False)
        if not index_path.is_file():
            _write_json_atomic(
                index_path,
                {"sequence": int(operation["sequence"]), "file": destination.name},
                durable=False,
            )
        self._remember_segment_owner(
            room_path, operation.get("segment_id"), operation.get("author")
        )
        self._update_compact_segment_owner(
            room_path, operation.get("segment_id"), operation.get("author")
        )
        return operation_hash, destination

    def _queue_operation_artifacts(self, room_path, operation):
        """Persist hot-feed records without delaying their live publication."""
        # Give peers a short uncontended window to read the hot feed before the
        # same SMB connection starts creating archive/index metadata. The hot
        # feed is already the durable write-ahead journal during this grace time.
        self._artifact_queue.put(
            (time.monotonic() + 0.75, Path(room_path), dict(operation))
        )
        with self._artifact_worker_lock:
            if self._artifact_worker is not None and self._artifact_worker.is_alive():
                return

            def drain():
                while True:
                    try:
                        due_at, item_room_path, item_operation = (
                            self._artifact_queue.get_nowait()
                        )
                    except queue.Empty:
                        with self._artifact_worker_lock:
                            if self._artifact_queue.empty():
                                self._artifact_worker = None
                                return
                        continue
                    try:
                        remaining = float(due_at) - time.monotonic()
                        if remaining > 0:
                            time.sleep(remaining)
                        self._persist_operation_artifacts(
                            item_room_path, item_operation
                        )
                    except Exception:
                        # The complete operation remains atomically stored in the
                        # hot feed. A later writer archives it before eviction.
                        pass
                    finally:
                        self._artifact_queue.task_done()

            self._artifact_worker = threading.Thread(
                target=drain,
                name="LiveSegmentation-operation-archive",
                daemon=True,
            )
            self._artifact_worker.start()

    def preflight(
        self,
        room_name,
        signature,
        plugin_version=PLUGIN_VERSION,
        protocol_version=COLLABORATION_PROTOCOL_VERSION,
    ):
        """Probe a shared location and room without joining or creating the room."""
        started = time.monotonic()
        room_key = self._room_key(room_name)
        preflight_path = (
            self.shared_folder / "LiveSegmentation" / "preflight" / room_key
        )
        participants_path = preflight_path / "participants"
        try:
            participants_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise LiveCollaborationError(
                f"The shared folder is not writable: {self.shared_folder}: {exc}"
            ) from exc

        participant_name = (
            f"{_safe_file_component(self.user_name, 'user', 28)}--"
            f"{hashlib.sha256(self.user_name.encode('utf-8')).hexdigest()[:12]}.json"
        )
        participant_path = participants_path / participant_name
        participant = {
            "user": self.user_name,
            "plugin_version": str(plugin_version),
            "protocol_version": int(protocol_version),
            "volume_signature": str(signature),
            "updated_at": _utc_iso(),
            "updated_epoch": time.time(),
        }
        _write_json_atomic(participant_path, participant, durable=False)
        if _read_json_file(participant_path) != participant:
            raise LiveCollaborationError("Shared-folder read/write preflight was inconsistent")
        try:
            server_mtime = participant_path.stat().st_mtime
        except OSError as exc:
            raise LiveCollaborationError(f"Could not stat shared preflight file: {exc}") from exc
        clock_offset = float(server_mtime) - float(participant["updated_epoch"])

        participants = []
        estimated_server_now = time.time() + clock_offset
        try:
            candidate_paths = list(participants_path.glob("*.json"))
        except OSError as exc:
            raise LiveCollaborationError(f"Could not list shared preflight participants: {exc}") from exc
        for path in candidate_paths:
            try:
                if estimated_server_now - path.stat().st_mtime > PREFLIGHT_PARTICIPANT_TTL_SECONDS:
                    continue
                data = _read_json_file(path)
                if isinstance(data, dict):
                    participants.append(data)
            except (LiveCollaborationError, OSError, TypeError, ValueError):
                continue

        room_path = self.rooms_root / room_key
        metadata_path = room_path / "room.json"
        metadata = _read_json_file(metadata_path) if metadata_path.is_file() else None
        room_exists = metadata is not None
        duplicate_sequences = (
            self._duplicate_operation_sequences(room_path) if room_exists else []
        )
        schema_version = int(metadata.get("schema_version", 0) or 0) if metadata else 0
        room_compatible = not metadata or metadata.get("volume_signature") == str(signature)
        schema_compatible = not metadata or schema_version == SHARED_FOLDER_SCHEMA_VERSION
        minimum = str(
            metadata.get("minimum_plugin_version") or SHARED_FOLDER_MINIMUM_PLUGIN_VERSION
        ) if metadata else SHARED_FOLDER_MINIMUM_PLUGIN_VERSION
        plugin_compatible = _version_tuple(plugin_version) >= _version_tuple(minimum)
        peers = [item for item in participants if item.get("user") != self.user_name]
        incompatible_peers = [
            item
            for item in peers
            if int(item.get("protocol_version", 0) or 0) != int(protocol_version)
            or str(item.get("plugin_version") or "") != str(plugin_version)
            or str(item.get("volume_signature") or "") != str(signature)
        ]
        checks = [
            _preflight_check(
                "connection",
                "pass",
                "Shared-folder connection",
                f"Atomic read/write succeeded in {((time.monotonic() - started) * 1000.0):.0f} ms.",
            ),
            _preflight_check(
                "permissions",
                "pass",
                "Permissions",
                "This computer can create, replace, read, and list collaboration files.",
            ),
            _preflight_check(
                "room-format",
                "pass"
                if schema_compatible and plugin_compatible and not duplicate_sequences
                else "fail",
                "Room format and plugin",
                f"Plugin {plugin_version}; room schema {schema_version if room_exists else 'new'}; "
                f"minimum plugin {minimum}."
                + (
                    " Duplicate operation sequence(s): "
                    + ", ".join(str(item) for item in duplicate_sequences)
                    + "."
                    if duplicate_sequences
                    else ""
                ),
                "This room was damaged by a stale-cache collision in an older release. "
                "Create a new room with version 0.14.0 or newer."
                if duplicate_sequences
                else "Create a new room with Live Segmentation 0.14.0 on every computer. "
                "Older rooms do not provide collision-safe global label identities."
                if room_exists and schema_version < SHARED_FOLDER_SCHEMA_VERSION
                else "Install the same current Live Segmentation release on every computer."
                if not schema_compatible or not plugin_compatible
                else "",
            ),
            _preflight_check(
                "source-volume",
                "pass" if room_compatible else "fail",
                "Source volume",
                "The existing room uses the same source-volume signature."
                if room_exists and room_compatible
                else "The room does not exist yet; this dataset can initialize it."
                if not room_exists
                else "The existing room belongs to a different source volume.",
                "Load exactly the same dataset or use another room name."
                if not room_compatible
                else "",
            ),
            _preflight_check(
                "peer-computer",
                "fail" if incompatible_peers else "pass" if peers else "warning",
                "Second computer",
                f"Detected {len(peers)} other compatible preflight participant(s)."
                if peers and not incompatible_peers
                else f"Detected {len(incompatible_peers)} participant(s) with a protocol or dataset mismatch."
                if incompatible_peers
                else "No second computer has run this check for the same room in the last two minutes.",
                "Run Check connection on the other computer, then rerun it here."
                if not peers
                else "Use the same release and source data on all computers."
                if incompatible_peers
                else "",
            ),
            _preflight_check(
                "clock",
                "warning" if abs(clock_offset) > 5.0 else "pass",
                "Computer/share clock",
                f"Estimated clock difference is {clock_offset:+.1f} seconds.",
                "Enable automatic clock synchronization on both computers."
                if abs(clock_offset) > 5.0
                else "",
            ),
        ]
        return finalize_preflight_report(
            {
                "transport": "shared-folder",
                "latency_seconds": round(time.monotonic() - started, 4),
                "remote_version": None,
                "checks": checks,
            }
        )

    def join(self, room_name, signature):
        room_key = self._room_key(room_name)
        room_path = self.rooms_root / room_key
        try:
            room_path.mkdir(parents=True, exist_ok=True)
            # Each mkdir is a separate SMB round trip. Creating/checking these
            # independent directories serially made a healthy institutional NAS
            # look frozen for several seconds during every join.
            directory_names = (
                "operations",
                "operation-index",
                "presence",
                "chat",
                "chat-index",
                "locks",
                "segment-index",
                "backups",
                "snapshots",
                "operation-archives",
                "conflicts",
                "roles",
                "reviews",
                "access-requests",
                "audit",
            )
            _parallel_map(
                lambda name: (room_path / name).mkdir(exist_ok=True),
                directory_names,
                max_workers=MAX_PARALLEL_IO_WORKERS,
            )
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
                    "minimum_plugin_version": SHARED_FOLDER_MINIMUM_PLUGIN_VERSION,
                    "capabilities": [
                        "segment-deletion-tombstone-v1",
                        "global-segment-identity-v1",
                        "explicit-segment-metadata-v1",
                    ],
                    "name": str(room_name).strip(),
                    "room_id": room_key,
                    "volume_signature": str(signature),
                    "created_by": self.user_name,
                    "created_at": _utc_iso(),
                }
                _write_json_atomic(metadata_path, metadata)
                created = True

        schema_version = int(metadata.get("schema_version", 0))
        if schema_version != SHARED_FOLDER_SCHEMA_VERSION:
            minimum_version = str(
                metadata.get("minimum_plugin_version") or "a newer version"
            )
            if schema_version < SHARED_FOLDER_SCHEMA_VERSION:
                raise LiveCollaborationError(
                    "This room uses an older label-identity format that cannot safely "
                    "distinguish labels created simultaneously on different computers. "
                    "Create a new room with Live Segmentation 0.14.0 on every computer."
                )
            raise LiveCollaborationError(
                "This room uses a newer Live Segmentation format and requires "
                f"plugin version {minimum_version} or newer. Update the plugin on "
                "every computer before joining this room."
            )
        duplicate_sequences = self._duplicate_operation_sequences(room_path)
        if duplicate_sequences:
            raise LiveCollaborationError(
                "This room contains duplicate operation sequence(s) "
                + ", ".join(str(item) for item in duplicate_sequences)
                + " from an older stale-cache collision. Its ordering is ambiguous. "
                "Create a new room with Live Segmentation 0.14.0 or newer."
            )
        self._room_id = room_key
        self._room_path = room_path
        self._segment_owners_cache = {}
        self._known_presence_users = {
            value
            for value in (
                self.user_name,
                str(metadata.get("created_by") or "").strip(),
            )
            if value
        }
        self._presence_paths_cache = {}
        self._last_presence_directory_scan = 0.0
        self._last_operation_recovery_scan = 0.0
        self._last_chat_recovery_scan = 0.0
        self._register_presence_user(room_path, self.user_name)
        self._append_audit(room_path, "room.join", {"created": created})
        return {
            "id": room_key,
            "name": metadata.get("name") or str(room_name).strip(),
            "created": created,
            "created_by": metadata.get("created_by"),
            "latest_sequence": self._latest_sequence(room_path),
            "presence": self._read_presence(room_path),
        }

    def _analyze_operation_conflicts(
        self,
        room_id,
        room_path,
        operation,
        stored,
        sequence,
        base_sequence,
    ):
        conflicts = []
        segment_id = str(operation.get("segment_id") or "")
        if base_sequence > 0 and (
            operation.get("operation_kind") != "snapshot"
            or operation.get("segment_deleted")
        ):
            for previous in self.operations(room_id, base_sequence, limit=5000):
                previous_sequence = int(previous.get("sequence", 0))
                if previous_sequence <= base_sequence or previous_sequence >= sequence:
                    continue
                if previous.get("author") == self.user_name or previous.get(
                    "system_snapshot"
                ):
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
                            "other_sequence": previous_sequence,
                            "overlap_voxels": overlap,
                        }
                    )
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
                durable=False,
            )
        self._append_audit_async(
            room_path,
            "segmentation.operation",
            {
                "sequence": sequence,
                "segment_id": stored.get("segment_id"),
                "changed_voxels": stored.get("changed_voxels"),
                "conflict_count": len(conflicts),
            },
        )
        return conflicts

    def push_operation(self, room_id, operation, defer_conflicts=False):
        room_path = self._require_room(room_id)
        operation_id = str(operation.get("client_operation_id") or "").strip()
        if not operation_id:
            raise LiveCollaborationError("Live operation is missing its client ID")
        operation_hash = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:20]
        operations_path = room_path / "operations"
        operation_index_path = room_path / "operation-index" / f"{operation_hash}.json"
        state_path = room_path / "sequence-state.json"
        with self._sequence_lock(room_path):
            state = {}
            if state_path.is_file():
                try:
                    state = _read_json_file(state_path)
                except LiveCollaborationError:
                    state = {}
            existing = None
            for recent_operation in state.get("inline_operations") or []:
                if str(recent_operation.get("client_operation_id") or "") == operation_id:
                    return {
                        "sequence": int(recent_operation.get("sequence", 0)),
                        "duplicate": True,
                    }
            if operation_index_path.is_file():
                try:
                    indexed = _read_json_file(operation_index_path)
                    indexed_name = str(indexed.get("file") or "")
                    if Path(indexed_name).name == indexed_name:
                        indexed_path = operations_path / indexed_name
                        if indexed_path.is_file():
                            existing = indexed_path
                except LiveCollaborationError:
                    pass
            if existing is None:
                legacy_matches = sorted(operations_path.glob(f"*--{operation_hash}.json"))
                existing = legacy_matches[0] if legacy_matches else None
            if existing is not None:
                sequence = self._operation_sequence(existing)
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
            # Cross-check the cache against immutable files on every write.
            # This prevents two computers from allocating the same sequence
            # when one SMB redirector still serves an older state-file cache.
            latest_sequence = self._latest_sequence(room_path)
            sequence = latest_sequence + 1
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
            recent_operations = list(state.get("recent_operations") or [])
            known_recent_files = {
                str(item.get("file") or "")
                for item in recent_operations
                if isinstance(item, dict)
            }
            try:
                for existing_path in operations_path.glob("*.json"):
                    existing_sequence = self._operation_sequence(existing_path)
                    if (
                        existing_sequence is not None
                        and existing_path.name not in known_recent_files
                    ):
                        recent_operations.append(
                            {
                                "sequence": existing_sequence,
                                "file": existing_path.name,
                                "operation_hash": existing_path.stem.split("--", 1)[-1],
                            }
                        )
            except OSError as exc:
                raise LiveCollaborationError(
                    f"Could not reconcile shared operations: {exc}"
                ) from exc
            recent_operations = [
                item for item in recent_operations if int(item.get("sequence", 0)) != sequence
            ]
            recent_operations.append(
                {
                    "sequence": sequence,
                    "file": destination.name,
                    "operation_hash": operation_hash,
                }
            )
            recent_operations = recent_operations[-RECENT_FEED_LIMIT:]
            previous_inline = list(state.get("inline_operations") or [])
            inline_operations = _bounded_inline_operations(previous_inline + [stored])
            retained_sequences = {
                int(item.get("sequence", 0)) for item in inline_operations
            }
            # The hot feed is also a write-ahead journal. Before an old inline
            # record is evicted, make sure its append-only archive exists.
            for previous_operation in previous_inline:
                previous_sequence = int(previous_operation.get("sequence", 0))
                if previous_sequence in retained_sequences:
                    continue
                _, previous_destination, _ = self._operation_paths(
                    room_path, previous_operation
                )
                if not previous_destination.is_file():
                    _write_json_atomic(
                        previous_destination, previous_operation, durable=False
                    )
            # Publish the immutable operation before the replaceable polling
            # cache.  A peer can therefore discover it even while its SMB
            # client still shows an older sequence-state.json generation.
            _write_json_atomic(destination, stored, durable=False)
            _write_shared_hot_cache(
                state_path,
                {
                    "latest_sequence": sequence,
                    "updated_at": _utc_iso(),
                    "recent_operations": recent_operations,
                    "inline_operations": inline_operations,
                },
                label="live-operation cache",
            )
        # The immutable operation above is the source of truth. Retry index and
        # owner metadata are derived in one background lane; the compact state
        # file remains only a replaceable polling accelerator.
        self._queue_operation_artifacts(room_path, stored)
        if defer_conflicts:
            def analyze_later():
                time.sleep(0.75)
                try:
                    self._analyze_operation_conflicts(
                        room_id,
                        room_path,
                        operation,
                        stored,
                        sequence,
                        base_sequence,
                    )
                except Exception:
                    pass

            threading.Thread(
                target=analyze_later,
                name="LiveSegmentation-conflict-analysis",
                daemon=True,
            ).start()
            return {"sequence": sequence, "duplicate": False}

        conflicts = self._analyze_operation_conflicts(
            room_id,
            room_path,
            operation,
            stored,
            sequence,
            base_sequence,
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
        operations_path = room_path / "operations"
        if not operations_path.is_dir():
            raise LiveCollaborationError(
                f"Shared operations folder is unavailable: {operations_path}"
            )
        state_path = room_path / "sequence-state.json"
        try:
            state = _read_json_file(state_path) if state_path.is_file() else {}
        except LiveCollaborationError:
            state = {}
        try:
            latest = max(0, int(state.get("latest_sequence", 0)))
        except (TypeError, ValueError):
            latest = 0
        immutable_entries = []
        if latest <= after_sequence:
            monotonic_now = time.monotonic()
            if (
                monotonic_now - self._last_operation_recovery_scan
                < IMMUTABLE_RECOVERY_SCAN_INTERVAL_SECONDS
            ):
                return []
            self._last_operation_recovery_scan = monotonic_now
            try:
                immutable_entries = [
                    (sequence, path)
                    for path in operations_path.glob("*.json")
                    if (sequence := self._operation_sequence(path)) is not None
                ]
            except OSError as exc:
                raise LiveCollaborationError(
                    f"Could not list shared operations: {exc}"
                ) from exc
            if immutable_entries:
                latest = max(latest, max(sequence for sequence, _ in immutable_entries))
        if latest <= after_sequence:
            return []
        inline_by_sequence = {
            int(item.get("sequence", 0)): dict(item)
            for item in state.get("inline_operations") or []
            if isinstance(item, dict) and int(item.get("sequence", 0) or 0) > 0
        }
        expected_sequences = list(
            range(after_sequence + 1, min(latest, after_sequence + limit) + 1)
        )
        if expected_sequences and all(
            sequence in inline_by_sequence for sequence in expected_sequences
        ):
            operations = [inline_by_sequence[sequence] for sequence in expected_sequences]
            for operation in operations:
                segment_id = str(operation.get("segment_id") or "")
                author = str(operation.get("author") or "")
                if segment_id and author:
                    self._segment_owners_cache.setdefault(segment_id, author)
            return operations

        recent = [
            item
            for item in state.get("recent_operations") or []
            if isinstance(item, dict)
            and int(item.get("sequence", 0) or 0) > 0
            and Path(str(item.get("file") or "")).name == str(item.get("file") or "")
        ]
        recent = sorted(recent, key=lambda item: int(item["sequence"]))
        recent_covers_request = bool(recent) and (
            after_sequence >= int(recent[0]["sequence"]) - 1
        )
        if recent_covers_request:
            selected = [
                (int(item["sequence"]), operations_path / str(item["file"]))
                for item in recent
                if int(item["sequence"]) > after_sequence
            ]
            if any(not path.is_file() for _, path in selected):
                selected = []
                recent_covers_request = False
        if not recent_covers_request:
            if not immutable_entries:
                try:
                    immutable_entries = [
                        (sequence, path)
                        for path in operations_path.glob("*.json")
                        if (sequence := self._operation_sequence(path)) is not None
                    ]
                except OSError as exc:
                    raise LiveCollaborationError(
                        f"Could not list shared operations: {exc}"
                    ) from exc
            selected.extend(
                (sequence, path)
                for sequence, path in immutable_entries
                if sequence > after_sequence
            )
        selected = sorted(selected)[:limit]

        def read_operation(item):
            sequence, path = item
            operation = _read_json_file(path)
            operation["sequence"] = sequence
            return operation

        operations = _parallel_map(read_operation, selected)
        operations_by_sequence = {
            int(operation["sequence"]): operation for operation in operations
        }
        for sequence, operation in inline_by_sequence.items():
            if after_sequence < sequence <= after_sequence + limit:
                operations_by_sequence[sequence] = operation
        operations = []
        start_sequence = after_sequence + 1
        if start_sequence not in operations_by_sequence:
            snapshots = [
                sequence
                for sequence, operation in operations_by_sequence.items()
                if operation.get("operation_kind") == "snapshot"
            ]
            if snapshots:
                start_sequence = min(snapshots)
        for sequence in range(start_sequence, min(latest, after_sequence + limit) + 1):
            operation = operations_by_sequence.get(sequence)
            if operation is None:
                break
            operations.append(operation)
        for operation in operations:
            segment_id = str(operation.get("segment_id") or "")
            author = str(operation.get("author") or "")
            if segment_id and author:
                self._segment_owners_cache.setdefault(segment_id, author)
            if author:
                self._known_presence_users.add(author)
        return operations

    def _presence_path(self, room_path):
        return self._presence_path_for_user(room_path, self.user_name)

    @staticmethod
    def _presence_path_for_user(room_path, user_name):
        user_name = str(user_name or "").strip()
        readable = _safe_file_component(user_name, fallback="user", max_length=28)
        digest = hashlib.sha256(user_name.encode("utf-8")).hexdigest()[:12]
        return room_path / "presence" / f"{readable}--{digest}.json"

    def _register_presence_user(self, room_path, user_name):
        """Keep a compact participant index so SMB directory caches cannot hide peers."""
        user_name = str(user_name or "").strip()
        if not user_name:
            return
        registry_path = room_path / "participants.json"
        with self._named_lock(room_path, "participants", timeout_seconds=3.0):
            registry = {}
            if registry_path.is_file():
                try:
                    registry = _read_json_file(registry_path)
                except LiveCollaborationError:
                    registry = {}
            users = {
                str(item).strip()
                for item in registry.get("users") or []
                if str(item).strip()
            }
            users.add(user_name)
            _write_json_atomic(
                registry_path,
                {"users": sorted(users, key=str.casefold), "updated_at": _utc_iso()},
                durable=False,
            )
        self._known_presence_users.update(users)

    def _read_presence_registry(self, room_path):
        registry_path = room_path / "participants.json"
        if not registry_path.is_file():
            return
        try:
            registry = _read_json_file(registry_path)
        except LiveCollaborationError:
            return
        self._known_presence_users.update(
            str(item).strip()
            for item in registry.get("users") or []
            if str(item).strip()
        )

    def _read_presence(self, room_path):
        now = time.time()
        users = []
        presence_path = room_path / "presence"
        if not presence_path.is_dir():
            raise LiveCollaborationError(
                f"Shared presence folder is unavailable: {presence_path}"
            )
        self._read_presence_registry(room_path)
        paths_by_name = {
            str(path): path
            for path in self._presence_paths_cache.values()
        }
        for user_name in self._known_presence_users:
            path = self._presence_path_for_user(room_path, user_name)
            paths_by_name[str(path)] = path
        monotonic_now = time.monotonic()
        if (
            monotonic_now - self._last_presence_directory_scan
            >= PRESENCE_DIRECTORY_SCAN_INTERVAL_SECONDS
        ):
            self._last_presence_directory_scan = monotonic_now
            try:
                for path in presence_path.glob("*.json"):
                    paths_by_name[str(path)] = path
            except OSError as exc:
                raise LiveCollaborationError(
                    f"Could not list shared presence: {exc}"
                ) from exc
        def read_user(path):
            try:
                if not path.is_file():
                    return None
                data = _read_json_file(path)
                if now - float(data.get("last_seen_epoch", 0.0)) <= self.presence_ttl_seconds:
                    data.pop("last_seen_epoch", None)
                    return data
            except (LiveCollaborationError, TypeError, ValueError):
                pass
            return None

        users.extend(
            item
            for item in _parallel_map(read_user, paths_by_name.values())
            if item is not None
        )
        for item in users:
            user_name = str(item.get("user") or "").strip()
            if user_name:
                self._known_presence_users.add(user_name)
                self._presence_paths_cache[user_name] = self._presence_path_for_user(
                    room_path, user_name
                )
        return sorted(users, key=lambda item: str(item.get("user", "")).casefold())

    def presence(self, room_id, details):
        room_path = self._require_room(room_id)
        data = {
            "user": self.user_name,
            "last_seen": _utc_iso(),
            "last_seen_epoch": time.time(),
            "presence_session_id": str(
                (details or {}).get("presence_session_id")
                or self.presence_session_id
            ),
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

    def leave(self, room_id, presence_session_id=None):
        room_path = self._require_room(room_id)
        try:
            self._append_audit(room_path, "room.leave")
        except Exception:
            pass
        try:
            presence_path = self._presence_path(room_path)
            current = _read_json_file(presence_path) if presence_path.is_file() else {}
            expected_session = str(presence_session_id or self.presence_session_id)
            current_session = str(current.get("presence_session_id") or "")
            # A delayed cleanup from an earlier connection must not delete the
            # heartbeat written by a newer session of the same user.
            if not current_session or current_session == expected_session:
                presence_path.unlink(missing_ok=True)
        except (OSError, LiveCollaborationError):
            pass
        self._room_id = None
        self._room_path = None
        self._segment_owners_cache = {}
        return {"left": True}

    def _latest_message_sequence(self, room_path):
        latest, _ = self._recent_feed_state(
            room_path / "chat-state.json", "latest_sequence", "recent_messages"
        )
        # As with segmentation operations, immutable chat files outrank a
        # possibly stale SMB cache generation.
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
        message_index_path = room_path / "chat-index" / f"{message_hash}.json"
        # Chat has its own short critical section. It must never wait for a large
        # segmentation operation or block one in return.
        with self._named_lock(room_path, "chat-sequence", timeout_seconds=3.0):
            existing = None
            if message_index_path.is_file():
                try:
                    indexed = _read_json_file(message_index_path)
                    indexed_name = str(indexed.get("file") or "")
                    if Path(indexed_name).name == indexed_name:
                        indexed_path = chat_path / indexed_name
                        if indexed_path.is_file():
                            existing = indexed_path
                except LiveCollaborationError:
                    pass
            if existing is None:
                legacy_matches = sorted(chat_path.glob(f"*--{message_hash}.json"))
                existing = legacy_matches[0] if legacy_matches else None
            if existing is not None:
                message = _read_json_file(existing)
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
            destination = chat_path / f"{sequence:020d}--{message_hash}.json"
            _, recent_messages = self._recent_feed_state(
                room_path / "chat-state.json", "latest_sequence", "recent_messages"
            )
            recent_messages = [
                item for item in recent_messages if int(item.get("sequence", 0)) != sequence
            ]
            recent_messages.append({"sequence": sequence, "file": destination.name})
            recent_messages = recent_messages[-RECENT_FEED_LIMIT:]
            _write_json_atomic(destination, message, durable=False)
            _write_json_atomic(
                message_index_path,
                {"sequence": sequence, "file": destination.name},
                durable=False,
            )
            _write_shared_hot_cache(
                room_path / "chat-state.json",
                {
                    "latest_sequence": sequence,
                    "updated_at": _utc_iso(),
                    "recent_messages": recent_messages,
                },
                label="chat cache",
            )
        self._append_audit_async(
            room_path,
            "chat.message",
            {"message_sequence": sequence, "has_anchor": bool(message.get("anchor"))},
        )
        return {**message, "duplicate": False}

    def chat_messages(self, room_id, after_sequence, limit=500):
        room_path = self._require_room(room_id)
        after_sequence = int(after_sequence)
        selected = []
        chat_path = room_path / "chat"
        if not chat_path.is_dir():
            raise LiveCollaborationError(
                f"Shared chat folder is unavailable: {chat_path}"
            )
        latest, recent = self._recent_feed_state(
            room_path / "chat-state.json", "latest_sequence", "recent_messages"
        )
        immutable_entries = []
        if latest <= after_sequence:
            monotonic_now = time.monotonic()
            if (
                monotonic_now - self._last_chat_recovery_scan
                < IMMUTABLE_RECOVERY_SCAN_INTERVAL_SECONDS
            ):
                return []
            self._last_chat_recovery_scan = monotonic_now
            try:
                immutable_entries = [
                    (sequence, path)
                    for path in chat_path.glob("*.json")
                    if (sequence := self._message_sequence(path)) is not None
                ]
            except OSError as exc:
                raise LiveCollaborationError(
                    f"Could not list shared chat messages: {exc}"
                ) from exc
            if immutable_entries:
                latest = max(latest, max(sequence for sequence, _ in immutable_entries))
        if latest <= after_sequence:
            return []
        recent = sorted(recent, key=lambda item: int(item["sequence"]))
        recent_covers_request = bool(recent) and (
            after_sequence >= int(recent[0]["sequence"]) - 1
        )
        if recent_covers_request:
            selected = [
                (int(item["sequence"]), chat_path / str(item["file"]))
                for item in recent
                if int(item["sequence"]) > after_sequence
            ]
            if any(not path.is_file() for _, path in selected):
                selected = []
                recent_covers_request = False
        if not recent_covers_request:
            if not immutable_entries:
                try:
                    immutable_entries = [
                        (sequence, path)
                        for path in chat_path.glob("*.json")
                        if (sequence := self._message_sequence(path)) is not None
                    ]
                except OSError as exc:
                    raise LiveCollaborationError(
                        f"Could not list shared chat messages: {exc}"
                    ) from exc
            selected.extend(
                (sequence, path)
                for sequence, path in immutable_entries
                if sequence > after_sequence
            )
        selected = sorted(selected)[: max(1, min(int(limit), 5000))]

        def read_message(item):
            sequence, path = item
            message = _read_json_file(path)
            message["sequence"] = sequence
            return message

        messages = _parallel_map(read_message, selected)
        self._known_presence_users.update(
            str(item.get("author") or "").strip()
            for item in messages
            if str(item.get("author") or "").strip()
        )
        return messages

    @staticmethod
    def _segment_lock_path(room_path, segment_id):
        readable = _safe_file_component(segment_id, fallback="segment", max_length=28)
        digest = hashlib.sha256(str(segment_id).encode("utf-8")).hexdigest()[:12]
        return room_path / "locks" / f"{readable}--{digest}.json"

    @staticmethod
    def _segment_owner_path(room_path, segment_id):
        readable = _safe_file_component(segment_id, fallback="segment", max_length=28)
        digest = hashlib.sha256(str(segment_id).encode("utf-8")).hexdigest()[:12]
        return room_path / "segment-index" / f"{readable}--{digest}.json"

    def _remember_segment_owner(self, room_path, segment_id, owner):
        segment_id = str(segment_id or "").strip()
        owner = str(owner or "").strip()
        if not segment_id or not owner or segment_id in self._segment_owners_cache:
            return
        path = self._segment_owner_path(room_path, segment_id)
        if path.is_file():
            try:
                existing = _read_json_file(path)
                existing_owner = str(existing.get("owner") or "").strip()
                if existing_owner:
                    self._segment_owners_cache[segment_id] = existing_owner
                    return
            except LiveCollaborationError:
                pass
        _write_json_atomic(
            path,
            {
                "segment_id": segment_id,
                "owner": owner,
                "indexed_at": _utc_iso(),
            },
            durable=False,
        )
        self._segment_owners_cache[segment_id] = owner

    def _update_compact_segment_owner(self, room_path, segment_id, owner):
        """Update the single-read owner index while the caller holds the sequence lock."""
        segment_id = str(segment_id or "").strip()
        owner = str(owner or "").strip()
        if not segment_id or not owner:
            return
        state_path = room_path / "segment-index-state.json"
        state = {}
        if state_path.is_file():
            try:
                state = _read_json_file(state_path)
            except LiveCollaborationError:
                state = {}
        owners = dict(state.get("owners") or {})
        if str(owners.get(segment_id) or "") == owner:
            self._segment_owners_cache.setdefault(segment_id, owner)
            return
        owners.setdefault(segment_id, owner)
        _write_json_atomic(
            state_path,
            {"owners": owners, "updated_at": _utc_iso()},
            durable=False,
        )

    def _segment_creators(self, room_path):
        creators = dict(self._segment_owners_cache)
        index_path = room_path / "segment-index"
        operations_path = room_path / "operations"
        if not index_path.is_dir() or not operations_path.is_dir():
            raise LiveCollaborationError(
                f"Shared label metadata is unavailable in {room_path}"
            )
        hot_state_path = room_path / "sequence-state.json"
        if hot_state_path.is_file():
            try:
                hot_state = _read_json_file(hot_state_path)
                for operation in hot_state.get("inline_operations") or []:
                    segment_id = str(operation.get("segment_id") or "")
                    owner = str(operation.get("author") or "")
                    if segment_id and owner:
                        creators.setdefault(segment_id, owner)
            except LiveCollaborationError:
                pass
        compact_index_path = room_path / "segment-index-state.json"
        if compact_index_path.is_file():
            try:
                compact = _read_json_file(compact_index_path)
                for segment_id, owner in (compact.get("owners") or {}).items():
                    segment_id = str(segment_id or "")
                    owner = str(owner or "")
                    if segment_id and owner:
                        creators.setdefault(segment_id, owner)
                self._segment_owners_cache = creators
                return creators
            except LiveCollaborationError:
                pass
        complete_path = index_path / ".complete"
        try:
            index_records = _parallel_map(_read_json_file, index_path.glob("*.json"))
            for data in index_records:
                segment_id = str(data.get("segment_id") or "")
                owner = str(data.get("owner") or "")
                if segment_id and owner:
                    creators.setdefault(segment_id, owner)
        except OSError as exc:
            raise LiveCollaborationError(f"Could not inspect segment ownership: {exc}") from exc
        if complete_path.is_file():
            self._segment_owners_cache = creators
            _write_json_atomic(
                compact_index_path,
                {"owners": creators, "updated_at": _utc_iso()},
                durable=False,
            )
            return creators

        # One-time migration for rooms created before the compact owner index
        # existed. Subsequent lock polling reads only one tiny file per label.
        selected = []
        try:
            for path in operations_path.glob("*.json"):
                sequence = self._operation_sequence(path)
                if sequence is not None:
                    selected.append((sequence, path))
        except OSError as exc:
            raise LiveCollaborationError(f"Could not inspect segment ownership: {exc}") from exc
        migration_records = _parallel_map(
            lambda item: _read_json_file(item[1]), sorted(selected)
        )
        for operation in migration_records:
            segment_id = str(operation.get("segment_id") or "")
            if segment_id and segment_id not in creators:
                owner = str(operation.get("author") or "")
                if owner:
                    creators[segment_id] = owner
                    self._remember_segment_owner(room_path, segment_id, owner)
        try:
            complete_path.touch(exist_ok=True)
        except OSError:
            pass
        self._segment_owners_cache = creators
        _write_json_atomic(
            compact_index_path,
            {"owners": creators, "updated_at": _utc_iso()},
            durable=False,
        )
        return creators

    def segment_locks(self, room_id):
        room_path = self._require_room(room_id)
        creators = self._segment_creators(room_path)

        def read_lock(item):
            segment_id, owner = item
            lock_path = self._segment_lock_path(room_path, segment_id)
            lock_data = _read_json_file(lock_path) if lock_path.is_file() else {}
            expires_epoch = float(lock_data.get("expires_epoch", 0.0) or 0.0)
            locked = bool(lock_data.get("locked", False))
            if locked and expires_epoch and time.time() >= expires_epoch:
                locked = False
            return {
                "segment_id": segment_id,
                "owner": str(lock_data.get("owner") or owner),
                "locked": locked,
                "updated_at": lock_data.get("updated_at"),
                **(
                    {"updated_by": lock_data.get("updated_by")}
                    if lock_data.get("updated_by")
                    else {}
                ),
                **(
                    {"expires_at": lock_data.get("expires_at")}
                    if locked and lock_data.get("expires_at")
                    else {}
                ),
            }

        result = _parallel_map(read_lock, creators.items())
        return sorted(result, key=lambda item: item["segment_id"].casefold())

    def set_segment_lock(self, room_id, segment_id, locked, expires_minutes=0):
        room_path = self._require_room(room_id)
        segment_id = str(segment_id or "").strip()
        if not segment_id:
            raise LiveCollaborationError("Select a label first")
        owner = self._segment_owners_cache.get(segment_id)
        if not owner:
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
        _write_json_atomic(
            self._segment_lock_path(room_path, segment_id), state, durable=False
        )
        self._append_audit_async(
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
        try:
            _write_json_atomic(
                room_path / "audit" / f"{stamp:020d}--{record['id'][:12]}.json",
                record,
                durable=False,
            )
        except LiveCollaborationError:
            # Audit is valuable, but it must never make a successful live edit,
            # chat message, or lock update look as though the room disconnected.
            return None
        return record

    def _append_audit_async(self, room_path, event, details=None):
        """Keep non-critical audit I/O out of latency-sensitive live actions."""

        def write_record():
            try:
                self._append_audit(room_path, event, details)
            except Exception:
                pass

        threading.Thread(
            target=write_record,
            name="LiveSegmentation-audit",
            daemon=True,
        ).start()

    def audit_events(self, room_id, limit=500):
        room_path = self._require_room(room_id)
        try:
            paths = sorted(room_path.joinpath("audit").glob("*.json"), reverse=True)
        except OSError as exc:
            raise LiveCollaborationError(f"Could not list audit events: {exc}") from exc
        return _parallel_map(
            _read_json_file, paths[: max(1, min(int(limit), 5000))]
        )

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
        records = _parallel_map(
            _read_json_file, room_path.joinpath("roles").glob("*.json")
        )
        for record in records:
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
        result = _parallel_map(
            _read_json_file, room_path.joinpath("reviews").glob("*.json")
        )
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
        records = _parallel_map(
            _read_json_file, room_path.joinpath("access-requests").glob("*.json")
        )
        for record in records:
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
        def read_active(path):
            sequence = self._operation_sequence(path)
            return (sequence, _read_json_file(path)) if sequence is not None else None

        for item in _parallel_map(read_active, active_paths):
            if item is not None:
                records[item[0]] = item[1]

        def read_archive(archive):
            archived = []
            try:
                with zipfile.ZipFile(archive, "r") as bundle:
                    for name in bundle.namelist():
                        sequence = self._operation_sequence(Path(name))
                        if sequence is None:
                            continue
                        archived.append(
                            (sequence, json.loads(bundle.read(name).decode("utf-8")))
                        )
            except (OSError, ValueError, zipfile.BadZipFile) as exc:
                raise LiveCollaborationError(
                    f"Could not read history archive {archive.name}: {exc}"
                ) from exc
            return archived

        for archived in _parallel_map(read_archive, archive_paths):
            for sequence, operation in archived:
                records.setdefault(sequence, operation)
        state_path = room_path / "sequence-state.json"
        if state_path.is_file():
            try:
                state = _read_json_file(state_path)
                for operation in state.get("inline_operations") or []:
                    sequence = int(operation.get("sequence", 0))
                    if sequence > 0:
                        records.setdefault(sequence, dict(operation))
            except (LiveCollaborationError, TypeError, ValueError):
                pass
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
        # Explicit compaction must include operations that are still inside the
        # hot-feed archive grace period.
        self._artifact_queue.join()
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
            _, recent_operations = self._recent_feed_state(
                room_path / "sequence-state.json",
                "latest_sequence",
                "recent_operations",
            )
            for index, operation in enumerate(segment_operations):
                sequence += 1
                operation_id = f"snapshot-{group_id}-{index}"
                operation_hash = hashlib.sha256(operation_id.encode()).hexdigest()[:20]
                stored = {
                    **operation,
                    "client_operation_id": operation_id,
                    "operation_kind": str(
                        operation.get("operation_kind") or "snapshot"
                    ),
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
                _write_json_atomic(
                    room_path / "operation-index" / f"{operation_hash}.json",
                    {"sequence": sequence, "file": destination.name},
                    durable=False,
                )
                self._remember_segment_owner(
                    room_path, stored.get("segment_id"), stored.get("author")
                )
                self._update_compact_segment_owner(
                    room_path, stored.get("segment_id"), stored.get("author")
                )
                recent_operations.append(
                    {"sequence": sequence, "file": destination.name}
                )
                created.append(stored)
            recent_operations = recent_operations[-RECENT_FEED_LIMIT:]
            _write_json_atomic(
                room_path / "sequence-state.json",
                {
                    "latest_sequence": sequence,
                    "updated_at": _utc_iso(),
                    "recent_operations": recent_operations,
                    "inline_operations": _bounded_inline_operations(created),
                },
                durable=False,
            )
            manifest = {
                "id": group_id,
                "created_at": _utc_iso(),
                "created_by": self.user_name,
                "before_sequence": before_sequence,
                "first_sequence": created[0]["sequence"],
                "last_sequence": created[-1]["sequence"],
                "segment_count": len(
                    {
                        str(operation.get("segment_id") or "")
                        for operation in created
                    }
                ),
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
                "segments": len(
                    {
                        str(operation.get("segment_id") or "")
                        for operation in created
                    }
                ),
                "through_sequence": before_sequence,
                "compacted": bool(compact),
            },
        )
        return manifest

    def snapshot_manifests(self, room_id):
        room_path = self._require_room(room_id)
        result = _parallel_map(
            _read_json_file, room_path.joinpath("snapshots").glob("*.json")
        )
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
        paths = list(room_path.joinpath("conflicts").glob("*.json"))

        def read_conflict(path):
            record = _read_json_file(path)
            record["_path"] = str(path)
            return record

        for record in _parallel_map(read_conflict, paths):
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

        def read_backup(path):
            meta_path = path.with_suffix(path.suffix + ".json")
            meta = _read_json_file(meta_path) if meta_path.is_file() else {}
            stat = path.stat()
            return {
                "name": path.name,
                "path": str(path),
                "size_bytes": stat.st_size,
                "modified_epoch": stat.st_mtime,
                "pinned": bool(meta.get("pinned", False)),
                "sha256": meta.get("sha256"),
                "created_by": meta.get("created_by"),
            }

        result = _parallel_map(
            read_backup, room_path.joinpath("backups").glob("*.mrb")
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

    def reserve_project_backup(self, room_id, interval_seconds, force=False):
        room_path = self._require_room(room_id)
        now = time.time()
        state_path = room_path / "backup-state.json"
        with self._named_lock(room_path, "backup", timeout_seconds=3.0):
            state = _read_json_file(state_path) if state_path.is_file() else {}
            last_backup = float(state.get("last_backup_epoch", 0.0) or 0.0)
            if not force and now - last_backup < max(60.0, float(interval_seconds)):
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


LAN_RELAY_METHODS = {
    "preflight",
    "join",
    "push_operation",
    "operations",
    "presence",
    "health_check",
    "leave",
    "send_chat",
    "chat_messages",
    "segment_locks",
    "set_segment_lock",
    "room_history",
    "snapshot_manifests",
    "state_at_sequence",
    "publish_room_snapshot",
    "room_conflicts",
    "resolve_conflict",
    "room_roles",
    "set_room_role",
    "review_states",
    "set_review_state",
    "request_segment_access",
    "segment_access_requests",
    "transfer_segment_owner",
    "get_material_template",
    "set_material_template",
    "audit_events",
    "diagnostics",
}


class LanRelayServer:
    """One-click stdlib LAN relay backed by the normal room store."""

    def __init__(self, storage_folder, access_code=None, host="0.0.0.0", port=0):
        self.storage_folder = normalize_shared_folder(storage_folder)
        self.access_code = str(access_code or uuid.uuid4().hex)
        self.host = str(host or "0.0.0.0")
        self.port = int(port or 0)
        self._httpd = None
        self._thread = None
        self._clients = {}
        self._clients_lock = threading.Lock()

    @staticmethod
    def _advertised_host():
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.connect(("8.8.8.8", 80))
                return str(probe.getsockname()[0])
            finally:
                probe.close()
        except OSError:
            try:
                return socket.gethostbyname(socket.gethostname())
            except OSError:
                return "127.0.0.1"

    @property
    def url(self):
        if self._httpd is None:
            return None
        return f"http://{self._advertised_host()}:{self._httpd.server_address[1]}"

    def _client(self, user_name):
        user_name = str(user_name or "").strip()
        if not user_name:
            raise LiveCollaborationError("LAN request is missing the user name")
        with self._clients_lock:
            client = self._clients.get(user_name)
            if client is None:
                client = SharedFolderRoomClient(self.storage_folder, user_name)
                self._clients[user_name] = client
            return client

    def _dispatch(self, payload):
        if not hmac.compare_digest(
            str(payload.get("access_code") or ""), self.access_code
        ):
            raise LiveCollaborationError("The LAN session code is invalid")
        method = str(payload.get("method") or "")
        if method not in LAN_RELAY_METHODS:
            raise LiveCollaborationError("Unsupported LAN collaboration request")
        client = self._client(payload.get("user"))
        args = payload.get("args") or []
        kwargs = payload.get("kwargs") or {}
        result = getattr(client, method)(*args, **kwargs)
        if method == "preflight" and isinstance(result, dict):
            result["remote_version"] = PLUGIN_VERSION
        return result

    def start(self):
        if self._httpd is not None:
            return self.url
        relay = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _send(self, status, payload):
                body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
                self.send_response(int(status))
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/health":
                    self._send(200, {"status": "ok", "transport": "direct-lan"})
                else:
                    self._send(404, {"error": "Not found"})

            def do_POST(self):
                if self.path != "/rpc":
                    self._send(404, {"error": "Not found"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 64 * 1024 * 1024:
                        raise LiveCollaborationError("Invalid LAN request size")
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    self._send(200, {"result": relay._dispatch(payload)})
                except Exception as exc:
                    self._send(400, {"error": str(exc)})

            def log_message(self, format, *args):
                del format, args

        self._httpd = http.server.ThreadingHTTPServer((self.host, self.port), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="LiveSegmentation-LAN-relay",
            daemon=True,
        )
        self._thread.start()
        return self.url

    def stop(self):
        httpd = self._httpd
        self._httpd = None
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except OSError:
                pass
        self._thread = None
        self._clients = {}


class LanRoomClient:
    """Dependency-free client for a one-click LAN relay."""

    def __init__(self, server_url, user_name, access_code, timeout_seconds=3.0):
        self.server_url = normalize_server_url(server_url)
        self.user_name = str(user_name or "").strip()
        self.access_code = str(access_code or "").strip()
        self.timeout_seconds = float(timeout_seconds)
        self.presence_session_id = uuid.uuid4().hex
        if not self.user_name:
            raise ValueError("Enter your display name")
        if not self.access_code:
            raise ValueError("Enter or import the LAN session code")

    def presence(self, room_id, details):
        return self._rpc(
            "presence",
            room_id,
            {**(details or {}), "presence_session_id": self.presence_session_id},
        )

    def leave(self, room_id):
        return self._rpc("leave", room_id, self.presence_session_id)

    def _rpc(self, method, *args, **kwargs):
        payload = json.dumps(
            {
                "user": self.user_name,
                "access_code": self.access_code,
                "method": str(method),
                "args": list(args),
                "kwargs": kwargs,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.server_url}/rpc",
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("error")
            except Exception:
                detail = str(exc)
            raise LiveCollaborationError(f"Direct LAN request failed: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LiveCollaborationError(f"Direct LAN connection failed: {exc}") from exc
        if not isinstance(result, dict) or "result" not in result:
            raise LiveCollaborationError("The LAN relay returned an invalid response")
        return result["result"]

    def preflight(
        self,
        room_name,
        signature,
        plugin_version=PLUGIN_VERSION,
        protocol_version=COLLABORATION_PROTOCOL_VERSION,
    ):
        started = time.monotonic()
        report = dict(
            self._rpc(
                "preflight",
                room_name,
                signature,
                plugin_version,
                int(protocol_version),
            )
            or {}
        )
        checks = list(report.get("checks") or [])
        checks.insert(
            0,
            _preflight_check(
                "direct-lan",
                "pass",
                "Direct LAN relay",
                f"Authenticated relay request completed in {(time.monotonic() - started) * 1000.0:.0f} ms.",
            ),
        )
        checks.insert(
            1,
            _preflight_check(
                "relay-version",
                "pass" if report.get("remote_version") == PLUGIN_VERSION else "fail",
                "LAN host version",
                f"Plugin {PLUGIN_VERSION}; LAN host {report.get('remote_version') or 'unknown'}.",
                "Install the same current release on the LAN host and every participant."
                if report.get("remote_version") != PLUGIN_VERSION
                else "",
            ),
        )
        checks.insert(
            2,
            _preflight_check(
                "transport-security",
                "warning",
                "LAN transport security",
                "The built-in LAN relay is unencrypted and intended only for a trusted LAN or VPN.",
                "For collaborators outside the local network, use a Remote HTTPS server.",
            ),
        )
        return finalize_preflight_report(
            {
                **report,
                "transport": "direct-lan",
                "latency_seconds": round(time.monotonic() - started, 4),
                "checks": checks,
            }
        )

    def __getattr__(self, name):
        if name not in LAN_RELAY_METHODS:
            raise AttributeError(name)
        return lambda *args, **kwargs: self._rpc(name, *args, **kwargs)


class HybridRoomClient:
    """Prefer direct LAN requests and fall back to the identical shared store."""

    MIRRORED_METHODS = {
        "push_operation",
        "send_chat",
        "set_segment_lock",
        "set_room_role",
        "set_review_state",
        "request_segment_access",
        "transfer_segment_owner",
        "set_material_template",
        "publish_room_snapshot",
    }

    def __init__(self, lan_client, fallback_client=None, retry_seconds=5.0):
        self.lan_client = lan_client
        self.fallback_client = fallback_client
        self.retry_seconds = max(1.0, float(retry_seconds))
        self._retry_primary_after = 0.0
        self._last_primary_error = None
        self.fallback_count = 0
        self.user_name = lan_client.user_name
        self.server_url = lan_client.server_url
        self.shared_folder = (
            fallback_client.shared_folder if fallback_client is not None else None
        )
        self._primary_room = None
        self._fallback_room = None
        self._fallback_join_done = threading.Event()
        self._fallback_join_error = None
        self._reconcile_lock = threading.Lock()

    def _mirror_in_background(self, method, args, kwargs):
        if self.fallback_client is None or self._fallback_room is None:
            return

        def mirror():
            try:
                mirrored_args = list(args)
                if mirrored_args:
                    mirrored_args[0] = self._fallback_room["id"]
                getattr(self.fallback_client, method)(*mirrored_args, **kwargs)
            except Exception as exc:
                self._fallback_join_error = str(exc)

        threading.Thread(
            target=mirror,
            name=f"LiveSegmentation-LAN-mirror-{method}",
            daemon=True,
        ).start()

    def _reconcile_rooms(self):
        if self._primary_room is None or self._fallback_room is None:
            return
        if not self._reconcile_lock.acquire(blocking=False):
            return
        try:
            primary_sequence = int(self._primary_room.get("latest_sequence", 0))
            fallback_sequence = int(self._fallback_room.get("latest_sequence", 0))
            if primary_sequence == fallback_sequence:
                return
            if fallback_sequence > primary_sequence:
                state = self.fallback_client.state_at_sequence(
                    self._fallback_room["id"], fallback_sequence
                )
                if state:
                    self.lan_client.publish_room_snapshot(
                        self._primary_room["id"],
                        state,
                        compact=False,
                        label="Recovered from shared-folder fallback",
                    )
            else:
                state = self.lan_client.state_at_sequence(
                    self._primary_room["id"], primary_sequence
                )
                if state:
                    self.fallback_client.publish_room_snapshot(
                        self._fallback_room["id"],
                        state,
                        compact=False,
                        label="Mirrored from direct LAN",
                    )
        except Exception as exc:
            self._fallback_join_error = str(exc)
        finally:
            self._reconcile_lock.release()

    def _start_fallback_join(self, room_name, signature):
        if self.fallback_client is None:
            self._fallback_join_done.set()
            return

        def join_fallback():
            try:
                self._fallback_room = self.fallback_client.join(room_name, signature)
                self._fallback_join_error = None
                self._reconcile_rooms()
            except Exception as exc:
                self._fallback_join_error = str(exc)
            finally:
                self._fallback_join_done.set()

        threading.Thread(
            target=join_fallback,
            name="LiveSegmentation-fallback-join",
            daemon=True,
        ).start()

    def _call(self, method, *args, **kwargs):
        if time.monotonic() >= self._retry_primary_after:
            try:
                value = getattr(self.lan_client, method)(*args, **kwargs)
                self._last_primary_error = None
                self._retry_primary_after = 0.0
                if method in self.MIRRORED_METHODS:
                    self._mirror_in_background(method, args, kwargs)
                return value
            except Exception as exc:
                self._last_primary_error = str(exc)
                self._retry_primary_after = time.monotonic() + self.retry_seconds
                if self.fallback_client is None:
                    raise
        if self.fallback_client is None or self._fallback_room is None:
            raise LiveCollaborationError(self._last_primary_error or "Direct LAN unavailable")
        self.fallback_count += 1
        fallback_args = list(args)
        if fallback_args:
            fallback_args[0] = self._fallback_room["id"]
        return getattr(self.fallback_client, method)(*fallback_args, **kwargs)

    def join(self, room_name, signature):
        self._start_fallback_join(room_name, signature)
        primary_room = None
        primary_error = None
        try:
            primary_room = self.lan_client.join(room_name, signature)
        except Exception as exc:
            primary_error = exc
        self._primary_room = primary_room
        if primary_room is not None:
            # A slow or offline institutional share must not delay a healthy
            # direct-LAN join. The fallback continues joining on its daemon lane.
            self._fallback_join_done.wait(1.0)
            if self._fallback_room is not None:
                self._reconcile_rooms()
            return primary_room
        self._fallback_join_done.wait(14.0)
        if self._fallback_room is None:
            raise primary_error or LiveCollaborationError("No collaboration transport is available")
        self._last_primary_error = str(primary_error)
        self._retry_primary_after = time.monotonic() + self.retry_seconds
        self.fallback_count += 1
        return self._fallback_room

    def preflight(
        self,
        room_name,
        signature,
        plugin_version=PLUGIN_VERSION,
        protocol_version=COLLABORATION_PROTOCOL_VERSION,
    ):
        def run_lan():
            return self.lan_client.preflight(
                room_name, signature, plugin_version, protocol_version
            )

        def run_fallback():
            if self.fallback_client is None:
                return None
            return self.fallback_client.preflight(
                room_name, signature, plugin_version, protocol_version
            )

        results = {}
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {"direct": executor.submit(run_lan)}
            if self.fallback_client is not None:
                futures["fallback"] = executor.submit(run_fallback)
            for name, future in futures.items():
                try:
                    results[name] = future.result()
                except Exception as exc:
                    results[name] = finalize_preflight_report(
                        {
                            "transport": name,
                            "checks": [
                                _preflight_check(
                                    f"{name}-connection",
                                    "fail",
                                    "Direct LAN" if name == "direct" else "Fallback folder",
                                    str(exc),
                                    "Check the address, code, firewall, and network route."
                                    if name == "direct"
                                    else "Check that the shared folder is reachable and writable.",
                                )
                            ],
                        }
                    )
        direct = results["direct"]
        fallback = results.get("fallback")
        checks = [
            {**item, "id": f"direct-{item.get('id')}"}
            for item in direct.get("checks") or []
        ]
        if fallback is not None:
            checks.extend(
                {**item, "id": f"fallback-{item.get('id')}", "title": f"Fallback: {item.get('title')}"}
                for item in fallback.get("checks") or []
            )
        report = finalize_preflight_report(
            {
                "transport": "direct-lan-with-shared-folder-fallback"
                if fallback is not None
                else "direct-lan",
                "checks": checks,
                "direct_lan": direct,
                "fallback": fallback,
            }
        )
        if direct.get("status") == "fail" and fallback and fallback.get("status") != "fail":
            report["status"] = "warning"
        return report

    def leave(self, room_id):
        result = None
        for client in (self.lan_client, self.fallback_client):
            if client is None:
                continue
            try:
                result = client.leave(room_id)
            except Exception:
                pass
        return result or {"left": True}

    def reserve_project_backup(self, room_id, interval_seconds, force=False):
        if self.fallback_client is None:
            return None
        return self.fallback_client.reserve_project_backup(
            room_id, interval_seconds, force=force
        )

    def diagnostics(self, room_id):
        started = time.monotonic()
        primary_ok = True
        try:
            primary = self.lan_client.diagnostics(room_id)
        except Exception as exc:
            primary_ok = False
            primary = {"status": "unavailable", "error": str(exc)}
        fallback = None
        if self.fallback_client is not None and self._fallback_room is not None:
            try:
                fallback = self.fallback_client.diagnostics(
                    self._fallback_room["id"]
                )
            except Exception as exc:
                fallback = {"status": "unavailable", "error": str(exc)}
        return {
            "status": "ok" if primary_ok or fallback else "unavailable",
            "transport": "direct-lan-with-shared-folder-fallback",
            "latency_seconds": round(time.monotonic() - started, 4),
            "direct_lan": primary,
            "fallback": fallback,
            "fallback_count": int(self.fallback_count),
        }

    def __getattr__(self, name):
        if name in {
            "list_project_backups",
            "set_backup_pinned",
            "verify_project_backup",
            "prune_project_backups",
        } and self.fallback_client is not None:
            return getattr(self.fallback_client, name)
        if name not in LAN_RELAY_METHODS:
            raise AttributeError(name)
        return lambda *args, **kwargs: self._call(name, *args, **kwargs)


def _uses_shared_folder(client):
    return isinstance(client, SharedFolderRoomClient) or (
        isinstance(client, HybridRoomClient) and client.fallback_client is not None
    )


def benchmark_room_transport(client, room_id, after_sequence=0, samples=5):
    """Measure non-destructive health and live-feed reads for the active room."""
    samples = max(2, min(int(samples), 20))
    health = []
    feed = []
    errors = []
    for _ in range(samples):
        started = time.monotonic()
        try:
            client.health_check(room_id)
            health.append(time.monotonic() - started)
        except Exception as exc:
            errors.append(str(exc))
        started = time.monotonic()
        try:
            client.operations(room_id, int(after_sequence), limit=1)
            feed.append(time.monotonic() - started)
        except Exception as exc:
            errors.append(str(exc))

    def stats(values):
        if not values:
            return None
        ordered = sorted(values)
        p95_index = min(len(ordered) - 1, max(0, int(round(0.95 * len(ordered) - 1))))
        return {
            "minimum_ms": round(ordered[0] * 1000.0, 2),
            "median_ms": round(ordered[len(ordered) // 2] * 1000.0, 2),
            "p95_ms": round(ordered[p95_index] * 1000.0, 2),
            "maximum_ms": round(ordered[-1] * 1000.0, 2),
        }

    combined = health + feed
    maximum = max(combined) if combined else float("inf")
    if errors and not combined:
        rating = "unavailable"
    elif errors or maximum >= 2.5:
        rating = "unstable"
    elif maximum >= 0.75:
        rating = "slow"
    elif maximum >= 0.2:
        rating = "good"
    else:
        rating = "excellent"
    return {
        "format": "live-segmentation-connection-benchmark-v1",
        "rating": rating,
        "sample_count": samples,
        "health": stats(health),
        "live_feed": stats(feed),
        "error_count": len(errors),
        "errors": errors[:3],
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
        self._joined_at_epoch = 0.0
        self._last_edit_activity_epoch = 0.0
        self.segmentation_node_id = None
        self.volume_shape = None
        self.source_volume_signature = None
        self.baselines = {}
        self.baseline_bounds = {}
        self.dirty_segments = set()
        self.force_snapshots = set()
        self.metadata_updates = set()
        self.outgoing = []
        self.outgoing_keys = set()
        self.awaiting_echo = []
        self._applied_local_operation_ids = set()
        self._known_segment_ids = set()
        self._segment_metadata = {}
        self._preserve_added_segment_ids = False
        self.pending_segment_deletions = {}
        self._observed_node = None
        self._observed_segmentation = None
        self._observer_tags = []
        self._observer_callbacks = []
        # vtkSegmentation emits its notifications while native Segment Editor
        # models are still updating.  Never traverse or mutate the segmentation
        # from that callback stack: queue one zero-delay Qt turn instead.
        self._segmentation_event_generation = 0
        self._segmentation_event_flush_scheduled = False
        self._pending_segmentation_event_ids = set()
        self._pending_segmentation_event_unspecified = False
        self._pending_segmentation_preserve_added_ids = False
        self._segment_revisions = {}
        self._segment_verifications = {}
        self._shared_folder_watcher = None
        self._applying_remote = False
        # Independent lanes keep slow project maintenance from blocking live
        # edits, presence, chat, or label locks.
        self._worker = None  # legacy compatibility alias; live lanes are below
        self._realtime_worker = None  # legacy compatibility alias
        self._edit_push_worker = None
        self._edit_pull_worker = None
        self._chat_send_worker = None
        self._chat_pull_worker = None
        self._presence_worker = None
        self._lock_set_worker = None
        self._lock_pull_worker = None
        self._maintenance_worker = None
        self._join_worker = None
        self._preflight_worker = None
        self._preflight_running = False
        self._preflight_started_at = 0.0
        self._joining = False
        self._join_started_at = 0.0
        self._join_status_second = -1
        self._join_context = None
        self._worker_results = queue.Queue()
        self._last_presence_send = 0.0
        self._presence_worker_started_at = 0.0
        self._presence_stall_status_second = -1
        self._presence_last_observed = {}
        self._last_metadata_fetch = 0.0
        self._last_sync_poll = 0.0
        self._last_chat_poll = 0.0
        self._last_lock_poll = 0.0
        self._last_health_check = 0.0
        self._last_backup_check = 0.0
        self._last_error = None
        self._last_transport_warning = None
        self._last_transport_result_at = 0.0
        self._transport_stall_status_second = -1
        self._session_token = 0
        self.connection_healthy = False
        self._connection_validation_pending = False
        self._connection_validation_started_at = 0.0
        self._connection_error_popup_shown = False
        self._connection_error_dialog = None
        self._force_sync_refresh = False
        self._force_realtime_refresh = False
        self._force_health_check = False
        self._force_advanced_refresh = False
        self._last_sync_duration = None
        self.last_chat_sequence = 0
        self.pending_chat = []
        self.displayed_chat_sequences = set()
        self.displayed_chat_ids = set()
        self.optimistic_chat_ids = set()
        self.segment_owners = {}
        self.segment_locks_state = {}
        self.pending_lock_changes = {}
        self.presence_by_user = {}
        self.room_roles_state = {}
        self.review_states_state = {}
        self.access_requests_state = []
        self.chat_anchors = {}
        self.history_records = []
        self.history_sequences = set()
        self.conflicts_state = []
        self.material_template_state = None
        self.backup_records = []
        self.last_diagnostics = None
        self.last_benchmark = None
        self.last_quality_report = None
        self.pending_actions = []
        self._last_advanced_fetch = 0.0
        self._last_snapshot_sequence = 0
        self._snapshot_requested = False
        self._snapshot_label = ""
        self._restoring_sequence = None
        self.chat_dock = None
        self.chat_dock_history = None
        self.chat_dock_input = None
        self.chat_dock_send_button = None
        self.chat_dock_anchor_checkbox = None
        self.activity_dock = None
        self.activity_dock_text = None
        self.comments = {}
        self.resolved_comment_ids = set()
        self.session_metrics = SessionMetrics()
        self._last_metrics_display = 0.0
        self._operation_journal = None
        self._journal_context = None
        self._journal_recovery = []
        self._comparison_node_id = None
        self.lan_server = None
        self._last_hybrid_fallback_count = 0

    def setup(self):
        import ctk
        import qt

        settings = qt.QSettings()
        default_user = str(settings.value(self.SETTINGS_PREFIX + "user", getpass.getuser()))
        # Connection targets are deliberately session-only.  In particular, do
        # not even copy a stale UNC path into a widget during startup: Windows
        # and Qt may synchronously probe such a path while restoring UI state.
        # Clearing legacy values here also makes crash recovery deterministic.
        for key in ("room", "transport", "sharedFolder", "server"):
            settings.remove(self.SETTINGS_PREFIX + key)
        settings.sync()
        default_room = ""
        default_transport = "shared-folder"
        default_shared_folder = ""
        default_server = ""
        recent_shared_folders = decode_recent_shared_folders(
            settings.value(self.SETTINGS_PREFIX + "recentSharedFolders", "[]")
        )

        self.group = qt.QGroupBox("Live collaboration")
        layout = qt.QVBoxLayout(self.group)
        explanation = qt.QLabel(
            "Join the same room to see each other's segmentation changes automatically. "
            "Use a shared/network folder, a trusted local LAN, or a Remote HTTPS server "
            "for collaborators anywhere on the internet."
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
        self.transport_combo.addItem("Direct LAN + shared-folder fallback")
        self.transport_combo.addItem("Remote HTTPS server")
        self.transport_combo.setCurrentIndex(2 if default_transport == "server" else 0)
        identity_layout.addRow("Name", self.user_edit)
        identity_layout.addRow("Room", self.room_edit)
        identity_layout.addRow("Connection", self.transport_combo)

        self.shared_folder_label = qt.QLabel("Shared folder")
        self.shared_folder_widget = qt.QWidget()
        shared_folder_layout = qt.QGridLayout(self.shared_folder_widget)
        shared_folder_layout.setContentsMargins(0, 0, 0, 0)
        self.shared_folder_edit = qt.QComboBox()
        self.shared_folder_edit.setEditable(True)
        self.shared_folder_edit.setInsertPolicy(qt.QComboBox.NoInsert)
        self.shared_folder_edit.setMaxVisibleItems(RECENT_SHARED_FOLDER_LIMIT)
        self.shared_folder_edit.addItems(recent_shared_folders)
        self.shared_folder_edit.setCurrentIndex(-1)
        self.shared_folder_edit.setEditText(default_shared_folder)
        self.shared_folder_edit.lineEdit().setPlaceholderText(
            r"e.g. P:\LiveSegmentation or \\server\share"
        )
        self.shared_folder_edit.setToolTip(
            "Choose a recently successful shared folder or enter a new path. "
            "The field always starts empty and the saved list is not accessed until you join."
        )
        self.shared_folder_button = qt.QPushButton("Browse…")
        self.shared_folder_button.clicked.connect(self.choose_shared_folder)
        self.shared_folder_clear_button = qt.QPushButton("Clear list")
        self.shared_folder_clear_button.setToolTip(
            "Forget the locally stored recent shared-folder paths"
        )
        self.shared_folder_clear_button.clicked.connect(self.clear_recent_shared_folders)
        shared_folder_layout.addWidget(self.shared_folder_edit, 0, 0, 1, 2)
        shared_folder_layout.addWidget(self.shared_folder_button, 1, 0)
        shared_folder_layout.addWidget(self.shared_folder_clear_button, 1, 1)
        shared_folder_layout.setColumnStretch(0, 1)
        shared_folder_layout.setColumnStretch(1, 1)
        identity_layout.addRow(self.shared_folder_label, self.shared_folder_widget)
        layout.addLayout(identity_layout)

        self.join_button = qt.QPushButton("Join live room")
        self.join_button.setMinimumHeight(34)
        self.join_button.clicked.connect(self.toggle_connection)
        connection_actions = qt.QGridLayout()
        connection_actions.addWidget(self.join_button, 0, 0, 1, 2)
        self.preflight_button = qt.QPushButton("Check connection")
        self.preflight_button.setToolTip(
            "Test reachability, permissions, protocol, plugin compatibility, source data, "
            "clock skew, and whether the second computer can see the same room"
        )
        self.preflight_button.clicked.connect(self.run_connection_preflight)
        connection_actions.addWidget(self.preflight_button, 1, 0, 1, 2)
        self.refresh_button = qt.QPushButton("Sync now")
        self.refresh_button.setToolTip(
            "Immediately request new edits, participants, chat, and label locks. "
            "This does not rescan the version history or backups."
        )
        self.refresh_button.enabled = False
        self.refresh_button.clicked.connect(self.refresh_now)
        connection_actions.addWidget(self.refresh_button, 2, 0)
        self.benchmark_button = qt.QPushButton("Benchmark")
        self.benchmark_button.setToolTip(
            "Measure non-destructive health and live-feed round trips for this room"
        )
        self.benchmark_button.enabled = False
        self.benchmark_button.clicked.connect(self.run_connection_benchmark)
        connection_actions.addWidget(self.benchmark_button, 2, 1)
        connection_actions.setColumnStretch(0, 1)
        connection_actions.setColumnStretch(1, 1)
        layout.addLayout(connection_actions)

        self.status_label = qt.QLabel("● Offline")
        self.status_label.setStyleSheet("color: #777;")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.users_label = qt.QLabel("Nobody else is connected")
        self.users_label.setWordWrap(True)
        layout.addWidget(self.users_label)
        self.preflight_text = qt.QPlainTextEdit()
        self.preflight_text.setReadOnly(True)
        self.preflight_text.setMaximumHeight(165)
        self.preflight_text.setPlaceholderText(
            "Run Check connection on both computers before joining. The check does not create or enter the room."
        )
        layout.addWidget(self.preflight_text)
        presence_actions = qt.QGridLayout()
        self.collaborator_combo = qt.QComboBox()
        self.collaborator_combo.setToolTip("Select an online collaborator")
        self.follow_checkbox = qt.QCheckBox("Follow view")
        self.follow_checkbox.setToolTip(
            "Keep slice offsets and crosshair aligned with the selected collaborator"
        )
        self.jump_to_user_button = qt.QPushButton("Jump to user")
        self.jump_to_user_button.enabled = False
        self.jump_to_user_button.clicked.connect(self.jump_to_selected_user)
        self.open_activity_dock_button = qt.QPushButton("Open activity")
        self.open_activity_dock_button.clicked.connect(self.show_activity_dock)
        presence_actions.addWidget(self.collaborator_combo, 0, 0, 1, 2)
        presence_actions.addWidget(self.follow_checkbox, 1, 0)
        presence_actions.addWidget(self.jump_to_user_button, 1, 1)
        presence_actions.addWidget(self.open_activity_dock_button, 2, 0, 1, 2)
        presence_actions.setColumnStretch(0, 1)
        presence_actions.setColumnStretch(1, 1)
        layout.addLayout(presence_actions)

        label_management_layout = qt.QFormLayout()
        self.label_combo = qt.QComboBox()
        self.label_combo.enabled = False
        self.label_combo.setToolTip(
            "Choose the label whose lock, owner, and review state you want to manage"
        )
        self.label_combo.currentIndexChanged.connect(self._on_label_combo_changed)
        label_management_layout.addRow("Label to manage", self.label_combo)
        layout.addLayout(label_management_layout)

        self.lock_status_label = qt.QLabel("Select a label after joining to manage its lock")
        self.lock_status_label.setWordWrap(True)
        layout.addWidget(self.lock_status_label)
        self.lock_button = qt.QPushButton("Lock selected label")
        self.lock_button.enabled = False
        self.lock_button.clicked.connect(self.toggle_selected_segment_lock)
        layout.addWidget(self.lock_button)
        lock_actions = qt.QGridLayout()
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
        lock_actions.addWidget(qt.QLabel("Lock duration"), 0, 0)
        lock_actions.addWidget(self.lock_expiry_spin, 0, 1)
        lock_actions.addWidget(self.request_access_button, 1, 0)
        lock_actions.addWidget(self.transfer_owner_button, 1, 1)
        lock_actions.setColumnStretch(0, 1)
        lock_actions.setColumnStretch(1, 1)
        layout.addLayout(lock_actions)

        review_actions = qt.QGridLayout()
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
        review_actions.addWidget(self.review_state_combo, 0, 0)
        review_actions.addWidget(self.set_review_button, 0, 1)
        review_actions.addWidget(self.review_note_edit, 1, 0, 1, 2)
        review_actions.setColumnStretch(0, 1)
        review_actions.setColumnStretch(1, 1)
        layout.addLayout(review_actions)

        role_actions = qt.QGridLayout()
        self.role_combo = qt.QComboBox()
        for role in ROOM_ROLES:
            self.role_combo.addItem(role.title(), role)
        self.set_role_button = qt.QPushButton("Set selected user's role")
        self.set_role_button.enabled = False
        self.set_role_button.clicked.connect(self.set_selected_user_role)
        role_actions.addWidget(qt.QLabel("Room role"), 0, 0)
        role_actions.addWidget(self.role_combo, 0, 1)
        role_actions.addWidget(self.set_role_button, 1, 0, 1, 2)
        role_actions.setColumnStretch(1, 1)
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
        chat_location_layout = qt.QGridLayout()
        self.chat_anchor_checkbox = qt.QCheckBox("Attach current Slicer location")
        self.chat_anchor_checkbox.checked = True
        self.chat_location_combo = qt.QComboBox()
        self.chat_location_combo.setToolTip("Messages that contain a spatial location")
        self.jump_to_chat_button = qt.QPushButton("Jump to message location")
        self.jump_to_chat_button.enabled = False
        self.jump_to_chat_button.clicked.connect(self.jump_to_selected_chat_anchor)
        self.open_chat_dock_button = qt.QPushButton("Open persistent chat")
        self.open_chat_dock_button.clicked.connect(self.show_chat_dock)
        chat_location_layout.addWidget(self.chat_anchor_checkbox, 0, 0, 1, 2)
        chat_location_layout.addWidget(self.chat_location_combo, 1, 0, 1, 2)
        chat_location_layout.addWidget(self.jump_to_chat_button, 2, 0)
        chat_location_layout.addWidget(self.open_chat_dock_button, 2, 1)
        chat_location_layout.setColumnStretch(0, 1)
        chat_location_layout.setColumnStretch(1, 1)
        chat_layout.addLayout(chat_location_layout)
        comment_layout = qt.QHBoxLayout()
        self.comment_input = qt.QLineEdit()
        self.comment_input.setPlaceholderText("Comment on selected label/current position…")
        self.comment_input.enabled = False
        self.comment_button = qt.QPushButton("Add comment")
        self.comment_button.enabled = False
        self.comment_button.clicked.connect(self.add_spatial_comment)
        comment_layout.addWidget(self.comment_input, 1)
        comment_layout.addWidget(self.comment_button)
        chat_layout.addLayout(comment_layout)
        self.comment_tree = qt.QTreeWidget()
        self.comment_tree.setHeaderLabels(["Status", "Time", "Author", "Label / comment"])
        self.comment_tree.setMaximumHeight(120)
        self.comment_tree.itemDoubleClicked.connect(
            lambda _item, _column: self.jump_to_selected_comment()
        )
        chat_layout.addWidget(self.comment_tree)
        comment_actions = qt.QHBoxLayout()
        self.jump_to_comment_button = qt.QPushButton("Jump to comment")
        self.jump_to_comment_button.enabled = False
        self.jump_to_comment_button.clicked.connect(self.jump_to_selected_comment)
        self.resolve_comment_button = qt.QPushButton("Resolve selected comment")
        self.resolve_comment_button.enabled = False
        self.resolve_comment_button.clicked.connect(self.resolve_selected_comment)
        comment_actions.addWidget(self.jump_to_comment_button)
        comment_actions.addWidget(self.resolve_comment_button)
        chat_layout.addLayout(comment_actions)
        layout.addWidget(chat_group)
        self._setup_chat_dock()

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
        self.backup_enabled_checkbox.toggled.connect(self._on_backup_settings_changed)
        self.backup_interval_spin.valueChanged.connect(self._on_backup_settings_changed)
        self.backup_retention_spin.valueChanged.connect(self._on_backup_settings_changed)
        self.backup_tree = qt.QTreeWidget()
        self.backup_tree.setHeaderLabels(["Backup", "Size", "Pinned", "Checksum"])
        self.backup_tree.setMaximumHeight(135)
        backup_form.addRow(self.backup_tree)
        backup_buttons = qt.QGridLayout()
        self.backup_now_button = qt.QPushButton("Back up now")
        self.backup_now_button.clicked.connect(self.create_backup_now)
        self.refresh_backups_button = qt.QPushButton("Refresh backups")
        self.refresh_backups_button.clicked.connect(self.refresh_backup_list)
        self.pin_backup_button = qt.QPushButton("Pin / unpin")
        self.pin_backup_button.clicked.connect(self.toggle_selected_backup_pin)
        self.verify_backup_button = qt.QPushButton("Verify")
        self.verify_backup_button.clicked.connect(self.verify_selected_backup)
        self.restore_backup_button = qt.QPushButton("Restore")
        self.restore_backup_button.clicked.connect(self.restore_selected_backup)
        for index, button in enumerate((
            self.backup_now_button,
            self.refresh_backups_button,
            self.pin_backup_button,
            self.verify_backup_button,
            self.restore_backup_button,
        )):
            button.enabled = False
            if index == 4:
                backup_buttons.addWidget(button, 2, 0, 1, 2)
            else:
                backup_buttons.addWidget(button, index // 2, index % 2)
        backup_buttons.setColumnStretch(0, 1)
        backup_buttons.setColumnStretch(1, 1)
        backup_form.addRow(backup_buttons)
        layout.addWidget(backup_group)
        self.backup_group = backup_group

        history_group = ctk.ctkCollapsibleButton()
        history_group.text = "Live activity, version history, and conflicts"
        history_group.collapsed = False
        history_layout = qt.QVBoxLayout(history_group)
        self.history_filter_edit = qt.QLineEdit()
        self.history_filter_edit.setPlaceholderText("Filter live activity by user, label, type, or sequence")
        self.history_filter_edit.textChanged.connect(self.filter_history_tree)
        history_layout.addWidget(self.history_filter_edit)
        self.history_tree = qt.QTreeWidget()
        self.history_tree.setHeaderLabels(
            ["Time", "Activity", "Seq", "Changed voxels", "Type"]
        )
        self.history_tree.setMaximumHeight(180)
        history_layout.addWidget(self.history_tree)
        history_buttons = qt.QGridLayout()
        self.refresh_history_button = qt.QPushButton("Reload full history")
        self.refresh_history_button.clicked.connect(self.refresh_advanced_state)
        self.restore_revision_button = qt.QPushButton("Restore selected revision")
        self.restore_revision_button.clicked.connect(self.restore_selected_revision)
        self.compare_revision_button = qt.QPushButton("Compare with current")
        self.compare_revision_button.clicked.connect(self.compare_selected_revision)
        self.undo_shared_button = qt.QPushButton("Undo my last shared edit")
        self.undo_shared_button.clicked.connect(self.undo_last_shared_edit)
        self.create_snapshot_button = qt.QPushButton("Snapshot + compact now")
        self.create_snapshot_button.clicked.connect(self.request_room_snapshot)
        self.snapshot_label_edit = qt.QLineEdit()
        self.snapshot_label_edit.setPlaceholderText("Optional milestone name")
        history_buttons.addWidget(self.snapshot_label_edit, 0, 0, 1, 2)
        for index, button in enumerate((
            self.refresh_history_button,
            self.restore_revision_button,
            self.compare_revision_button,
            self.undo_shared_button,
            self.create_snapshot_button,
        )):
            button.enabled = False
            if index == 4:
                history_buttons.addWidget(button, 3, 0, 1, 2)
            else:
                history_buttons.addWidget(button, 1 + index // 2, index % 2)
        history_buttons.setColumnStretch(0, 1)
        history_buttons.setColumnStretch(1, 1)
        history_layout.addLayout(history_buttons)
        self.conflict_tree = qt.QTreeWidget()
        self.conflict_tree.setHeaderLabels(
            ["Seq", "Label", "Other user", "Overlap", "Resolution"]
        )
        self.conflict_tree.setMaximumHeight(120)
        history_layout.addWidget(self.conflict_tree)
        conflict_buttons = qt.QGridLayout()
        self.resolve_latest_button = qt.QPushButton("Accept latest")
        self.resolve_mine_button = qt.QPushButton("Reapply mine")
        self.resolve_other_button = qt.QPushButton("Use other version")
        self.resolve_union_button = qt.QPushButton("Merge union")
        self.resolve_latest_button.clicked.connect(lambda: self.resolve_selected_conflict("latest"))
        self.resolve_mine_button.clicked.connect(lambda: self.resolve_selected_conflict("mine"))
        self.resolve_other_button.clicked.connect(lambda: self.resolve_selected_conflict("other"))
        self.resolve_union_button.clicked.connect(lambda: self.resolve_selected_conflict("union"))
        for index, button in enumerate((
            self.resolve_latest_button,
            self.resolve_mine_button,
            self.resolve_other_button,
            self.resolve_union_button,
        )):
            button.enabled = False
            conflict_buttons.addWidget(button, index // 2, index % 2)
        conflict_buttons.setColumnStretch(0, 1)
        conflict_buttons.setColumnStretch(1, 1)
        history_layout.addLayout(conflict_buttons)
        self.review_queue_tree = qt.QTreeWidget()
        self.review_queue_tree.setHeaderLabels(["Review state", "Label", "Updated by", "Note"])
        self.review_queue_tree.setMaximumHeight(120)
        self.review_queue_tree.itemDoubleClicked.connect(
            self.select_review_queue_label
        )
        history_layout.addWidget(self.review_queue_tree)
        layout.addWidget(history_group)

        project_group = ctk.ctkCollapsibleButton()
        project_group.text = "Diagnostics, templates, and invitations"
        project_group.collapsed = True
        project_layout = qt.QVBoxLayout(project_group)
        privacy = qt.QLabel(
            "Privacy: room names, chat, diagnostics, invitations, and complete MRB backups "
            "must not contain patient-identifying information unless your approved storage and "
            "governance explicitly permit it. Invitation files never contain API keys. "
            "Direct-LAN invitations do contain the temporary session code; share them privately."
        )
        privacy.setWordWrap(True)
        privacy.setStyleSheet("color: #8a4b00;")
        project_layout.addWidget(privacy)
        self.diagnostics_text = qt.QPlainTextEdit()
        self.diagnostics_text.setReadOnly(True)
        self.diagnostics_text.setMaximumHeight(130)
        project_layout.addWidget(self.diagnostics_text)
        self.performance_label = qt.QLabel("No live performance samples yet")
        self.performance_label.setWordWrap(True)
        project_layout.addWidget(self.performance_label)
        diagnostic_buttons = qt.QGridLayout()
        self.run_diagnostics_button = qt.QPushButton("Run diagnostics")
        self.run_diagnostics_button.clicked.connect(self.run_room_diagnostics)
        self.export_diagnostics_button = qt.QPushButton("Export sanitized report")
        self.export_diagnostics_button.clicked.connect(self.export_diagnostics)
        self.export_metrics_button = qt.QPushButton("Export research metrics")
        self.export_metrics_button.clicked.connect(self.export_session_metrics)
        diagnostic_buttons.addWidget(self.run_diagnostics_button, 0, 0)
        diagnostic_buttons.addWidget(self.export_diagnostics_button, 0, 1)
        diagnostic_buttons.addWidget(self.export_metrics_button, 1, 0, 1, 2)
        diagnostic_buttons.setColumnStretch(0, 1)
        diagnostic_buttons.setColumnStretch(1, 1)
        project_layout.addLayout(diagnostic_buttons)
        quality_layout = qt.QGridLayout()
        self.quality_min_component_spin = qt.QSpinBox()
        self.quality_min_component_spin.minimum = 1
        self.quality_min_component_spin.maximum = 1000000
        self.quality_min_component_spin.value = 20
        self.run_quality_button = qt.QPushButton("Run segmentation quality checks")
        self.run_quality_button.clicked.connect(self.run_quality_checks)
        quality_layout.addWidget(qt.QLabel("Small component <"), 0, 0)
        quality_layout.addWidget(self.quality_min_component_spin, 0, 1)
        quality_layout.addWidget(qt.QLabel("voxels"), 0, 2)
        quality_layout.addWidget(self.run_quality_button, 1, 0, 1, 3)
        quality_layout.setColumnStretch(1, 1)
        project_layout.addLayout(quality_layout)
        self.quality_text = qt.QPlainTextEdit()
        self.quality_text.setReadOnly(True)
        self.quality_text.setMaximumHeight(110)
        project_layout.addWidget(self.quality_text)
        template_buttons = qt.QVBoxLayout()
        self.publish_template_button = qt.QPushButton("Publish labels as room template")
        self.publish_template_button.clicked.connect(self.publish_material_template)
        self.apply_template_button = qt.QPushButton("Apply room template")
        self.apply_template_button.clicked.connect(self.apply_material_template)
        template_buttons.addWidget(self.publish_template_button)
        template_buttons.addWidget(self.apply_template_button)
        project_layout.addLayout(template_buttons)
        invite_buttons = qt.QVBoxLayout()
        self.export_invite_button = qt.QPushButton("Export .livesegroom invitation")
        self.export_invite_button.clicked.connect(self.export_invitation)
        self.import_invite_button = qt.QPushButton("Import invitation")
        self.import_invite_button.clicked.connect(self.import_invitation)
        invite_buttons.addWidget(self.export_invite_button)
        invite_buttons.addWidget(self.import_invite_button)
        project_layout.addLayout(invite_buttons)
        layout.addWidget(project_group)

        recovery_group = ctk.ctkCollapsibleButton()
        recovery_group.text = "Crash recovery"
        recovery_group.collapsed = True
        recovery_layout = qt.QVBoxLayout(recovery_group)
        self.recovery_enabled_checkbox = qt.QCheckBox("Keep crash-recovery journal")
        self.recovery_enabled_checkbox.setToolTip(
            "Keep a local crash-recovery journal for unacknowledged edits"
        )
        self.recovery_enabled_checkbox.checked = str(
            settings.value(self.SETTINGS_PREFIX + "crashRecovery", "true")
        ).lower() not in {"0", "false", "no"}
        self.recovery_enabled_checkbox.toggled.connect(
            self._on_recovery_setting_changed
        )
        recovery_layout.addWidget(self.recovery_enabled_checkbox)
        self.recovery_status_label = qt.QLabel("No recoverable edits for the active room")
        self.recovery_status_label.setWordWrap(True)
        recovery_layout.addWidget(self.recovery_status_label)
        recovery_buttons = qt.QHBoxLayout()
        self.recover_edits_button = qt.QPushButton("Recover pending edits")
        self.discard_recovery_button = qt.QPushButton("Discard recovery")
        self.recover_edits_button.enabled = False
        self.discard_recovery_button.enabled = False
        self.recover_edits_button.clicked.connect(self.recover_pending_edits)
        self.discard_recovery_button.clicked.connect(self.discard_pending_recovery)
        recovery_buttons.addWidget(self.recover_edits_button)
        recovery_buttons.addWidget(self.discard_recovery_button)
        recovery_layout.addLayout(recovery_buttons)
        layout.addWidget(recovery_group)

        self.server_settings = ctk.ctkCollapsibleButton()
        self.server_settings.text = "HTTPS server address"
        self.server_settings.collapsed = False
        advanced_layout = qt.QFormLayout(self.server_settings)
        self.server_edit = qt.QLineEdit(default_server)
        self.server_edit.setPlaceholderText("https://collaboration.example.org")
        self.api_key_edit = qt.QLineEdit()
        self.api_key_edit.echoMode = qt.QLineEdit.Password
        self.allow_insecure_http_checkbox = qt.QCheckBox(
            "Allow insecure HTTP for a trusted local test only"
        )
        self.allow_insecure_http_checkbox.checked = False
        self.server_security_label = qt.QLabel(
            "For internet use, enter the same public https:// address and an individual "
            "access token on every computer. No institutional intranet or shared drive is required."
        )
        self.server_security_label.setWordWrap(True)
        advanced_layout.addRow("Server URL", self.server_edit)
        advanced_layout.addRow("Access token", self.api_key_edit)
        advanced_layout.addRow(self.allow_insecure_http_checkbox)
        advanced_layout.addRow(self.server_security_label)
        layout.addWidget(self.server_settings)

        self.lan_settings = ctk.ctkCollapsibleButton()
        self.lan_settings.text = "Direct LAN address and host"
        self.lan_settings.collapsed = False
        lan_form = qt.QFormLayout(self.lan_settings)
        self.lan_url_edit = qt.QLineEdit()
        self.lan_url_edit.setPlaceholderText("e.g. http://192.168.1.20:8765")
        self.lan_access_code_edit = qt.QLineEdit()
        self.lan_access_code_edit.setPlaceholderText("Imported or generated session code")
        self.lan_access_code_edit.echoMode = qt.QLineEdit.PasswordEchoOnEdit
        self.lan_host_checkbox = qt.QCheckBox("Host the LAN relay on this computer")
        self.lan_host_checkbox.setToolTip(
            "Enable this on exactly one computer. Check connection or Join live room "
            "will start the host automatically. Other users enter the displayed URL and code."
        )
        self.lan_port_spin = qt.QSpinBox()
        self.lan_port_spin.minimum = 0
        self.lan_port_spin.maximum = 65535
        self.lan_port_spin.value = 0
        self.lan_port_spin.specialValueText = "Automatic"
        self.lan_host_button = qt.QPushButton("Start LAN host")
        self.lan_host_button.clicked.connect(self.toggle_lan_host)
        self.lan_status_label = qt.QLabel(
            "Host: enable the checkbox, then click Start LAN host (or Join). "
            "Guest: enter the host's LAN relay URL and session code. No shared folder is required; "
            "a selected folder is used only as an optional mirror/fallback. Use this unencrypted "
            "relay only on a trusted LAN or VPN."
        )
        self.lan_status_label.setWordWrap(True)
        lan_form.addRow("LAN relay URL", self.lan_url_edit)
        lan_form.addRow("Session code", self.lan_access_code_edit)
        lan_form.addRow(self.lan_host_checkbox)
        lan_form.addRow("Host port", self.lan_port_spin)
        lan_form.addRow(self.lan_host_button)
        lan_form.addRow(self.lan_status_label)
        layout.addWidget(self.lan_settings)

        # Connection addresses belong next to the connection selector, not at
        # the bottom of a long module panel. Reinsert both mode-specific panels
        # immediately below the identity form so their URL fields are visible
        # before the Join and Check connection buttons.
        layout.removeWidget(self.server_settings)
        layout.removeWidget(self.lan_settings)
        layout.insertWidget(2, self.server_settings)
        layout.insertWidget(2, self.lan_settings)

        self.transport_combo.currentIndexChanged.connect(self._update_transport_fields)
        self._update_transport_fields()

        self.owner.layout.addWidget(self.group)
        self._setup_activity_dock()

        self.timer = qt.QTimer()
        # Watchers handle the normal hot path. This short timer remains the
        # compatibility fallback for network filesystems without notifications.
        self.timer.setInterval(75)
        self.timer.timeout.connect(self.on_timer)

    @staticmethod
    def _text(widget):
        for attribute in ("currentText", "text"):
            if not hasattr(widget, attribute):
                continue
            value = getattr(widget, attribute)
            return str(value() if callable(value) else value).strip()
        return ""

    def _transport_mode(self):
        index = self.transport_combo.currentIndex
        index = index() if callable(index) else index
        return {0: "shared-folder", 1: "direct-lan", 2: "server"}.get(
            int(index), "shared-folder"
        )

    def _live_status_text(self):
        if isinstance(self.client, HybridRoomClient):
            transport = (
                "direct LAN / shared-folder fallback"
                if self.client.fallback_client is not None
                else "direct LAN"
            )
        elif isinstance(self.client, LanRoomClient):
            transport = "direct LAN"
        elif isinstance(self.client, SharedFolderRoomClient):
            transport = "shared folder"
        else:
            transport = "server"
        node = self._segmentation_node()
        node_name = node.GetName() if node is not None else "shared segmentation"
        return f"● Live via {transport} in room “{self.room_name}” — editing “{node_name}”"

    def _initial_sync_status_text(self):
        completed = min(int(self.last_sequence), int(self.initial_sequence))
        total = max(1, int(self.initial_sequence))
        return (
            "● Connected — loading shared segmentation… "
            f"{completed} / {total} changes"
        )

    def _update_transport_fields(self, index=None):
        del index
        mode = self._transport_mode()
        shared = mode in {"shared-folder", "direct-lan"}
        direct = mode == "direct-lan"
        self.shared_folder_label.setText(
            "Fallback folder (optional)" if direct else "Shared folder"
        )
        self.shared_folder_label.setVisible(shared)
        self.shared_folder_widget.setVisible(shared)
        self.server_settings.setVisible(mode == "server")
        self.lan_settings.setVisible(direct)
        if mode == "server":
            self.server_settings.collapsed = False
        if direct:
            self.lan_settings.collapsed = False
        self.backup_group.setVisible(shared)
        if hasattr(self, "backup_enabled_checkbox"):
            self._on_backup_settings_changed()

    def toggle_lan_host(self, checked=False):
        del checked
        import qt

        if self.lan_server is not None:
            self.lan_server.stop()
            self.lan_server = None
            self.lan_host_button.setText("Start LAN host")
            self.lan_status_label.setText("LAN host stopped")
            return
        try:
            shared_folder = self._text(self.shared_folder_edit)
            access_code = self._text(self.lan_access_code_edit) or uuid.uuid4().hex
            port = self.lan_port_spin.value
            port = port() if callable(port) else port
            local_root = Path(
                str(
                    qt.QStandardPaths.writableLocation(
                        qt.QStandardPaths.AppLocalDataLocation
                    )
                )
            )
            relay_identity = hashlib.sha256(
                f"{shared_folder}\0{access_code}".encode()
            ).hexdigest()[:24]
            relay_storage = (
                local_root
                / "LiveSegmentation"
                / "direct-lan-rooms"
                / relay_identity
            )
            self.lan_server = LanRelayServer(
                relay_storage,
                access_code=access_code,
                port=int(port),
            )
            url = self.lan_server.start()
            self.lan_url_edit.setText(url)
            self.lan_access_code_edit.setText(access_code)
            self.lan_host_checkbox.checked = True
            self.lan_host_button.setText("Stop LAN host")
            self.lan_status_label.setText(
                f"LAN host ready at {url}. Local live state is mirrored to the "
                "optional shared-folder fallback. Export a .livesegroom invitation."
            )
        except Exception as exc:
            if self.lan_server is not None:
                self.lan_server.stop()
                self.lan_server = None
            self._show_error(f"Could not start the LAN host: {exc}", popup=True)

    def choose_shared_folder(self, checked=False):
        del checked
        import qt
        import slicer

        selected = qt.QFileDialog.getExistingDirectory(
            slicer.util.mainWindow(),
            "Select shared/network folder",
            "",
        )
        if selected:
            self.shared_folder_edit.setEditText(str(selected))

    def clear_recent_shared_folders(self, checked=False):
        del checked
        import qt

        self.shared_folder_edit.clear()
        self.shared_folder_edit.setCurrentIndex(-1)
        self.shared_folder_edit.setEditText("")
        settings = qt.QSettings()
        settings.remove(self.SETTINGS_PREFIX + "recentSharedFolders")
        settings.sync()

    def _remember_recent_shared_folder(self, path):
        import qt

        settings = qt.QSettings()
        recent = update_recent_shared_folders(
            settings.value(self.SETTINGS_PREFIX + "recentSharedFolders", "[]"),
            path,
        )
        settings.setValue(
            self.SETTINGS_PREFIX + "recentSharedFolders",
            json.dumps(recent, ensure_ascii=False),
        )
        settings.sync()
        self.shared_folder_edit.clear()
        self.shared_folder_edit.addItems(recent)
        self.shared_folder_edit.setCurrentIndex(0 if recent else -1)
        self.shared_folder_edit.setEditText(recent[0] if recent else "")

    def _journal_for_context(self, context):
        import qt

        root = str(
            qt.QStandardPaths.writableLocation(qt.QStandardPaths.AppLocalDataLocation)
        )
        identity = json.dumps(dict(context or {}), sort_keys=True, separators=(",", ":"))
        file_name = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24] + ".json"
        return PendingOperationJournal(
            Path(root) / "LiveSegmentation" / "recovery" / file_name
        )

    def _on_recovery_setting_changed(self, enabled):
        import qt

        settings = qt.QSettings()
        settings.setValue(self.SETTINGS_PREFIX + "crashRecovery", bool(enabled))
        settings.sync()
        self._sync_operation_journal()

    def _sync_operation_journal(self):
        if self._operation_journal is None or self._journal_context is None:
            return
        enabled = self.recovery_enabled_checkbox.checked
        enabled = enabled() if callable(enabled) else enabled
        if not enabled:
            self._operation_journal.clear()
            return
        pending = []
        seen = set()
        for operation in [*self.outgoing, *self.awaiting_echo]:
            operation_id = str(operation.get("client_operation_id") or "")
            if operation_id and operation_id not in seen:
                pending.append(dict(operation))
                seen.add(operation_id)
        self._operation_journal.write(self._journal_context, pending)
        if pending:
            self.recovery_status_label.setText(
                f"Crash journal contains {len(pending)} unacknowledged edit(s)."
            )
        elif not self._journal_recovery:
            self.recovery_status_label.setText("No recoverable edits for the active room")

    def recover_pending_edits(self, checked=False):
        del checked
        existing = {
            str(item.get("client_operation_id") or "")
            for item in [*self.outgoing, *self.awaiting_echo]
        }
        recovered = 0
        for operation in self._journal_recovery:
            operation_id = str(operation.get("client_operation_id") or "")
            if not operation_id or operation_id in existing:
                continue
            self.outgoing.append(dict(operation))
            existing.add(operation_id)
            recovered += 1
            self.session_metrics.operation_queued(operation_id)
        self._journal_recovery = []
        self.recover_edits_button.enabled = False
        self.discard_recovery_button.enabled = False
        self.recovery_status_label.setText(
            f"Queued {recovered} recovered edit(s) for idempotent synchronization."
        )
        self._sync_operation_journal()
        self._force_sync_refresh = True
        self.on_timer()

    def discard_pending_recovery(self, checked=False):
        del checked
        self._journal_recovery = []
        if self._operation_journal is not None:
            self._operation_journal.clear()
        self.recover_edits_button.enabled = False
        self.discard_recovery_button.enabled = False
        self.recovery_status_label.setText("Recovery was discarded for this room")

    def _update_performance_label(self):
        summary = self.session_metrics.summary()
        stages = summary.get("stages") or {}
        preferred = []
        for key, label in (
            ("edit_roundtrip", "edit roundtrip"),
            ("edit-push", "publish"),
            ("edit-pull", "receive poll"),
            ("apply", "apply/render"),
            ("chat-send", "chat send"),
        ):
            if key in stages:
                preferred.append(f"{label}: {stages[key]['last_ms']:.0f} ms")
        if self.last_benchmark:
            preferred.append(f"connection: {self.last_benchmark.get('rating', 'unknown')}")
        self.performance_label.setText(
            "Live performance — " + " · ".join(preferred)
            if preferred
            else "No live performance samples yet"
        )

    def run_connection_benchmark(self, checked=False):
        del checked
        if not self.connected:
            self._show_error("Join a live room before running a benchmark", popup=True)
            return
        self.benchmark_button.enabled = False
        self.performance_label.setText("Running non-destructive connection benchmark…")
        self._queue_action("benchmark", after_sequence=int(self.last_sequence))

    def export_session_metrics(self, checked=False):
        del checked
        import qt
        import slicer

        if not self.connected:
            self._show_error("Join a live room before exporting session metrics", popup=True)
            return
        path = qt.QFileDialog.getSaveFileName(
            slicer.util.mainWindow(),
            "Export anonymized Live Segmentation metrics",
            "live-segmentation-session-metrics.json",
            "JSON (*.json)",
        )
        if not path:
            return
        payload = self.session_metrics.summary()
        payload["connection_benchmark"] = (
            {
                key: value
                for key, value in self.last_benchmark.items()
                if key != "errors"
            }
            if self.last_benchmark
            else None
        )
        payload["quality_summary"] = (
            {
                "issue_count": self.last_quality_report.get("issue_count", 0),
                "label_count": len(self.last_quality_report.get("labels") or {}),
            }
            if self.last_quality_report
            else None
        )
        Path(str(path)).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        slicer.util.showStatusMessage("Anonymized research metrics exported", 3000)

    def run_quality_checks(self, checked=False):
        del checked
        if not self.connected:
            self._show_error("Join a live room before checking segmentation quality", popup=True)
            return
        node = self._segmentation_node()
        masks = {
            str(segment_id): self._read_mask(node, segment_id)
            for segment_id in node.GetSegmentation().GetSegmentIDs()
        }
        threshold = self.quality_min_component_spin.value
        threshold = threshold() if callable(threshold) else threshold
        self.run_quality_button.enabled = False
        self.quality_text.setPlainText("Analyzing empty labels, components, and overlaps…")

        def analyze():
            started = time.monotonic()
            try:
                report = segmentation_quality_report(masks, int(threshold))
                self._worker_results.put(
                    {
                        "lane": "local-quality",
                        "session_token": self._session_token,
                        "quality": report,
                        "duration": time.monotonic() - started,
                    }
                )
            except Exception as exc:
                self._worker_results.put(
                    {
                        "lane": "local-quality",
                        "session_token": self._session_token,
                        "error": str(exc),
                        "duration": time.monotonic() - started,
                    }
                )

        threading.Thread(
            target=analyze,
            name="LiveSegmentation-quality-check",
            daemon=True,
        ).start()

    def toggle_connection(self, checked=False):
        del checked
        if self._joining:
            self._cancel_join()
        elif self.connected:
            self.leave()
        else:
            self.join()

    def _show_error(self, message, popup=False):
        import qt
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
            if self._connection_error_dialog is None:
                dialog = qt.QMessageBox(slicer.util.mainWindow())
                dialog.setWindowTitle("Live Segmentation connection problem")
                dialog.setIcon(qt.QMessageBox.Critical)
                dialog.setStandardButtons(qt.QMessageBox.Ok)
                dialog.setModal(False)
                self._connection_error_dialog = dialog
            self._connection_error_dialog.setText(detail)
            self._connection_error_dialog.show()

    def refresh_now(self, checked=False):
        del checked
        if not self.connected:
            self._show_error("Join a live room before refreshing", popup=True)
            return
        self._force_sync_refresh = True
        self._force_realtime_refresh = True
        self._force_health_check = True
        self.status_label.setText("● Syncing live state…")
        self.status_label.setStyleSheet("color: #b26a00;")
        self.on_timer()

    def _disconnect_for_connection_loss(self, message):
        message = str(message or "The collaboration location stopped responding")
        shared_folder_session = _uses_shared_folder(self.client)
        self.leave(notify_remote=False)
        if shared_folder_session:
            self.shared_folder_edit.setCurrentIndex(-1)
            self.shared_folder_edit.setEditText("")
        self._show_error(
            f"{message} The live session was reset locally; choose a reachable "
            "shared folder or server and join again.",
            popup=True,
        )

    def _setup_chat_dock(self):
        import qt
        import slicer

        main_window = slicer.util.mainWindow()
        if main_window is None:
            return
        self.chat_dock = qt.QDockWidget("Live Segmentation Chat", main_window)
        self.chat_dock.setObjectName("LiveSegmentationChatDock")
        self.chat_dock.setToolTip(
            "Room chat stays visible while Segment Editor or another module is open"
        )
        container = qt.QWidget()
        layout = qt.QVBoxLayout(container)
        self.chat_dock_history = qt.QPlainTextEdit()
        self.chat_dock_history.setReadOnly(True)
        self.chat_dock_history.setPlaceholderText("Join a live room to chat")
        layout.addWidget(self.chat_dock_history, 1)
        entry = qt.QHBoxLayout()
        self.chat_dock_input = qt.QLineEdit()
        self.chat_dock_input.setPlaceholderText("Write a room message…")
        self.chat_dock_input.enabled = False
        self.chat_dock_input.returnPressed.connect(self.send_chat_dock_message)
        self.chat_dock_send_button = qt.QPushButton("Send")
        self.chat_dock_send_button.enabled = False
        self.chat_dock_send_button.clicked.connect(self.send_chat_dock_message)
        entry.addWidget(self.chat_dock_input, 1)
        entry.addWidget(self.chat_dock_send_button)
        layout.addLayout(entry)
        self.chat_dock_anchor_checkbox = qt.QCheckBox(
            "Comment at current Slicer position (jump target)"
        )
        self.chat_dock_anchor_checkbox.checked = True
        layout.addWidget(self.chat_dock_anchor_checkbox)
        self.chat_dock.setWidget(container)
        main_window.addDockWidget(qt.Qt.RightDockWidgetArea, self.chat_dock)
        self.chat_dock.hide()

    def _setup_activity_dock(self):
        import qt
        import slicer

        main_window = slicer.util.mainWindow()
        if main_window is None:
            return
        self.activity_dock = qt.QDockWidget("Live Segmentation Activity", main_window)
        self.activity_dock.setObjectName("LiveSegmentationActivityDock")
        self.activity_dock.setToolTip(
            "Remote edits remain visible while Segment Editor or another module is active"
        )
        self.activity_dock_text = qt.QPlainTextEdit()
        self.activity_dock_text.setReadOnly(True)
        self.activity_dock_text.setPlaceholderText("Your and collaborators' label changes appear here")
        self.activity_dock.setWidget(self.activity_dock_text)
        main_window.addDockWidget(qt.Qt.RightDockWidgetArea, self.activity_dock)
        self.activity_dock.hide()

    def show_activity_dock(self, checked=False):
        del checked
        if self.activity_dock is not None:
            self.activity_dock.show()
            self.activity_dock.raise_()

    def _append_activity(self, text):
        if self.activity_dock_text is None:
            return
        self.activity_dock_text.appendPlainText(
            f"[{datetime.now().strftime('%H:%M:%S')}] {str(text)}"
        )
        self._scroll_chat_to_end(self.activity_dock_text)

    def show_chat_dock(self, checked=False):
        del checked
        if self.chat_dock is not None:
            self.chat_dock.show()
            self.chat_dock.raise_()

    @staticmethod
    def _scroll_chat_to_end(widget):
        if widget is None:
            return
        scroll_bar = widget.verticalScrollBar()
        maximum = scroll_bar.maximum
        maximum = maximum() if callable(maximum) else maximum
        scroll_bar.setValue(maximum)

    def _append_chat_line(self, line):
        self.chat_history.appendPlainText(str(line))
        if self.chat_dock_history is not None:
            self.chat_dock_history.appendPlainText(str(line))
        self._scroll_chat_to_end(self.chat_history)
        self._scroll_chat_to_end(self.chat_dock_history)

    def _queue_chat_message(self, text, anchor_enabled, anchor_metadata=None):
        if not self.connected:
            self._show_error("Join a live room before sending a message", popup=True)
            return
        text = str(text or "").strip()
        if not text:
            return
        message = {"client_message_id": str(uuid.uuid4()), "text": text}
        if anchor_enabled or anchor_metadata:
            message["anchor"] = self._current_location() if anchor_enabled else {}
            message["anchor"].update(dict(anchor_metadata or {}))
            if message["anchor"].get("message_kind") == "comment":
                message["anchor"]["comment_id"] = message["client_message_id"]
        self.pending_chat.append(message)
        self.session_metrics.increment(
            "comments_queued"
            if (message.get("anchor") or {}).get("message_kind") == "comment"
            else "chat_messages_queued"
        )
        self.optimistic_chat_ids.add(message["client_message_id"])
        marker = " 📍" if message.get("anchor") else ""
        self._append_chat_line(
            f"[{datetime.now().strftime('%H:%M')}] {self.user_name}: {text}{marker}"
        )
        self.displayed_chat_ids.add(
            f"{self.user_name}\0{message['client_message_id']}"
        )
        self._force_realtime_refresh = True
        self.on_timer()
        return message

    def send_chat_message(self):
        anchor_enabled = self.chat_anchor_checkbox.checked
        anchor_enabled = anchor_enabled() if callable(anchor_enabled) else anchor_enabled
        self._queue_chat_message(self._text(self.chat_input), bool(anchor_enabled))
        self.chat_input.clear()

    def send_chat_dock_message(self):
        if self.chat_dock_input is None:
            return
        anchor_enabled = self.chat_dock_anchor_checkbox.checked
        anchor_enabled = anchor_enabled() if callable(anchor_enabled) else anchor_enabled
        self._queue_chat_message(self._text(self.chat_dock_input), bool(anchor_enabled))
        self.chat_dock_input.clear()

    def add_spatial_comment(self, checked=False):
        del checked
        text = self._text(self.comment_input)
        if not text:
            return
        segment_id = self._selected_segment_id()
        node = self._segmentation_node()
        segment = (
            node.GetSegmentation().GetSegment(segment_id)
            if node is not None and segment_id
            else None
        )
        message = self._queue_chat_message(
            text,
            True,
            {
                "message_kind": "comment",
                "segment_id": segment_id,
                "segment_name": segment.GetName() if segment is not None else "",
            },
        )
        if message:
            self._register_comment(
                {
                    "sequence": 0,
                    "client_message_id": message["client_message_id"],
                    "author": self.user_name,
                    "text": text,
                    "created_at": _utc_iso(),
                    "anchor": message.get("anchor"),
                }
            )
        self.comment_input.clear()

    def _register_comment(self, message):
        anchor = message.get("anchor") or {}
        comment_id = str(
            anchor.get("comment_id") or message.get("client_message_id") or ""
        )
        if not comment_id:
            return
        record = dict(message)
        record["resolved"] = comment_id in self.resolved_comment_ids
        self.comments[comment_id] = record
        self._refresh_comment_tree()

    def _refresh_comment_tree(self):
        import qt

        selected_id = None
        selected = self.comment_tree.currentItem()
        if selected is not None:
            selected_id = str(selected.data(0, 32) or "")
        self.comment_tree.clear()
        for comment_id, record in sorted(
            self.comments.items(),
            key=lambda item: int(item[1].get("sequence", 0)),
            reverse=True,
        ):
            anchor = record.get("anchor") or {}
            label = str(anchor.get("segment_name") or anchor.get("segment_id") or "view")
            item = qt.QTreeWidgetItem(
                [
                    "resolved" if record.get("resolved") else "open",
                    self._local_clock_text(record.get("created_at")),
                    str(record.get("author") or ""),
                    f"{label}: {str(record.get('text') or '')}",
                ]
            )
            item.setData(0, 32, comment_id)
            self.comment_tree.addTopLevelItem(item)
            if comment_id == selected_id:
                self.comment_tree.setCurrentItem(item)
        has_comments = bool(self.comments)
        self.jump_to_comment_button.enabled = has_comments
        self.resolve_comment_button.enabled = has_comments

    def _selected_comment_id(self):
        item = self.comment_tree.currentItem()
        return str(item.data(0, 32) or "") if item is not None else ""

    def jump_to_selected_comment(self, checked=False):
        del checked
        record = self.comments.get(self._selected_comment_id()) or {}
        anchor = record.get("anchor")
        if isinstance(anchor, dict):
            self._apply_location(anchor)

    def resolve_selected_comment(self, checked=False):
        del checked
        comment_id = self._selected_comment_id()
        if not comment_id:
            return
        self.resolved_comment_ids.add(comment_id)
        if comment_id in self.comments:
            self.comments[comment_id]["resolved"] = True
        self._refresh_comment_tree()
        self._queue_chat_message(
            "Resolved a spatial comment",
            False,
            {
                "message_kind": "comment-resolution",
                "target_comment_id": comment_id,
            },
        )

    def toggle_selected_segment_lock(self, checked=False):
        del checked
        if not self.connected:
            self._show_error("Join a live room before changing a label lock", popup=True)
            return
        segment_id = self._selected_segment_id()
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
        self._force_realtime_refresh = True
        self.on_timer()

    def _queue_action(self, action, **payload):
        record = {"id": str(uuid.uuid4()), "action": str(action), **payload}
        self.pending_actions.append(record)
        return record

    def _selected_segment_id(self):
        if hasattr(self, "label_combo"):
            value = self._combo_current_data(self.label_combo)
            if value:
                return str(value)
        _, segment_id = self.owner.get_selected_segmentation_node_and_segment_id()
        return str(segment_id) if segment_id else None

    def _refresh_label_combo(self):
        if not hasattr(self, "label_combo"):
            return
        current = self._selected_segment_id()
        node = self._segmentation_node()
        entries = []
        if node is not None:
            for segment_id in node.GetSegmentation().GetSegmentIDs():
                segment = node.GetSegmentation().GetSegment(segment_id)
                entries.append(
                    (str(segment_id), str(segment.GetName() if segment is not None else segment_id))
                )
        combo_count = self.label_combo.count
        combo_count = combo_count() if callable(combo_count) else combo_count
        existing = [
            str(self.label_combo.itemData(index))
            for index in range(int(combo_count))
        ]
        if existing == [segment_id for segment_id, _ in entries]:
            return
        self.label_combo.blockSignals(True)
        self.label_combo.clear()
        selected_index = 0
        for index, (segment_id, name) in enumerate(entries):
            self.label_combo.addItem(name, segment_id)
            if segment_id == current:
                selected_index = index
        if entries:
            self.label_combo.setCurrentIndex(selected_index)
        self.label_combo.blockSignals(False)
        self.label_combo.enabled = bool(self.connected and entries)

    def _on_label_combo_changed(self, index=None):
        del index
        segment_id = self._selected_segment_id()
        if segment_id and hasattr(self.owner, "select_segment_in_editor"):
            self.owner.select_segment_in_editor(segment_id)
        self._update_lock_controls()

    def _on_backup_settings_changed(self, value=None):
        del value
        import qt

        enabled = self.backup_enabled_checkbox.checked
        enabled = enabled() if callable(enabled) else enabled
        interval = self.backup_interval_spin.value
        interval = interval() if callable(interval) else interval
        retention = self.backup_retention_spin.value
        retention = retention() if callable(retention) else retention
        shared = self._transport_mode() == "shared-folder" or (
            self._transport_mode() == "direct-lan"
            and bool(self._text(self.shared_folder_edit))
        )
        self.backup_interval_spin.enabled = bool(shared and enabled)
        self.backup_retention_spin.enabled = bool(shared and enabled)
        settings = qt.QSettings()
        settings.setValue(self.SETTINGS_PREFIX + "automaticBackups", bool(enabled))
        settings.setValue(
            self.SETTINGS_PREFIX + "backupIntervalMinutes",
            int(interval),
        )
        settings.setValue(
            self.SETTINGS_PREFIX + "backupRetention",
            int(retention),
        )
        if not enabled:
            self.backup_status_label.setText("Automatic project backups are off.")
        else:
            effective_seconds, large_volume_safety = (
                self._automatic_backup_interval_seconds(float(interval) * 60.0)
            )
            if large_volume_safety:
                self.backup_status_label.setText(
                    "Large-dataset safety is active: complete .mrb backups are "
                    f"limited to every {int(effective_seconds // 60)} min and only "
                    f"start after {int(AUTO_BACKUP_IDLE_SECONDS)} s without edits. "
                    "Manual Back up now remains available."
                )
            else:
                self.backup_status_label.setText(
                    f"A complete .mrb project is saved every {int(interval)} min "
                    f"after {int(AUTO_BACKUP_IDLE_SECONDS)} s without edits; the "
                    f"newest {int(retention)} unpinned backups are kept."
                )

    def _source_volume_memory_bytes(self):
        try:
            volume_node = self.owner.get_volume_node()
            image = volume_node.GetImageData() if volume_node is not None else None
            return int(image.GetActualMemorySize()) * 1024 if image is not None else 0
        except Exception:
            return 0

    def _automatic_backup_interval_seconds(self, configured_seconds):
        configured_seconds = max(60.0, float(configured_seconds))
        large_volume = self._source_volume_memory_bytes() >= LARGE_VOLUME_BYTES
        if (
            large_volume
            and configured_seconds < LARGE_VOLUME_AUTO_BACKUP_INTERVAL_SECONDS
        ):
            return LARGE_VOLUME_AUTO_BACKUP_INTERVAL_SECONDS, True
        return configured_seconds, False

    def create_backup_now(self, checked=False):
        del checked
        if not self.connected or not _uses_shared_folder(self.client):
            self._show_error("Join a shared-folder room before creating a backup", popup=True)
            return
        self.backup_status_label.setText("Creating a complete project backup…")
        self._queue_action("backup_now")
        self.on_timer()

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
        segment_id = self._selected_segment_id()
        if segment_id:
            self._queue_action("request_access", segment_id=segment_id, message="")

    def transfer_selected_segment_owner(self, checked=False):
        del checked
        segment_id = self._selected_segment_id()
        target = self._combo_current_text(self.collaborator_combo)
        if segment_id and target:
            self._queue_action(
                "transfer_owner", segment_id=segment_id, target_user=target
            )

    def set_selected_segment_review_state(self, checked=False):
        del checked
        segment_id = self._selected_segment_id()
        if not segment_id:
            self._show_error("Select a label first", popup=True)
            return
        self._queue_action(
            "review_state",
            segment_id=segment_id,
            state=str(self._combo_current_data(self.review_state_combo)),
            note=self._text(self.review_note_edit),
        )

    def select_review_queue_label(self, item=None, column=0):
        del column
        item = item or self.review_queue_tree.currentItem()
        if item is None:
            return
        segment_id = str(item.data(0, 32) or "")
        if not segment_id:
            return
        for index in range(self.label_combo.count):
            if str(self.label_combo.itemData(index) or "") == segment_id:
                self.label_combo.setCurrentIndex(index)
                break
        self.owner.select_segment_in_editor(segment_id)

    def set_selected_user_role(self, checked=False):
        del checked
        target = self._combo_current_text(self.collaborator_combo)
        if target:
            self._queue_action(
                "set_role", target_user=target, role=str(self._combo_current_data(self.role_combo))
            )

    def refresh_advanced_state(self, checked=False):
        del checked
        self._force_advanced_refresh = True
        self.on_timer()

    def filter_history_tree(self, value=None):
        query = str(value if value is not None else self._text(self.history_filter_edit)).casefold()
        count = self.history_tree.topLevelItemCount
        count = count() if callable(count) else count
        for index in range(int(count)):
            item = self.history_tree.topLevelItem(index)
            column_count = self.history_tree.columnCount
            column_count = column_count() if callable(column_count) else column_count
            haystack = " ".join(
                str(item.text(column)) for column in range(int(column_count))
            ).casefold()
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
        self.on_timer()

    def compare_selected_revision(self, checked=False):
        del checked
        sequence = self._selected_history_sequence()
        if sequence is None:
            self._show_error("Select a revision in the timeline first", popup=True)
            return
        self.compare_revision_button.enabled = False
        self._queue_action("compare_revision", sequence=sequence)
        self.on_timer()

    def undo_last_shared_edit(self, checked=False):
        del checked
        target = next(
            (
                record
                for record in sorted(
                    self.history_records,
                    key=lambda item: int(item.get("sequence", 0)),
                    reverse=True,
                )
                if str(record.get("author") or "") == self.user_name
                and not record.get("system_snapshot")
            ),
            None,
        )
        if target is None:
            self._show_error("No shared edit by this user is available to undo", popup=True)
            return
        self.undo_shared_button.enabled = False
        self._queue_action("undo_operation", sequence=int(target["sequence"]))
        self.on_timer()

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
            self._preserve_added_segment_ids = True
            self.owner.apply_material_template(template)
            self._on_segmentation_modified()
        except Exception as exc:
            self._show_error(f"Could not apply material template: {exc}", popup=True)
        finally:
            self._preserve_added_segment_ids = False

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
            if transport == "shared-folder":
                location = self._text(self.shared_folder_edit)
            elif transport == "direct-lan":
                location = self._text(self.lan_url_edit)
            else:
                location = self._text(self.server_edit)
            payload = build_invitation(
                transport,
                self._text(self.room_edit),
                self._current_volume_signature(),
                location,
                getattr(self, "material_template_state", None),
                fallback_shared_folder=(
                    self._text(self.shared_folder_edit)
                    if transport == "direct-lan"
                    else None
                ),
                access_code=(
                    self._text(self.lan_access_code_edit)
                    if transport == "direct-lan"
                    else None
                ),
            )
            path = qt.QFileDialog.getSaveFileName(
                slicer.util.mainWindow(),
                "Export room invitation",
                "LiveSegmentation-room.livesegroom",
                "Live Segmentation room (*.livesegroom)",
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
            slicer.util.mainWindow(),
            "Import room invitation",
            "",
            "Live Segmentation room (*.livesegroom *.liveseg);;JSON (*.json)",
        )
        if not path:
            return
        self.import_invitation_path(path)

    def import_invitation_path(self, path):
        """Load an invitation from the UI or the registered file association."""
        import slicer

        try:
            invitation = parse_invitation(Path(str(path)).read_text(encoding="utf-8"))
            self.room_edit.setText(invitation["room_name"])
            if invitation["transport"] == "shared-folder":
                self.transport_combo.setCurrentIndex(0)
                self.shared_folder_edit.setEditText(invitation["location"])
            elif invitation["transport"] == "direct-lan":
                self.transport_combo.setCurrentIndex(1)
                self.lan_url_edit.setText(invitation["location"])
                self.lan_access_code_edit.setText(invitation.get("access_code") or "")
                self.shared_folder_edit.setEditText(
                    invitation.get("fallback_shared_folder") or ""
                )
                self.lan_host_checkbox.checked = False
            else:
                self.transport_combo.setCurrentIndex(2)
                self.server_edit.setText(invitation["location"])
            self.invitation_volume_signature = invitation["volume_signature"]
            self.material_template_state = invitation.get("material_template")
            self._update_transport_fields()
            slicer.util.showStatusMessage("Live Segmentation invitation loaded", 3000)
        except Exception as exc:
            self._show_error(f"Could not import invitation: {exc}", popup=True)

    def _allow_insecure_server_http(self):
        checked = self.allow_insecure_http_checkbox.checked
        return bool(checked() if callable(checked) else checked)

    def _build_connection_client(self, user_name, transport_mode):
        """Build the selected transport without joining or touching room state."""
        if transport_mode == "shared-folder":
            location = self._text(self.shared_folder_edit)
            return SharedFolderRoomClient(location, user_name), location
        if transport_mode == "direct-lan":
            host_locally = self.lan_host_checkbox.checked
            host_locally = host_locally() if callable(host_locally) else host_locally
            if host_locally and self.lan_server is None:
                self.toggle_lan_host()
                if self.lan_server is None:
                    raise ValueError("The local LAN relay could not be started")
            lan_url = self._text(self.lan_url_edit)
            lan_client = LanRoomClient(
                lan_url,
                user_name,
                self._text(self.lan_access_code_edit),
            )
            shared_folder = self._text(self.shared_folder_edit)
            fallback_client = (
                SharedFolderRoomClient(shared_folder, user_name)
                if shared_folder
                else None
            )
            return HybridRoomClient(lan_client, fallback_client), "\0".join(
                (lan_url, shared_folder)
            )
        server_url = validate_remote_server_url(
            self._text(self.server_edit),
            allow_insecure_http=self._allow_insecure_server_http(),
        )
        return (
            LiveRoomClient(
                server_url,
                user_name,
                self._text(self.api_key_edit),
            ),
            server_url,
        )

    @staticmethod
    def _preflight_lane(session_token, result_queue, client, room_name, signature):
        started = time.monotonic()
        try:
            report = client.preflight(
                room_name,
                signature,
                PLUGIN_VERSION,
                COLLABORATION_PROTOCOL_VERSION,
            )
            result_queue.put(
                {
                    "lane": "preflight",
                    "session_token": session_token,
                    "report": report,
                    "duration": time.monotonic() - started,
                }
            )
        except Exception as exc:
            result_queue.put(
                {
                    "lane": "preflight",
                    "session_token": session_token,
                    "error": str(exc),
                    "duration": time.monotonic() - started,
                }
            )

    def run_connection_preflight(self, checked=False):
        del checked
        if self._preflight_running or self.connected or self._joining:
            return
        try:
            user_name = self._text(self.user_edit)
            if not user_name:
                raise ValueError("Enter your display name")
            room_name = self._text(self.room_edit)
            if not room_name:
                raise ValueError("Enter a room name that all collaborators will use")
            signature = self._current_volume_signature()
            client, _location = self._build_connection_client(
                user_name, self._transport_mode()
            )
            self._preflight_running = True
            self._preflight_started_at = time.monotonic()
            self._set_connection_inputs_enabled(False)
            self.join_button.enabled = False
            self.preflight_button.setText("Checking…")
            self.preflight_text.setPlainText(
                "Checking connection, permissions, protocol, source volume, clock, and peer visibility…"
            )
            self.status_label.setText("● Running connection check in background…")
            self.status_label.setStyleSheet("color: #b26a00;")
            self.timer.start()
            self._preflight_worker = threading.Thread(
                target=self._preflight_lane,
                args=(
                    self._session_token,
                    self._worker_results,
                    client,
                    room_name,
                    signature,
                ),
                name="LiveSegmentation-preflight",
                daemon=True,
            )
            self._preflight_worker.start()
        except Exception as exc:
            self._preflight_running = False
            self._preflight_started_at = 0.0
            self._set_connection_inputs_enabled(True)
            self.join_button.enabled = True
            self.preflight_button.setText("Check connection")
            self._show_error(f"Connection check could not start: {exc}", popup=True)

    def _show_preflight_report(self, report):
        status = str(report.get("status") or "fail")
        status_label = {
            "pass": "READY",
            "warning": "READY WITH WARNINGS",
            "fail": "NOT READY",
        }.get(status, "NOT READY")
        lines = [
            f"{status_label} — {report.get('transport') or 'connection'}",
            "Run this check on both computers; rerun the first computer to confirm peer visibility.",
            "",
        ]
        symbols = {"pass": "[PASS]", "warning": "[WARN]", "fail": "[FAIL]"}
        for check in report.get("checks") or []:
            check_status = str(check.get("status") or "warning")
            lines.append(
                f"{symbols.get(check_status, '[WARN]')} {check.get('title')}: {check.get('detail')}"
            )
            if check.get("action"):
                lines.append(f"       Action: {check['action']}")
        self.preflight_text.setPlainText("\n".join(lines))
        if status == "pass":
            self.status_label.setText("● Connection check passed — ready to join")
            self.status_label.setStyleSheet("color: #188038; font-weight: bold;")
        elif status == "warning":
            self.status_label.setText("● Connection works, but review the warnings below")
            self.status_label.setStyleSheet("color: #b26a00; font-weight: bold;")
        else:
            self.status_label.setText("● Connection check failed — fix the items below")
            self.status_label.setStyleSheet("color: #c5221f; font-weight: bold;")

    def _set_connection_inputs_enabled(self, enabled):
        enabled = bool(enabled)
        self.user_edit.enabled = enabled
        self.room_edit.enabled = enabled
        self.transport_combo.enabled = enabled
        self.shared_folder_edit.enabled = enabled
        self.shared_folder_button.enabled = enabled
        self.shared_folder_clear_button.enabled = enabled
        self.server_edit.enabled = enabled
        self.api_key_edit.enabled = enabled
        self.allow_insecure_http_checkbox.enabled = enabled
        self.lan_url_edit.enabled = enabled
        self.lan_access_code_edit.enabled = enabled
        self.lan_host_checkbox.enabled = enabled
        self.lan_port_spin.enabled = enabled
        self.lan_host_button.enabled = enabled
        self.preflight_button.enabled = enabled and not self._preflight_running

    @staticmethod
    def _leave_client_in_background(client, room_id):
        if client is None or not room_id:
            return

        def leave_client():
            try:
                client.leave(room_id)
            except Exception:
                pass

        threading.Thread(
            target=leave_client,
            name="LiveSegmentation-leave",
            daemon=True,
        ).start()

    def _cancel_join(self, message=None):
        if not self._joining:
            return
        self._session_token += 1
        self._joining = False
        self._join_started_at = 0.0
        self._join_status_second = -1
        self._join_context = None
        self._join_worker = None
        self._preflight_running = False
        self._preflight_worker = None
        self._preflight_started_at = 0.0
        self.connection_healthy = False
        self._set_connection_inputs_enabled(True)
        self.owner.set_live_inputs_enabled(True)
        self.owner.set_live_session_active(False)
        self.join_button.setText("Join live room")
        if hasattr(self, "timer"):
            self.timer.stop()
        if message:
            self._show_error(message, popup=True)
        else:
            self.status_label.setText("● Offline")
            self.status_label.setStyleSheet("color: #777;")

    def _join_lane(self, session_token, client, room_name, signature):
        started = time.monotonic()
        try:
            room = client.join(room_name, signature)
            self._worker_results.put(
                {
                    "lane": "join",
                    "session_token": session_token,
                    "client": client,
                    "room": room,
                    "duration": time.monotonic() - started,
                }
            )
        except Exception as exc:
            self._worker_results.put(
                {
                    "lane": "join",
                    "session_token": session_token,
                    "client": client,
                    "error": str(exc),
                    "duration": time.monotonic() - started,
                }
            )

    def join(self):
        import slicer

        if self._joining:
            return
        try:
            self._session_token += 1
            self._stop_shared_folder_watcher()
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
            client, connection_location = self._build_connection_client(
                user_name, transport_mode
            )
            self._join_context = {
                "user_name": user_name,
                "room_name": room_name,
                "transport_mode": transport_mode,
                "volume_shape": tuple(int(value) for value in volume_array.shape),
                "volume_signature": signature,
                "connection_identity": hashlib.sha256(
                    connection_location.encode("utf-8")
                ).hexdigest()[:24],
            }
            self._joining = True
            self._join_started_at = time.monotonic()
            self._join_status_second = -1
            self.connection_healthy = False
            self._worker_results = queue.Queue()
            self._set_connection_inputs_enabled(False)
            self.owner.set_live_inputs_enabled(False)
            self.join_button.setText("Cancel connection")
            self.status_label.setText("● Connecting in background…")
            self.status_label.setStyleSheet("color: #b26a00;")
            self.timer.start()
            self._join_worker = threading.Thread(
                target=self._join_lane,
                args=(self._session_token, client, room_name, signature),
                name="LiveSegmentation-join",
                daemon=True,
            )
            self._join_worker.start()
        except Exception as exc:
            self._joining = False
            self._join_started_at = 0.0
            self._join_status_second = -1
            self._join_context = None
            self.connection_healthy = False
            self._set_connection_inputs_enabled(True)
            self.owner.set_live_inputs_enabled(True)
            self.owner.set_live_session_active(False)
            self.join_button.setText("Join live room")
            self._show_error(exc, popup=True)

    def _finish_join(self, client, room):
        import qt

        context = dict(self._join_context or {})
        try:
            segmentation_node = self.owner.prepare_shared_segmentation(room)
            if segmentation_node is None:
                raise ValueError("Could not create the shared segmentation")

            self._joining = False
            self._join_started_at = 0.0
            self._join_status_second = -1
            self._join_worker = None
            self._join_context = None
            self.client = client
            self.room_id = room["id"]
            self.room_name = str(room["name"])
            self.user_name = context["user_name"]
            self.segmentation_node_id = segmentation_node.GetID()
            self.volume_shape = tuple(context["volume_shape"])
            self.source_volume_signature = str(context["volume_signature"])
            self.last_sequence = 0
            self.initial_sequence = int(room.get("latest_sequence", 0))
            self.initial_sync_complete = self.initial_sequence == 0
            self.baselines.clear()
            self.baseline_bounds.clear()
            self.dirty_segments.clear()
            self.force_snapshots.clear()
            self.metadata_updates.clear()
            self.outgoing.clear()
            self.outgoing_keys.clear()
            self.awaiting_echo.clear()
            self._applied_local_operation_ids.clear()
            self._segment_metadata.clear()
            self.pending_segment_deletions.clear()
            self._segment_revisions.clear()
            self._segment_verifications.clear()
            self.last_chat_sequence = 0
            self.pending_chat.clear()
            self.displayed_chat_sequences.clear()
            self.displayed_chat_ids.clear()
            self.optimistic_chat_ids.clear()
            self.chat_history.clear()
            if self.chat_dock_history is not None:
                self.chat_dock_history.clear()
            self.segment_owners.clear()
            self.segment_locks_state.clear()
            self.pending_lock_changes.clear()
            self.presence_by_user.clear()
            self._presence_last_observed.clear()
            self._presence_worker_started_at = 0.0
            self._presence_stall_status_second = -1
            self.room_roles_state.clear()
            self.review_states_state.clear()
            self.access_requests_state.clear()
            self.chat_anchors.clear()
            self.history_records.clear()
            self.history_sequences.clear()
            self.conflicts_state.clear()
            self.backup_records.clear()
            self.last_diagnostics = None
            self.last_benchmark = None
            self.last_quality_report = None
            self.session_metrics = SessionMetrics()
            self._last_hybrid_fallback_count = int(
                client.fallback_count if isinstance(client, HybridRoomClient) else 0
            )
            self.comments.clear()
            self.resolved_comment_ids.clear()
            self.comment_tree.clear()
            self.review_queue_tree.clear()
            self.quality_text.clear()
            if self.activity_dock_text is not None:
                self.activity_dock_text.clear()
            self.pending_actions.clear()
            self._last_advanced_fetch = 0.0
            self._last_snapshot_sequence = 0
            self._snapshot_requested = False
            self._snapshot_label = ""
            self._restoring_sequence = None
            self._worker = None
            self._realtime_worker = None
            self._edit_push_worker = None
            self._edit_pull_worker = None
            self._chat_send_worker = None
            self._chat_pull_worker = None
            self._presence_worker = None
            self._lock_set_worker = None
            self._lock_pull_worker = None
            self._maintenance_worker = None
            self._last_presence_send = 0.0
            self._presence_worker_started_at = 0.0
            self._presence_stall_status_second = -1
            self._presence_last_observed.clear()
            self._last_metadata_fetch = 0.0
            self._last_sync_poll = 0.0
            self._last_chat_poll = 0.0
            self._last_lock_poll = 0.0
            self._last_health_check = 0.0
            self._last_backup_check = 0.0
            self._joined_at_epoch = time.time()
            self._last_edit_activity_epoch = self._joined_at_epoch
            self._force_sync_refresh = True
            self._force_realtime_refresh = True
            self._force_health_check = True
            self._force_advanced_refresh = True
            self._connection_error_popup_shown = False
            self._connection_validation_pending = False
            self._connection_validation_started_at = 0.0
            self._last_transport_warning = None
            self._last_transport_result_at = time.monotonic()
            self._transport_stall_status_second = -1
            self.connection_healthy = True
            self.connected = True
            self._observe_segmentation(segmentation_node)
            self._known_segment_ids = set(segmentation_node.GetSegmentation().GetSegmentIDs())
            for segment_id in self._known_segment_ids:
                self._remember_segment_metadata(segmentation_node, segment_id)
            if self.initial_sync_complete:
                self._initialize_baselines_and_seed(seed=bool(room.get("created")))

            settings = qt.QSettings()
            settings.setValue(self.SETTINGS_PREFIX + "user", self.user_name)
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
            settings.sync()
            self._set_connection_inputs_enabled(False)
            shared_folder_room = _uses_shared_folder(client)
            if shared_folder_room:
                self._remember_recent_shared_folder(str(client.shared_folder))
            self._journal_context = {
                "user_name": self.user_name,
                "room_name": self.room_name,
                "transport_mode": context["transport_mode"],
                "volume_signature": context["volume_signature"],
                "connection_identity": context["connection_identity"],
            }
            self._operation_journal = self._journal_for_context(
                self._journal_context
            )
            self._journal_recovery = self._operation_journal.read(
                self._journal_context
            )
            has_recovery = bool(self._journal_recovery)
            self.recover_edits_button.enabled = has_recovery
            self.discard_recovery_button.enabled = has_recovery
            self.recovery_status_label.setText(
                f"Found {len(self._journal_recovery)} unacknowledged edit(s) from a previous session."
                if has_recovery
                else "No recoverable edits for the active room"
            )
            self.backup_enabled_checkbox.enabled = shared_folder_room
            self._on_backup_settings_changed()
            self.owner.set_live_inputs_enabled(False)
            self.owner.set_live_session_active(True)
            self.join_button.setText("Leave live room")
            self.refresh_button.enabled = True
            self.chat_input.enabled = True
            self.chat_send_button.enabled = True
            self.comment_input.enabled = True
            self.comment_button.enabled = True
            if self.chat_dock_input is not None:
                self.chat_dock_input.enabled = True
                self.chat_dock_send_button.enabled = True
                self.chat_dock.setWindowTitle(f"Live Segmentation Chat — {self.room_name}")
                self.show_chat_dock()
            self.label_combo.enabled = True
            self.lock_expiry_spin.enabled = True
            self.refresh_history_button.enabled = True
            self.restore_revision_button.enabled = True
            self.compare_revision_button.enabled = True
            self.undo_shared_button.enabled = True
            self.create_snapshot_button.enabled = True
            self.run_diagnostics_button.enabled = True
            self.export_diagnostics_button.enabled = True
            self.export_metrics_button.enabled = True
            self.run_quality_button.enabled = True
            self.benchmark_button.enabled = True
            self.export_invite_button.enabled = True
            self.publish_template_button.enabled = True
            self.apply_template_button.enabled = True
            if shared_folder_room:
                for button in (
                    self.backup_now_button,
                    self.refresh_backups_button,
                    self.pin_backup_button,
                    self.verify_backup_button,
                    self.restore_backup_button,
                ):
                    button.enabled = True
            self._refresh_label_combo()
            if self.initial_sync_complete:
                self.status_label.setText(self._live_status_text())
                self.status_label.setStyleSheet("color: #188038; font-weight: bold;")
            else:
                self.status_label.setText(self._initial_sync_status_text())
                self.status_label.setStyleSheet("color: #b26a00; font-weight: bold;")
            self._update_presence(room.get("presence") or [])
            self._append_activity(
                f"Joined room {self.room_name} via {context['transport_mode']}"
            )
            self.show_activity_dock()
            self.timer.start()
        except Exception as exc:
            self._leave_client_in_background(client, room.get("id"))
            self._joining = False
            self._join_started_at = 0.0
            self._join_status_second = -1
            self.connected = False
            self.connection_healthy = False
            self._join_context = None
            self._set_connection_inputs_enabled(True)
            self.owner.set_live_inputs_enabled(True)
            self.owner.set_live_session_active(False)
            self.join_button.setText("Join live room")
            if hasattr(self, "timer"):
                self.timer.stop()
            try:
                self.owner.clear_live_segmentation()
            except Exception:
                pass
            self._show_error(exc, popup=True)

    def leave(self, notify_remote=True):
        client = self.client
        room_id = self.room_id
        segmentation_node_id = self.segmentation_node_id
        if notify_remote:
            if self._operation_journal is not None:
                self._operation_journal.clear()
        else:
            self._sync_operation_journal()
        self._joining = False
        self._join_started_at = 0.0
        self._join_status_second = -1
        self._join_context = None
        self._join_worker = None
        self._preflight_running = False
        self._preflight_worker = None
        self._preflight_started_at = 0.0
        self.connected = False
        self.connection_healthy = False
        self._connection_validation_pending = False
        self._connection_validation_started_at = 0.0
        self._session_token += 1
        if hasattr(self, "timer"):
            self.timer.stop()
        self._stop_shared_folder_watcher()
        self._unobserve_segmentation()
        if notify_remote:
            # An unreachable SMB share may keep a filesystem call blocked for
            # minutes.  Local teardown must always finish immediately; the
            # courtesy presence/audit cleanup is best-effort on a daemon lane.
            self._leave_client_in_background(client, room_id)
        self.client = None
        self.room_id = None
        self.room_name = None
        self.user_name = None
        self.segmentation_node_id = None
        self.volume_shape = None
        self.source_volume_signature = None
        self.last_sequence = 0
        self.initial_sequence = 0
        self.initial_sync_complete = False
        self._joined_at_epoch = 0.0
        self._last_edit_activity_epoch = 0.0
        self.baselines.clear()
        self.baseline_bounds.clear()
        self.dirty_segments.clear()
        self.force_snapshots.clear()
        self.metadata_updates.clear()
        self.outgoing.clear()
        self.outgoing_keys.clear()
        self.awaiting_echo.clear()
        self._applied_local_operation_ids.clear()
        self._segment_metadata.clear()
        self._preserve_added_segment_ids = False
        self.pending_segment_deletions.clear()
        self._segment_revisions.clear()
        self._segment_verifications.clear()
        self._known_segment_ids.clear()
        self.pending_chat.clear()
        self.displayed_chat_sequences.clear()
        self.displayed_chat_ids.clear()
        self.optimistic_chat_ids.clear()
        self.last_chat_sequence = 0
        self.segment_owners.clear()
        self.segment_locks_state.clear()
        self.pending_lock_changes.clear()
        self.presence_by_user.clear()
        self._presence_last_observed.clear()
        self._presence_worker_started_at = 0.0
        self._presence_stall_status_second = -1
        self.room_roles_state.clear()
        self.review_states_state.clear()
        self.access_requests_state.clear()
        self.chat_anchors.clear()
        self.history_records.clear()
        self.history_sequences.clear()
        self.conflicts_state.clear()
        self.material_template_state = None
        self.backup_records.clear()
        self.last_diagnostics = None
        self.last_benchmark = None
        self.last_quality_report = None
        self.pending_actions.clear()
        self._last_snapshot_sequence = 0
        self._snapshot_requested = False
        self._snapshot_label = ""
        self._restoring_sequence = None
        self._worker_results = queue.Queue()
        self._force_sync_refresh = False
        self._force_realtime_refresh = False
        self._force_health_check = False
        self._force_advanced_refresh = False
        self._connection_error_popup_shown = False
        self._last_transport_result_at = 0.0
        self._transport_stall_status_second = -1
        self.comments.clear()
        self.resolved_comment_ids.clear()
        self._journal_recovery = []
        self._journal_context = None
        self._operation_journal = None
        self._remove_comparison_node()
        if self.lan_server is not None:
            self.lan_server.stop()
            self.lan_server = None
            self.lan_host_button.setText("Start LAN host")
            self.lan_host_checkbox.checked = False
            self.lan_status_label.setText("LAN host stopped")
        try:
            self.owner.clear_live_segmentation(segmentation_node_id)
        except Exception:
            pass
        self._set_connection_inputs_enabled(True)
        self.preflight_button.setText("Check connection")
        self.backup_enabled_checkbox.enabled = True
        self._on_backup_settings_changed()
        self.owner.set_live_inputs_enabled(True)
        self.owner.set_live_session_active(False)
        self._update_transport_fields()
        self.join_button.setText("Join live room")
        self.refresh_button.enabled = False
        self.chat_input.enabled = False
        self.chat_send_button.enabled = False
        self.comment_input.enabled = False
        self.comment_button.enabled = False
        self.comment_input.clear()
        self.comment_tree.clear()
        self.jump_to_comment_button.enabled = False
        self.resolve_comment_button.enabled = False
        self.chat_history.clear()
        self.chat_input.clear()
        if self.chat_dock_history is not None:
            self.chat_dock_history.clear()
            self.chat_dock_input.clear()
            self.chat_dock_input.enabled = False
            self.chat_dock_send_button.enabled = False
            self.chat_dock.setWindowTitle("Live Segmentation Chat")
            self.chat_dock.hide()
        self.chat_location_combo.clear()
        self.collaborator_combo.clear()
        self.label_combo.clear()
        self.label_combo.enabled = False
        self.history_tree.clear()
        self.conflict_tree.clear()
        self.review_queue_tree.clear()
        self.backup_tree.clear()
        self.diagnostics_text.clear()
        self.preflight_text.clear()
        self.quality_text.clear()
        self.performance_label.setText("No live performance samples yet")
        if self.activity_dock_text is not None:
            self.activity_dock_text.clear()
        if self.activity_dock is not None:
            self.activity_dock.hide()
        self.recover_edits_button.enabled = False
        self.discard_recovery_button.enabled = False
        self.recovery_status_label.setText("No recoverable edits for the active room")
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
            self.compare_revision_button,
            self.undo_shared_button,
            self.create_snapshot_button,
            self.backup_now_button,
            self.refresh_backups_button,
            self.pin_backup_button,
            self.verify_backup_button,
            self.restore_backup_button,
        ):
            button.enabled = False
        for button in (
            self.benchmark_button,
            self.run_diagnostics_button,
            self.export_diagnostics_button,
            self.export_metrics_button,
            self.run_quality_button,
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
        # Preserve unacknowledged edits if Slicer exits or the module is
        # destroyed unexpectedly. An explicit Leave remains the clean discard.
        self.leave(notify_remote=False)
        try:
            import qt

            settings = qt.QSettings()
            for key in ("room", "transport", "sharedFolder", "server"):
                settings.remove(self.SETTINGS_PREFIX + key)
            settings.sync()
            self.room_edit.clear()
            self.shared_folder_edit.setCurrentIndex(-1)
            self.shared_folder_edit.setEditText("")
            self.server_edit.setText("http://127.0.0.1:8000")
            self.transport_combo.setCurrentIndex(0)
        except Exception:
            pass
        if self.chat_dock is not None:
            self.chat_dock.setParent(None)
            self.chat_dock.deleteLater()
            self.chat_dock = None
            self.chat_dock_history = None
            self.chat_dock_input = None
            self.chat_dock_send_button = None
            self.chat_dock_anchor_checkbox = None
        if self.activity_dock is not None:
            self.activity_dock.setParent(None)
            self.activity_dock.deleteLater()
            self.activity_dock = None
            self.activity_dock_text = None

    def _segmentation_node(self):
        import slicer

        return slicer.mrmlScene.GetNodeByID(self.segmentation_node_id) if self.segmentation_node_id else None

    def _observe_segmentation(self, node):
        import vtk
        import vtkSegmentationCorePython as vtkSegmentationCore

        self._unobserve_segmentation()
        self._observed_node = node
        self._observed_segmentation = node.GetSegmentation()
        string_events = (
            vtkSegmentationCore.vtkSegmentation.SegmentAdded,
            vtkSegmentationCore.vtkSegmentation.SegmentRemoved,
            vtkSegmentationCore.vtkSegmentation.SegmentModified,
        )
        for event_id in string_events:
            @vtk.calldata_type(vtk.VTK_STRING)
            def segment_callback(caller, event, segment_id, controller=self):
                controller._on_segmentation_modified(
                    caller, event, str(segment_id or "")
                )

            self._observer_callbacks.append(segment_callback)
            self._observer_tags.append(
                self._observed_segmentation.AddObserver(
                    event_id, segment_callback
                )
            )
        @vtk.calldata_type(vtk.VTK_STRING)
        def source_callback(caller, event, segment_id, controller=self):
            # vtkSegmentation provides the exact segment ID as call data.  It
            # is essential here: several segments may share one internal
            # binary-labelmap layer and therefore the same VTK modification
            # time, but only this segment was edited by Segment Editor.
            controller._on_segmentation_modified(
                caller, event, str(segment_id or "")
            )

        self._observer_callbacks.append(source_callback)
        self._observer_tags.append(
            self._observed_segmentation.AddObserver(
                vtkSegmentationCore.vtkSegmentation.SourceRepresentationModified,
                source_callback,
            )
        )

    @staticmethod
    def _segment_revision(node, segment_id):
        """Return a cheap revision token for a segment's binary labelmap."""
        if node is None:
            return None
        segment = node.GetSegmentation().GetSegment(str(segment_id))
        if segment is None:
            return None
        try:
            import vtkSegmentationCorePython as vtkSegmentationCore

            binary_name = (
                vtkSegmentationCore.vtkSegmentationConverter.GetSegmentationBinaryLabelmapRepresentationName()
            )
            image = segment.GetRepresentation(binary_name)
            if image is None:
                return None
            scalars = image.GetPointData().GetScalars()
            return (
                int(image.GetMTime()),
                int(scalars.GetMTime()) if scalars is not None else -1,
                tuple(int(value) for value in image.GetExtent()),
            )
        except Exception:
            return None

    @staticmethod
    def _segment_representation_key(node, segment_id):
        """Return the native identity of a segment's binary-labelmap layer."""
        if node is None:
            return None
        try:
            import vtkSegmentationCorePython as vtkSegmentationCore

            segment = node.GetSegmentation().GetSegment(str(segment_id))
            if segment is None:
                return None
            binary_name = (
                vtkSegmentationCore.vtkSegmentationConverter.GetSegmentationBinaryLabelmapRepresentationName()
            )
            image = segment.GetRepresentation(binary_name)
            if image is None:
                return None
            try:
                return str(image.GetAddressAsString(""))
            except Exception:
                return str(getattr(image, "__this__", "")) or None
        except Exception:
            return None

    def _shared_representation_ids(self, node):
        """Return IDs whose geometry revision belongs to a shared VTK layer."""
        groups = {}
        for segment_id in node.GetSegmentation().GetSegmentIDs():
            segment_id = str(segment_id)
            key = self._segment_representation_key(node, segment_id)
            if key:
                groups.setdefault(key, []).append(segment_id)
        return {
            segment_id
            for members in groups.values()
            if len(members) > 1
            for segment_id in members
        }

    def _ensure_independent_segment_labelmap(self, node, segment_id):
        """Move a live label to its own layer when Slicer currently shares it."""
        if node is None:
            return False
        segment_id = str(segment_id or "")
        if not segment_id:
            return False
        representation_key = self._segment_representation_key(node, segment_id)
        if not representation_key:
            return False
        shared = any(
            str(other_id) != segment_id
            and self._segment_representation_key(node, other_id)
            == representation_key
            for other_id in node.GetSegmentation().GetSegmentIDs()
        )
        if not shared:
            return False
        previous_applying_remote = self._applying_remote
        self._applying_remote = True
        try:
            separate = getattr(
                node.GetSegmentation(), "SeparateSegmentLabelmap", None
            )
            if not callable(separate):
                return False
            result = separate(segment_id)
            return result is not False
        finally:
            self._applying_remote = previous_applying_remote

    def _remember_segment_revision(self, node, segment_id):
        if node is None:
            return
        key = (node.GetID(), str(segment_id))
        self._segment_revisions[key] = self._segment_revision(node, segment_id)

    def _current_segment_metadata(self, node, segment_id):
        """Read current display metadata without mutating the local baseline."""
        segment_id = str(segment_id or "")
        if node is None or not segment_id:
            return None
        segment = node.GetSegmentation().GetSegment(segment_id)
        if segment is None:
            return None
        return {
            "segment_id": segment_id,
            "segment_name": segment.GetName() or segment_id,
            "color_hex": self._segment_color_hex(segment),
        }

    def _remember_segment_metadata(self, node, segment_id):
        """Cache display metadata while a segment still exists.

        Slicer's SegmentRemoved callback runs after the segment object has gone,
        so a deletion tombstone must use metadata captured earlier.
        """
        segment_id = str(segment_id or "")
        if node is None or not segment_id:
            return self._segment_metadata.get(segment_id)
        metadata = self._current_segment_metadata(node, segment_id)
        if metadata is None:
            return self._segment_metadata.get(segment_id)
        self._segment_metadata[segment_id] = metadata
        return metadata

    def _adopt_unique_segment_id(self, node, segment_id):
        """Replace a newly created local scene ID with a room-global ID."""
        segment_id = str(segment_id or "")
        if node is None or not segment_id or self._segment_id_is_globally_unique(
            segment_id
        ):
            return segment_id
        segmentation = node.GetSegmentation()
        segment = segmentation.GetSegment(segment_id)
        if segment is None:
            return segment_id
        replacement_id = new_collaboration_segment_id()
        self._applying_remote = True
        try:
            segmentation.RemoveSegment(segment_id)
            try:
                added = segmentation.AddSegment(segment, replacement_id)
            except Exception:
                segmentation.AddSegment(segment, segment_id)
                raise
            if added is False:
                segmentation.AddSegment(segment, segment_id)
                return segment_id
            try:
                segment.SetTag("LiveSegmentation.GlobalID", replacement_id)
            except Exception:
                pass
        finally:
            self._applying_remote = False
        try:
            self.owner.select_segment_in_editor(replacement_id)
        except Exception:
            pass
        return replacement_id

    @staticmethod
    def _segment_id_is_globally_unique(segment_id):
        """Recognize IDs that are already safe to publish to a shared room.

        Current Slicer releases normally create decimal 2.25 UUID OIDs.  Those
        IDs are globally unique and must not be removed/reinserted merely to add
        a LiveSeg prefix.  UUID strings and IDs generated by this plugin are
        likewise safe.  Legacy scene-local IDs such as ``Segment_1`` still get
        upgraded, but only after the originating VTK event has fully returned.
        """
        value = str(segment_id or "").strip()
        if not value:
            return False
        if value.startswith("LiveSeg-"):
            return True
        if re.fullmatch(r"2\.25\.[1-9]\d*", value):
            return True
        try:
            uuid.UUID(value.strip("{}"))
            return True
        except (AttributeError, TypeError, ValueError):
            return False

    def _schedule_segment_verification(self, key, duration=2.0):
        """Recheck a recently edited label after early Segment Editor events."""
        now = time.monotonic()
        existing = self._segment_verifications.get(key)
        if existing is None:
            self._segment_verifications[key] = {
                "deadline": now + float(duration),
                "next": now + 0.05,
                "delay": 0.08,
            }
            return
        existing["deadline"] = max(
            float(existing.get("deadline", 0.0)), now + float(duration)
        )
        existing["next"] = min(float(existing.get("next", now)), now + 0.05)
        existing["delay"] = min(float(existing.get("delay", 0.08)), 0.08)

    def _queue_due_segment_verifications(self, now):
        """Queue sparse trailing comparisons until an interactive edit settles."""
        for key, state in list(self._segment_verifications.items()):
            if now < float(state.get("next", 0.0)):
                continue
            self.dirty_segments.add(key)
            if now >= float(state.get("deadline", 0.0)):
                self._segment_verifications.pop(key, None)
                continue
            delay = min(max(float(state.get("delay", 0.08)) * 2.0, 0.12), 0.60)
            state["delay"] = delay
            state["next"] = now + delay

    def _probe_segment_revisions(self):
        """Find changed labels by their own data revisions, never by UI selection.

        Several Slicer effects emit ``SourceRepresentationModified`` without a
        segment ID. The active row can already point at another label when that
        deferred event is handled. Treating the active row as the event source
        can therefore publish Brain voxels under Mandibles. Cheap per-segment
        revision tokens keep event and label identity independent.
        """
        if self._applying_remote or not self.initial_sync_complete:
            return
        node = self._segmentation_node()
        if node is None:
            return
        shared_representation_ids = self._shared_representation_ids(node)
        for segment_id in node.GetSegmentation().GetSegmentIDs():
            segment_id = str(segment_id)
            key = (node.GetID(), segment_id)
            current_metadata = self._current_segment_metadata(node, segment_id)
            previous_metadata = self._segment_metadata.get(segment_id)
            if (
                current_metadata is not None
                and previous_metadata is not None
                and current_metadata != previous_metadata
            ):
                self._segment_metadata[segment_id] = current_metadata
                self.metadata_updates.add(key)
                self.dirty_segments.add(key)
                self._force_sync_refresh = True
            revision = self._segment_revision(node, segment_id)
            previous = self._segment_revisions.get(key)
            if (
                segment_id not in shared_representation_ids
                and revision is not None
                and previous is not None
                and revision != previous
            ):
                self.dirty_segments.add(key)
                self._schedule_segment_verification(key)
                self._force_sync_refresh = True

    def _stop_shared_folder_watcher(self):
        watcher = self._shared_folder_watcher
        self._shared_folder_watcher = None
        if watcher is None:
            return
        try:
            watcher.blockSignals(True)
            # Querying or removing a dead UNC path can block Qt's GUI thread.
            # Destroying the watcher drops all registrations without touching
            # the remote filesystem again.
            watcher.deleteLater()
        except Exception:
            pass

    def _arm_shared_folder_watcher(self):
        """Keep all shared-folder probing away from Qt's GUI thread.

        QFileSystemWatcher can synchronously touch a disconnected SMB/UNC path
        both when arming and when removing a watch.  The 75 ms timer already
        drives background pull lanes, so a watcher is unnecessary for latency
        and unsafe for application startup/shutdown.
        """
        self._stop_shared_folder_watcher()

    def _on_shared_folder_changed(self, path=None):
        del path
        if not self.connected:
            return
        self._force_sync_refresh = True
        self._arm_shared_folder_watcher()
        try:
            import qt

            qt.QTimer.singleShot(0, self.on_timer)
        except Exception:
            pass

    def _unobserve_segmentation(self):
        self._segmentation_event_generation += 1
        self._segmentation_event_flush_scheduled = False
        self._pending_segmentation_event_ids.clear()
        self._pending_segmentation_event_unspecified = False
        self._pending_segmentation_preserve_added_ids = False
        if self._observed_segmentation is not None:
            for observer_tag in self._observer_tags:
                try:
                    self._observed_segmentation.RemoveObserver(observer_tag)
                except Exception:
                    pass
        self._observed_node = None
        self._observed_segmentation = None
        self._observer_tags = []
        self._observer_callbacks = []
        self._segment_revisions.clear()

    def _on_segmentation_modified(self, caller=None, event=None, segment_id=None):
        del caller, event
        if self._applying_remote or not self.connected:
            return
        self._last_edit_activity_epoch = time.time()
        segment_id = str(segment_id or "")
        if segment_id:
            self._pending_segmentation_event_ids.add(segment_id)
        else:
            self._pending_segmentation_event_unspecified = True
        if self._preserve_added_segment_ids:
            self._pending_segmentation_preserve_added_ids = True
        if self._segmentation_event_flush_scheduled:
            return
        self._segmentation_event_flush_scheduled = True
        generation = self._segmentation_event_generation
        try:
            import qt

            qt.QTimer.singleShot(
                0,
                lambda queued_generation=generation: self._flush_segmentation_events(
                    queued_generation
                ),
            )
        except Exception:
            # Slicer always provides Qt.  If shutdown has already torn it down,
            # discard the pending callback instead of entering native MRML code
            # synchronously from the VTK observer.
            self._segmentation_event_flush_scheduled = False
            self._pending_segmentation_event_ids.clear()
            self._pending_segmentation_event_unspecified = False
            self._pending_segmentation_preserve_added_ids = False

    def _flush_segmentation_events(self, generation):
        """Process coalesced VTK notifications after native observers finish."""
        if generation != self._segmentation_event_generation:
            return
        self._segmentation_event_flush_scheduled = False
        event_segment_ids = set(self._pending_segmentation_event_ids)
        unspecified_event = self._pending_segmentation_event_unspecified
        preserve_added_ids = self._pending_segmentation_preserve_added_ids
        self._pending_segmentation_event_ids.clear()
        self._pending_segmentation_event_unspecified = False
        self._pending_segmentation_preserve_added_ids = False
        if self._applying_remote or not self.connected:
            return
        node = self._segmentation_node()
        if (
            node is None
            or self._observed_node is None
            or node.GetID() != self._observed_node.GetID()
            or self._observed_segmentation is None
        ):
            return
        self._process_segmentation_events(
            node, event_segment_ids, unspecified_event, preserve_added_ids
        )

    def _process_segmentation_events(
        self,
        node,
        event_segment_ids,
        unspecified_event=False,
        preserve_added_ids=False,
    ):
        """Update collaboration state outside the native VTK event stack."""
        current_ids = set(node.GetSegmentation().GetSegmentIDs())
        previous_ids = set(self._known_segment_ids)
        added_ids = current_ids - previous_ids
        if added_ids and not preserve_added_ids:
            for added_segment_id in sorted(added_ids):
                self._adopt_unique_segment_id(node, added_segment_id)
            current_ids = set(node.GetSegmentation().GetSegmentIDs())
            added_ids = current_ids - previous_ids
        removed_ids = previous_ids - current_ids
        ids_changed = bool(added_ids or removed_ids)
        metadata_changed_ids = set()
        for current_segment_id in current_ids:
            previous_metadata = self._segment_metadata.get(current_segment_id)
            current_metadata = self._current_segment_metadata(node, current_segment_id)
            if (
                current_metadata is not None
                and previous_metadata is not None
                and current_metadata != previous_metadata
            ):
                metadata_changed_ids.add(current_segment_id)
            if current_metadata is not None:
                self._segment_metadata[current_segment_id] = current_metadata
        for added_segment_id in added_ids:
            added_key = (node.GetID(), added_segment_id)
            self.force_snapshots.add(added_key)
            self.metadata_updates.add(added_key)
            self.segment_owners.setdefault(added_segment_id, self.user_name)
        for changed_segment_id in metadata_changed_ids:
            changed_key = (node.GetID(), changed_segment_id)
            self.metadata_updates.add(changed_key)
            self.dirty_segments.add(changed_key)
        for removed_segment_id in removed_ids:
            key = (node.GetID(), removed_segment_id)
            metadata = dict(
                self._segment_metadata.get(removed_segment_id)
                or {
                    "segment_id": removed_segment_id,
                    "segment_name": removed_segment_id,
                    "color_hex": "#4A90E2",
                }
            )
            if self.initial_sync_complete:
                self.pending_segment_deletions[removed_segment_id] = metadata
            self.dirty_segments.discard(key)
            self.force_snapshots.discard(key)
            self.metadata_updates.discard(key)
            self._segment_revisions.pop(key, None)
            self._segment_verifications.pop(key, None)
        self._known_segment_ids = current_ids
        event_segment_ids = {
            str(value) for value in (event_segment_ids or ()) if str(value or "")
        }
        # Newly painted segments are commonly packed into an existing binary
        # labelmap layer.  Separate only after Slicer's native callback has
        # returned; doing it inside that stack can invalidate Segment Editor's
        # model state.
        separated_labelmap = False
        for segment_id in sorted((event_segment_ids | added_ids) & current_ids):
            separated_labelmap = (
                self._ensure_independent_segment_labelmap(node, segment_id)
                or separated_labelmap
            )
        if separated_labelmap:
            # Separation replaces the shared representation objects for more
            # than one sibling.  Record those administrative MTime changes so
            # they can never masquerade as voxel edits on the next probe.
            for current_segment_id in current_ids:
                self._remember_segment_revision(node, current_segment_id)
        # Segment identity comes from the event payload or from that segment's
        # own representation revision. Never infer it from the currently
        # selected Segment Editor row: selection may change before a deferred
        # source-representation event is delivered.
        revision_changed_ids = set()
        shared_representation_ids = self._shared_representation_ids(node)
        for current_segment_id in current_ids:
            key = (node.GetID(), current_segment_id)
            previous_revision = self._segment_revisions.get(key)
            current_revision = self._segment_revision(node, current_segment_id)
            if (
                current_segment_id not in shared_representation_ids
                and previous_revision is not None
                and current_revision is not None
                and current_revision != previous_revision
            ):
                revision_changed_ids.add(current_segment_id)
        candidates = (event_segment_ids & current_ids) | revision_changed_ids | added_ids
        for candidate in candidates:
            key = (node.GetID(), candidate)
            self.dirty_segments.add(key)
            self._schedule_segment_verification(key)
        if ids_changed or metadata_changed_ids or event_segment_ids or unspecified_event:
            self._refresh_label_combo()
            self._update_lock_controls()
        self._force_sync_refresh = True

    def _initialize_baselines_and_seed(self, seed=False):
        node = self._segmentation_node()
        if node is None:
            return
        segment_ids = [
            str(segment_id)
            for segment_id in node.GetSegmentation().GetSegmentIDs()
        ]
        for segment_id in segment_ids:
            self._ensure_independent_segment_labelmap(node, segment_id)
        for segment_id in segment_ids:
            self._remember_segment_metadata(node, segment_id)
            key = (node.GetID(), segment_id)
            crop, bounds = self._read_mask_crop(node, segment_id)
            if seed:
                self.baselines[key] = ChunkedMaskBaseline(self.volume_shape)
                self.baseline_bounds[key] = None
                self.force_snapshots.add(key)
                self.metadata_updates.add(key)
                self.dirty_segments.add(key)
            else:
                self.baselines[key] = ChunkedMaskBaseline.from_crop(
                    self.volume_shape, crop, bounds
                )
                self.baseline_bounds[key] = list(bounds) if bounds is not None else None
            self._remember_segment_revision(node, segment_id)

    def _read_mask_crop(self, node, segment_id):
        volume_node = self.owner.get_volume_node()
        crop_reader = getattr(
            self.owner, "segment_mask_crop_in_reference_geometry", None
        )
        if callable(crop_reader):
            fast = crop_reader(node, segment_id, volume_node)
            if fast is not None:
                crop, bounds = fast
                return (
                    None if crop is None else np.asarray(crop, dtype=np.uint8),
                    None if bounds is None else [int(value) for value in bounds],
                )
        mask = self.owner.segment_mask_in_reference_geometry(
            node, segment_id, volume_node, self.volume_shape
        )
        mask = np.asarray(mask, dtype=np.uint8)
        bounds = _delta_bounds(mask != 0)
        if bounds is None:
            return None, None
        z0, z1, y0, y1, x0, x1 = bounds
        return np.ascontiguousarray(mask[z0:z1, y0:y1, x0:x1]), bounds

    def _mask_from_crop(self, crop, bounds):
        mask = np.zeros(tuple(self.volume_shape), dtype=np.uint8)
        if crop is not None and bounds is not None:
            z0, z1, y0, y1, x0, x1 = [int(value) for value in bounds]
            mask[z0:z1, y0:y1, x0:x1] = np.asarray(crop, dtype=np.uint8)
        return mask

    def _read_mask(self, node, segment_id):
        crop, bounds = self._read_mask_crop(node, segment_id)
        return self._mask_from_crop(crop, bounds)

    def _read_mask_region(self, node, segment_id, bounds):
        volume_node = self.owner.get_volume_node()
        region_reader = getattr(
            self.owner, "segment_mask_region_in_reference_geometry", None
        )
        if callable(region_reader):
            region = region_reader(node, segment_id, volume_node, bounds)
            if region is not None:
                return np.asarray(region, dtype=np.uint8)
        mask = self._read_mask(node, segment_id)
        z0, z1, y0, y1, x0, x1 = [int(value) for value in bounds]
        return np.ascontiguousarray(mask[z0:z1, y0:y1, x0:x1])

    @staticmethod
    def _crop_region(crop, crop_bounds, region_bounds):
        rz0, rz1, ry0, ry1, rx0, rx1 = [int(value) for value in region_bounds]
        result = np.zeros((rz1 - rz0, ry1 - ry0, rx1 - rx0), dtype=np.uint8)
        if crop is None or crop_bounds is None:
            return result
        cz0, cz1, cy0, cy1, cx0, cx1 = [int(value) for value in crop_bounds]
        result[
            cz0 - rz0 : cz1 - rz0,
            cy0 - ry0 : cy1 - ry0,
            cx0 - rx0 : cx1 - rx0,
        ] = np.asarray(crop, dtype=np.uint8)
        return result

    def _crop_differs_from_baseline(
        self, baseline, current_crop, current_bounds, baseline_bounds
    ):
        comparison_bounds = _bounds_union(current_bounds, baseline_bounds)
        if baseline is None or comparison_bounds is None:
            return False
        current_region = self._crop_region(
            current_crop, current_bounds, comparison_bounds
        )
        return bool(
            np.any(current_region != _baseline_region(baseline, comparison_bounds))
        )

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
            current_crop, current_bounds = self._read_mask_crop(node, segment_id)
            restore_bounds = _bounds_union(
                current_bounds,
                self.baseline_bounds.get((node.GetID(), segment_id)),
            )
            restored_incrementally = restore_bounds is None
            if restore_bounds is not None:
                z0, z1, y0, y1, x0, x1 = restore_bounds
                current_region = self._crop_region(
                    current_crop, current_bounds, restore_bounds
                )
                target_region = _baseline_region(baseline, restore_bounds)
                crop_updater = getattr(
                    self.owner, "update_segment_binary_labelmap_crop", None
                )
                restored_incrementally = bool(
                    callable(crop_updater)
                    and crop_updater(
                        current_region,
                        target_region,
                        restore_bounds,
                        node,
                        segment_id,
                        self.owner.get_volume_node(),
                    )
                )
            if not restored_incrementally:
                self.owner.update_segment_binary_labelmap_from_array(
                    _baseline_dense(baseline),
                    node,
                    segment_id,
                    self.owner.get_volume_node(),
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
        existing_operation_ids = {
            str(operation.get("client_operation_id") or "")
            for operation in self.outgoing
        }
        node = self._segmentation_node()
        for segment_id, metadata in list(self.pending_segment_deletions.items()):
            key = (self.segmentation_node_id, segment_id)
            baseline = self.baselines.get(key)
            if self._current_role() == "viewer" or self._locked_by_other(segment_id):
                if node is not None:
                    self._applying_remote = True
                    try:
                        self._ensure_segment(node, metadata)
                    finally:
                        self._applying_remote = False
                    self._restore_locked_segment(node, segment_id, baseline)
                    self._known_segment_ids.add(segment_id)
                    self._remember_segment_metadata(node, segment_id)
                self.pending_segment_deletions.pop(segment_id, None)
                continue
            encoded = encode_mask_crop_snapshot(None, None, self.volume_shape)
            operation = {
                "client_operation_id": str(uuid.uuid4()),
                "segment_id": segment_id,
                "segment_name": metadata.get("segment_name") or segment_id,
                "color_hex": metadata.get("color_hex") or "#4A90E2",
                "base_sequence": int(self.last_sequence),
                "segment_deleted": True,
                **encoded,
            }
            self.outgoing.append(operation)
            self.outgoing_keys.add(key)
            self.pending_segment_deletions.pop(segment_id, None)
            self.baselines.pop(key, None)
            self.baseline_bounds.pop(key, None)
            self.metadata_updates.discard(key)
            self._segment_revisions.pop(key, None)
            self._segment_verifications.pop(key, None)
        for key in list(self.dirty_segments):
            node = slicer.mrmlScene.GetNodeByID(key[0])
            if node is None or node.GetSegmentation().GetSegment(key[1]) is None:
                self.dirty_segments.discard(key)
                self.metadata_updates.discard(key)
                continue
            previous = self.baselines.get(key)
            current_crop, current_bounds = self._read_mask_crop(node, key[1])
            self._remember_segment_revision(node, key[1])
            if self._current_role() == "viewer":
                if self._crop_differs_from_baseline(
                    previous,
                    current_crop,
                    current_bounds,
                    self.baseline_bounds.get(key),
                ):
                    self._restore_locked_segment(node, key[1], previous)
                self.dirty_segments.discard(key)
                self.force_snapshots.discard(key)
                self.metadata_updates.discard(key)
                continue
            if self._locked_by_other(key[1]):
                if self._crop_differs_from_baseline(
                    previous,
                    current_crop,
                    current_bounds,
                    self.baseline_bounds.get(key),
                ):
                    self._restore_locked_segment(node, key[1], previous)
                self.dirty_segments.discard(key)
                self.force_snapshots.discard(key)
                self.metadata_updates.discard(key)
                continue
            if previous is None:
                previous = ChunkedMaskBaseline(self.volume_shape)
            # A new room/label is announced as a snapshot, but that snapshot is
            # encoded from the segment's effective crop (one voxel when empty),
            # never from the complete microscopy volume.
            if key in self.force_snapshots:
                encoded = encode_mask_crop_snapshot(
                    current_crop, current_bounds, self.volume_shape
                )
            else:
                pending_operations = [
                    operation
                    for operation in [*self.awaiting_echo, *self.outgoing]
                    if str(operation.get("segment_id") or "") == key[1]
                ]
                encoded = encode_mask_crop_delta_after_operations(
                    previous,
                    current_crop,
                    current_bounds,
                    self.baseline_bounds.get(key),
                    self.volume_shape,
                    pending_operations,
                )
            if encoded is None and key in self.metadata_updates:
                encoded = encode_metadata_update(self.volume_shape)
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
            if key in self.metadata_updates:
                operation["metadata_update"] = True
            self.outgoing.append(operation)
            self.outgoing_keys.add(key)
            self.dirty_segments.discard(key)
            self.force_snapshots.discard(key)
            self.metadata_updates.discard(key)
        for operation in self.outgoing:
            operation_id = str(operation.get("client_operation_id") or "")
            if operation_id and operation_id not in existing_operation_ids:
                self.session_metrics.operation_queued(operation_id)
                self.session_metrics.increment("edits_queued")
        self._sync_operation_journal()

    def _active_presence(self):
        details = self._current_location()
        details["color"] = [round(value, 4) for value in stable_user_color(self.user_name)]
        details["role"] = self._current_role()
        details["plugin_version"] = PLUGIN_VERSION
        details["protocol_version"] = COLLABORATION_PROTOCOL_VERSION
        details["transport"] = self._transport_mode()
        if self.source_volume_signature:
            details["volume_signature_hash"] = hashlib.sha256(
                self.source_volume_signature.encode("utf-8")
            ).hexdigest()[:16]
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
            key = (node.GetID(), segment_id)
            mask = self.baselines.get(key)
            if mask is None:
                crop, bounds = self._read_mask_crop(node, segment_id)
                mask = ChunkedMaskBaseline.from_crop(
                    self.volume_shape, crop, bounds
                )
            if isinstance(mask, ChunkedMaskBaseline):
                encoded_operations = encode_chunked_mask_snapshot(mask)
            else:
                bounds = self.baseline_bounds.get(key)
                crop = None
                if bounds is not None:
                    crop = _baseline_region(mask, bounds)
                encoded_operations = [
                    encode_mask_crop_snapshot(crop, bounds, self.volume_shape)
                ]
            for encoded in encoded_operations:
                if encoded is None:
                    continue
                result.append(
                    {
                        "segment_id": segment_id,
                        "segment_name": segment.GetName() or segment_id,
                        "color_hex": self._segment_color_hex(segment),
                        "metadata_update": True,
                        **encoded,
                    }
                )
        return result

    def on_timer(self):
        self._drain_worker_results()
        monotonic_now = time.monotonic()
        if self._preflight_running:
            elapsed = monotonic_now - self._preflight_started_at
            self.preflight_button.setText(
                f"Checking… {int(max(0.0, elapsed))} s"
            )
            if elapsed >= PREFLIGHT_TIMEOUT_SECONDS:
                self._session_token += 1
                self._preflight_running = False
                self._preflight_worker = None
                self._preflight_started_at = 0.0
                self._set_connection_inputs_enabled(True)
                self.join_button.enabled = True
                self.preflight_button.setText("Check connection")
                self._show_preflight_report(
                    finalize_preflight_report(
                        {
                            "transport": self._transport_mode(),
                            "checks": [
                                _preflight_check(
                                    "connection",
                                    "fail",
                                    "Connection timeout",
                                    f"The destination did not answer within {int(PREFLIGHT_TIMEOUT_SECONDS)} seconds.",
                                    "Check the address or folder, firewall, VPN/network route, and credentials.",
                                )
                            ],
                        }
                    )
                )
                self.timer.stop()
            return
        if self._joining:
            elapsed = (
                monotonic_now - self._join_started_at
                if self._join_started_at
                else 0.0
            )
            elapsed_second = max(0, int(elapsed))
            if elapsed_second != self._join_status_second:
                self._join_status_second = elapsed_second
                self.status_label.setText(
                    "● Connecting in background… "
                    f"{elapsed_second} / {int(SHARED_FOLDER_JOIN_TIMEOUT_SECONDS)} s"
                )
            if (
                self._join_started_at
                and elapsed >= SHARED_FOLDER_JOIN_TIMEOUT_SECONDS
            ):
                self._cancel_join(
                    "The collaboration location did not respond within "
                    f"{int(SHARED_FOLDER_JOIN_TIMEOUT_SECONDS)} seconds. "
                    "The connection attempt was cancelled locally and will not be "
                    "retried automatically."
                )
            return
        if not self.connected:
            return
        presence_worker_alive = (
            self._presence_worker is not None
            and self._presence_worker.is_alive()
        )
        if presence_worker_alive and self._presence_worker_started_at:
            presence_delay = monotonic_now - self._presence_worker_started_at
            if presence_delay >= PRESENCE_DELAY_WARNING_SECONDS:
                delay_second = int(presence_delay)
                if delay_second != self._presence_stall_status_second:
                    self._presence_stall_status_second = delay_second
                    # Do not turn one delayed NAS/server heartbeat into the
                    # misleading claim that every collaborator disconnected.
                    self._update_presence([])
        if _uses_shared_folder(self.client) and self._last_transport_result_at:
            response_silence = monotonic_now - self._last_transport_result_at
            response_state = shared_folder_response_state(response_silence)
            if response_state == "offline":
                self._disconnect_for_connection_loss(
                    "The shared folder did not answer any live-sync request within "
                    f"{int(SHARED_FOLDER_RESPONSE_TIMEOUT_SECONDS)} seconds."
                )
                return
            if response_state == "slow":
                silence_second = int(response_silence)
                if silence_second != self._transport_stall_status_second:
                    self._transport_stall_status_second = silence_second
                    self.connection_healthy = False
                    self.status_label.setText(
                        "● Shared folder is responding slowly — checking connection "
                        f"({silence_second} s)"
                    )
                    self.status_label.setStyleSheet(
                        "color: #b26a00; font-weight: bold;"
                    )
                self._force_health_check = True
        if (
            _uses_shared_folder(self.client)
            and self._connection_validation_pending
            and self._connection_validation_started_at
            and monotonic_now - self._connection_validation_started_at
            >= SHARED_FOLDER_RESPONSE_TIMEOUT_SECONDS
        ):
            self._disconnect_for_connection_loss(
                "The shared folder could not pass a read/write health check within "
                f"{int(SHARED_FOLDER_RESPONSE_TIMEOUT_SECONDS)} seconds."
            )
            return
        self._probe_segment_revisions()
        now = time.time()
        self._queue_due_segment_verifications(monotonic_now)

        force_sync = bool(self._force_sync_refresh)
        force_realtime = bool(self._force_realtime_refresh)

        if force_sync or now - self._last_sync_poll >= 0.10:
            self._prepare_outgoing()

        push_idle = self._edit_push_worker is None or not self._edit_push_worker.is_alive()
        snapshot_operations = []
        snapshot_label = ""
        should_snapshot = (
            push_idle
            and self.initial_sync_complete
            and not self.outgoing
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
        if push_idle and (self.outgoing or snapshot_operations):
            self._edit_push_worker = threading.Thread(
                target=self._edit_push_lane,
                args=(
                    self._session_token,
                    self.client,
                    self.room_id,
                    list(self.outgoing),
                    snapshot_operations,
                    snapshot_label,
                ),
                name="LiveSegmentation-edit-push",
                daemon=True,
            )
            self._edit_push_worker.start()

        pull_idle = self._edit_pull_worker is None or not self._edit_pull_worker.is_alive()
        if pull_idle and (force_sync or now - self._last_sync_poll >= 0.10):
            self._last_sync_poll = now
            operation_limit = (
                INITIAL_SYNC_OPERATION_BATCH if not self.initial_sync_complete else 500
            )
            self._edit_pull_worker = threading.Thread(
                target=self._edit_pull_lane,
                args=(
                    self._session_token,
                    self.client,
                    self.room_id,
                    int(self.last_sequence),
                    operation_limit,
                ),
                name="LiveSegmentation-edit-pull",
                daemon=True,
            )
            self._edit_pull_worker.start()

        chat_send_idle = (
            self._chat_send_worker is None or not self._chat_send_worker.is_alive()
        )
        if chat_send_idle and self.pending_chat:
            self._chat_send_worker = threading.Thread(
                target=self._chat_send_lane,
                args=(self._session_token, self.client, self.room_id, list(self.pending_chat)),
                name="LiveSegmentation-chat-send",
                daemon=True,
            )
            self._chat_send_worker.start()

        chat_pull_idle = (
            self._chat_pull_worker is None or not self._chat_pull_worker.is_alive()
        )
        if chat_pull_idle and (force_realtime or now - self._last_chat_poll >= 0.20):
            self._last_chat_poll = now
            self._chat_pull_worker = threading.Thread(
                target=self._chat_pull_lane,
                args=(
                    self._session_token,
                    self.client,
                    self.room_id,
                    int(self.last_chat_sequence),
                ),
                name="LiveSegmentation-chat-pull",
                daemon=True,
            )
            self._chat_pull_worker.start()

        lock_set_idle = self._lock_set_worker is None or not self._lock_set_worker.is_alive()
        if lock_set_idle and self.pending_lock_changes:
            self._lock_set_worker = threading.Thread(
                target=self._lock_set_lane,
                args=(
                    self._session_token,
                    self.client,
                    self.room_id,
                    dict(self.pending_lock_changes),
                ),
                name="LiveSegmentation-lock-set",
                daemon=True,
            )
            self._lock_set_worker.start()

        lock_pull_idle = (
            self._lock_pull_worker is None or not self._lock_pull_worker.is_alive()
        )
        if lock_pull_idle and (force_realtime or now - self._last_lock_poll >= 0.50):
            self._last_lock_poll = now
            self._lock_pull_worker = threading.Thread(
                target=self._lock_pull_lane,
                args=(self._session_token, self.client, self.room_id),
                name="LiveSegmentation-lock-pull",
                daemon=True,
            )
            self._lock_pull_worker.start()

        presence_idle = self._presence_worker is None or not self._presence_worker.is_alive()
        if presence_idle and (force_realtime or now - self._last_presence_send >= 0.75):
            self._last_presence_send = now
            self._presence_worker_started_at = time.monotonic()
            self._presence_stall_status_second = -1
            self._presence_worker = threading.Thread(
                target=self._presence_lane,
                args=(
                    self._session_token,
                    self.client,
                    self.room_id,
                    self._active_presence(),
                ),
                name="LiveSegmentation-presence",
                daemon=True,
            )
            self._presence_worker.start()

        # Keep the pull lane armed until the join watermark is actually
        # reached. A shared-folder checkpoint may briefly expose its new
        # sequence number before every compacted artifact is visible through
        # an SMB cache. Treat an empty first read as transient instead of
        # leaving a freshly joined room at sequence zero.
        self._force_sync_refresh = not self.initial_sync_complete
        self._force_realtime_refresh = False

        maintenance_idle = (
            self._maintenance_worker is None or not self._maintenance_worker.is_alive()
        )
        health_check = self._force_health_check or now - self._last_health_check >= 5.0
        backup_enabled = self.backup_enabled_checkbox.checked
        backup_enabled = backup_enabled() if callable(backup_enabled) else backup_enabled
        backup_value = self.backup_interval_spin.value
        backup_value = backup_value() if callable(backup_value) else backup_value
        backup_interval_seconds, _large_volume_safety = (
            self._automatic_backup_interval_seconds(float(backup_value) * 60.0)
        )
        backup_check = (
            bool(backup_enabled)
            and _uses_shared_folder(self.client)
            and self.initial_sync_complete
            and not self.outgoing
            and not self.dirty_segments
            and now - self._last_backup_check >= 10.0
            and now - self._joined_at_epoch >= backup_interval_seconds
            and now - self._last_edit_activity_epoch >= AUTO_BACKUP_IDLE_SECONDS
        )
        actions = list(self.pending_actions)
        if maintenance_idle and (
            health_check or backup_check or self._force_advanced_refresh or actions
        ):
            if health_check:
                self._last_health_check = now
            if backup_check:
                self._last_backup_check = now
            fetch_advanced = self._force_advanced_refresh
            self._force_health_check = False
            self._force_advanced_refresh = False
            self._maintenance_worker = threading.Thread(
                target=self._maintenance_sync_worker,
                args=(
                    self._session_token,
                    self.client,
                    self.room_id,
                    actions,
                    fetch_advanced,
                    health_check,
                    backup_check,
                    backup_interval_seconds,
                ),
                name="LiveSegmentation-maintenance",
                daemon=True,
            )
            self._maintenance_worker.start()

    def _edit_push_lane(
        self,
        session_token,
        client,
        room_id,
        outgoing,
        snapshot_operations,
        snapshot_label,
    ):
        started = time.monotonic()
        try:
            outgoing_ids = []
            rejected_segments = []
            conflicts_detected = []
            command_errors = []
            grouped_operations = {}
            for operation in outgoing:
                grouped_operations.setdefault(
                    str(operation.get("segment_id") or ""), []
                ).append(operation)

            def push_group(group):
                result = {"ids": [], "rejected": [], "conflicts": [], "errors": []}
                for operation in group:
                    try:
                        if _uses_shared_folder(client):
                            pushed = client.push_operation(
                                room_id, operation, defer_conflicts=True
                            )
                        else:
                            pushed = client.push_operation(room_id, operation)
                        result["ids"].append(operation["client_operation_id"])
                        result["conflicts"].extend((pushed or {}).get("conflicts") or [])
                    except Exception as exc:
                        message = str(exc)
                        if "locked by" not in message:
                            raise
                        result["ids"].append(operation["client_operation_id"])
                        result["rejected"].append(operation["segment_id"])
                        result["errors"].append(message)
                return result

            for group_result in _parallel_map(
                push_group, grouped_operations.values(), max_workers=4
            ):
                outgoing_ids.extend(group_result["ids"])
                rejected_segments.extend(group_result["rejected"])
                conflicts_detected.extend(group_result["conflicts"])
                command_errors.extend(group_result["errors"])
            snapshot = None
            if snapshot_operations:
                snapshot = client.publish_room_snapshot(
                    room_id, snapshot_operations, compact=True, label=snapshot_label
                )
            self._worker_results.put(
                {
                    "lane": "edit-push",
                    "session_token": session_token,
                    "outgoing_ids": outgoing_ids,
                    "rejected_segments": rejected_segments,
                    "conflicts_detected": conflicts_detected,
                    "command_errors": command_errors,
                    "snapshot": snapshot,
                    "duration": time.monotonic() - started,
                }
            )
        except Exception as exc:
            self._worker_results.put(
                {
                    "lane": "edit-push",
                    "session_token": session_token,
                    "error": str(exc),
                    "duration": time.monotonic() - started,
                }
            )

    def _edit_pull_lane(
        self, session_token, client, room_id, after_sequence, operation_limit=500
    ):
        started = time.monotonic()
        try:
            self._worker_results.put(
                {
                    "lane": "edit-pull",
                    "session_token": session_token,
                    "operations": client.operations(
                        room_id, after_sequence, limit=int(operation_limit)
                    ),
                    "duration": time.monotonic() - started,
                }
            )
        except Exception as exc:
            self._worker_results.put(
                {
                    "lane": "edit-pull",
                    "session_token": session_token,
                    "error": str(exc),
                    "duration": time.monotonic() - started,
                }
            )

    def _chat_send_lane(self, session_token, client, room_id, messages):
        started = time.monotonic()
        completed = []
        errors = []
        try:
            for message in messages:
                try:
                    client.send_chat(
                        room_id,
                        message["text"],
                        message["client_message_id"],
                        message.get("anchor"),
                    )
                    completed.append(message["client_message_id"])
                except Exception as exc:
                    message_text = str(exc)
                    if not any(
                        marker in message_text
                        for marker in ("4000 characters", "Enter a chat message")
                    ):
                        raise
                    completed.append(message["client_message_id"])
                    errors.append(message_text)
            self._worker_results.put(
                {
                    "lane": "chat-send",
                    "session_token": session_token,
                    "chat_ids": completed,
                    "command_errors": errors,
                    "duration": time.monotonic() - started,
                }
            )
        except Exception as exc:
            self._worker_results.put(
                {
                    "lane": "chat-send",
                    "session_token": session_token,
                    "error": str(exc),
                    "duration": time.monotonic() - started,
                }
            )

    def _chat_pull_lane(self, session_token, client, room_id, after_sequence):
        started = time.monotonic()
        try:
            self._worker_results.put(
                {
                    "lane": "chat-pull",
                    "session_token": session_token,
                    "messages": client.chat_messages(room_id, after_sequence),
                    "duration": time.monotonic() - started,
                }
            )
        except Exception as exc:
            self._worker_results.put(
                {
                    "lane": "chat-pull",
                    "session_token": session_token,
                    "error": str(exc),
                    "duration": time.monotonic() - started,
                }
            )

    def _presence_lane(self, session_token, client, room_id, details):
        started = time.monotonic()
        try:
            self._worker_results.put(
                {
                    "lane": "presence",
                    "session_token": session_token,
                    "users": client.presence(room_id, details),
                    "duration": time.monotonic() - started,
                }
            )
        except Exception as exc:
            self._worker_results.put(
                {
                    "lane": "presence",
                    "session_token": session_token,
                    "error": str(exc),
                    "duration": time.monotonic() - started,
                }
            )

    def _lock_set_lane(self, session_token, client, room_id, changes):
        started = time.monotonic()
        completed = []
        errors = []
        try:
            for segment_id, change in changes.items():
                try:
                    client.set_segment_lock(
                        room_id,
                        segment_id,
                        bool(change.get("locked")) if isinstance(change, dict) else bool(change),
                        int(change.get("expires_minutes", 0))
                        if isinstance(change, dict)
                        else 0,
                    )
                    completed.append(segment_id)
                except Exception as exc:
                    message = str(exc)
                    if not any(
                        marker in message for marker in ("Only ", "must synchronize once")
                    ):
                        raise
                    completed.append(segment_id)
                    errors.append(message)
            self._worker_results.put(
                {
                    "lane": "lock-set",
                    "session_token": session_token,
                    "lock_segment_ids": completed,
                    "command_errors": errors,
                    "duration": time.monotonic() - started,
                }
            )
        except Exception as exc:
            self._worker_results.put(
                {
                    "lane": "lock-set",
                    "session_token": session_token,
                    "error": str(exc),
                    "duration": time.monotonic() - started,
                }
            )

    def _lock_pull_lane(self, session_token, client, room_id):
        started = time.monotonic()
        try:
            self._worker_results.put(
                {
                    "lane": "lock-pull",
                    "session_token": session_token,
                    "locks": client.segment_locks(room_id),
                    "duration": time.monotonic() - started,
                }
            )
        except Exception as exc:
            self._worker_results.put(
                {
                    "lane": "lock-pull",
                    "session_token": session_token,
                    "error": str(exc),
                    "duration": time.monotonic() - started,
                }
            )

    def _sync_worker(
        self,
        session_token,
        client,
        room_id,
        outgoing,
        after_sequence,
        snapshot_operations,
        snapshot_label,
    ):
        started = time.monotonic()
        try:
            outgoing_ids = []
            rejected_segments = []
            conflicts_detected = []
            command_errors = []

            grouped_operations = {}
            for operation in outgoing:
                grouped_operations.setdefault(str(operation.get("segment_id") or ""), []).append(
                    operation
                )

            def push_segment_group(group):
                result = {
                    "outgoing_ids": [],
                    "rejected_segments": [],
                    "conflicts_detected": [],
                    "command_errors": [],
                }
                # Preserve edit order inside one label, while independent labels
                # are free to make progress in parallel.
                for operation in group:
                    try:
                        pushed = client.push_operation(room_id, operation)
                        result["outgoing_ids"].append(operation["client_operation_id"])
                        result["conflicts_detected"].extend(
                            (pushed or {}).get("conflicts") or []
                        )
                    except Exception as exc:
                        message = str(exc)
                        if "locked by" not in message:
                            raise
                        result["outgoing_ids"].append(operation["client_operation_id"])
                        result["rejected_segments"].append(operation["segment_id"])
                        result["command_errors"].append(message)
                return result

            for group_result in _parallel_map(
                push_segment_group,
                grouped_operations.values(),
                max_workers=4,
            ):
                outgoing_ids.extend(group_result["outgoing_ids"])
                rejected_segments.extend(group_result["rejected_segments"])
                conflicts_detected.extend(group_result["conflicts_detected"])
                command_errors.extend(group_result["command_errors"])
            snapshot = None
            if snapshot_operations:
                snapshot = client.publish_room_snapshot(
                    room_id, snapshot_operations, compact=True, label=snapshot_label
                )
            operations = client.operations(room_id, after_sequence)
            self._worker_results.put(
                {
                    "lane": "sync",
                    "session_token": session_token,
                    "outgoing_ids": outgoing_ids,
                    "rejected_segments": rejected_segments,
                    "conflicts_detected": conflicts_detected,
                    "command_errors": command_errors,
                    "operations": operations,
                    "snapshot": snapshot,
                    "duration": time.monotonic() - started,
                }
            )
        except Exception as exc:
            self._worker_results.put(
                {
                    "lane": "sync",
                    "session_token": session_token,
                    "error": str(exc),
                    "duration": time.monotonic() - started,
                }
            )

    def _realtime_sync_worker(
        self,
        session_token,
        client,
        room_id,
        presence,
        chat_outgoing,
        after_chat_sequence,
        lock_changes,
    ):
        started = time.monotonic()
        try:
            def send_chat_batch(_unused):
                completed = []
                errors = []
                for message in chat_outgoing:
                    try:
                        client.send_chat(
                            room_id,
                            message["text"],
                            message["client_message_id"],
                            message.get("anchor"),
                        )
                        completed.append(message["client_message_id"])
                    except Exception as exc:
                        message_text = str(exc)
                        if not any(
                            marker in message_text
                            for marker in ("4000 characters", "Enter a chat message")
                        ):
                            raise
                        completed.append(message["client_message_id"])
                        errors.append(message_text)
                return completed, errors

            def apply_lock_batch(_unused):
                def apply_one(item):
                    segment_id, change = item
                    try:
                        client.set_segment_lock(
                            room_id,
                            segment_id,
                            bool(change.get("locked"))
                            if isinstance(change, dict)
                            else bool(change),
                            int(change.get("expires_minutes", 0))
                            if isinstance(change, dict)
                            else 0,
                        )
                        return segment_id, None
                    except Exception as exc:
                        message = str(exc)
                        if not any(
                            marker in message
                            for marker in ("Only ", "must synchronize once")
                        ):
                            raise
                        return segment_id, message

                completed = []
                errors = []
                for segment_id, error in _parallel_map(
                    apply_one,
                    lock_changes.items(),
                    max_workers=4,
                ):
                    completed.append(segment_id)
                    if error:
                        errors.append(error)
                return completed, errors

            outbound_tasks = []
            if chat_outgoing:
                outbound_tasks.append(("chat", send_chat_batch))
            if lock_changes:
                outbound_tasks.append(("locks", apply_lock_batch))
            if presence is not None:
                outbound_tasks.append(
                    ("presence", lambda _unused: client.presence(room_id, presence))
                )

            outbound_results = dict(
                _parallel_map(
                    lambda task: (task[0], task[1](None)),
                    outbound_tasks,
                    max_workers=3,
                )
            )
            chat_ids, chat_errors = outbound_results.get("chat", ([], []))
            lock_segment_ids, lock_errors = outbound_results.get("locks", ([], []))
            command_errors = [*chat_errors, *lock_errors]
            users = outbound_results.get("presence")

            live_reads = _parallel_map(
                lambda task: (task[0], task[1]()),
                [
                    ("messages", lambda: client.chat_messages(room_id, after_chat_sequence)),
                    ("locks", lambda: client.segment_locks(room_id)),
                ],
                max_workers=2,
            )
            live_results = dict(live_reads)
            messages = live_results["messages"]
            locks = live_results["locks"]
            self._worker_results.put(
                {
                    "lane": "realtime",
                    "session_token": session_token,
                    "chat_ids": chat_ids,
                    "lock_segment_ids": lock_segment_ids,
                    "command_errors": command_errors,
                    "users": users,
                    "messages": messages,
                    "locks": locks,
                    "duration": time.monotonic() - started,
                }
            )
        except Exception as exc:
            self._worker_results.put(
                {
                    "lane": "realtime",
                    "session_token": session_token,
                    "error": str(exc),
                    "duration": time.monotonic() - started,
                }
            )

    def _maintenance_sync_worker(
        self,
        session_token,
        client,
        room_id,
        actions,
        fetch_advanced,
        health_check,
        backup_check,
        backup_interval_seconds,
    ):
        started = time.monotonic()
        health_ok = False
        try:
            if health_check:
                client.health_check(room_id)
                health_ok = True
            action_ids = []
            action_results = []
            command_errors = []
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
                    elif kind == "backup_now":
                        value = client.reserve_project_backup(
                            room_id, 0, force=True
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
                    elif kind == "compare_revision":
                        value = client.state_at_sequence(room_id, action["sequence"])
                    elif kind == "undo_operation":
                        sequence = int(action["sequence"])
                        operations = client.operations(
                            room_id, max(0, sequence - 1), limit=1
                        )
                        target = next(
                            (
                                operation
                                for operation in operations
                                if int(operation.get("sequence", 0)) == sequence
                            ),
                            None,
                        )
                        if target is None:
                            raise LiveCollaborationError(
                                "The selected edit is no longer available for undo"
                            )
                        value = {
                            "target": target,
                            "before": client.state_at_sequence(
                                room_id, max(0, sequence - 1)
                            ),
                        }
                    elif kind == "benchmark":
                        value = benchmark_room_transport(
                            client,
                            room_id,
                            action.get("after_sequence", 0),
                            samples=5,
                        )
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
                    action_results.append(
                        {
                            "id": action["id"],
                            "action": action.get("action"),
                            "error": str(exc),
                            "request": action,
                        }
                    )
            advanced = None
            if fetch_advanced:
                advanced_fetches = [
                    ("history", lambda: client.room_history(room_id, 500)),
                    ("conflicts", lambda: client.room_conflicts(room_id, False)),
                    ("roles", lambda: client.room_roles(room_id)),
                    ("reviews", lambda: client.review_states(room_id)),
                    (
                        "access_requests",
                        lambda: client.segment_access_requests(room_id),
                    ),
                    ("material_template", lambda: client.get_material_template(room_id)),
                    ("audit", lambda: client.audit_events(room_id, 100)),
                ]
                if hasattr(client, "snapshot_manifests"):
                    advanced_fetches.append(
                        ("snapshots", lambda: client.snapshot_manifests(room_id))
                    )
                advanced = dict(
                    _parallel_map(
                        lambda task: (task[0], task[1]()),
                        advanced_fetches,
                        max_workers=8,
                    )
                )
            backup = (
                client.reserve_project_backup(room_id, backup_interval_seconds)
                if backup_check
                else None
            )
            self._worker_results.put(
                {
                    "lane": "maintenance",
                    "session_token": session_token,
                    "action_ids": action_ids,
                    "action_results": action_results,
                    "command_errors": command_errors,
                    "advanced": advanced,
                    "backup": backup,
                    "health_checked": bool(health_check),
                    "health_ok": health_ok,
                    "duration": time.monotonic() - started,
                }
            )
        except Exception as exc:
            self._worker_results.put(
                {
                    "lane": "maintenance",
                    "session_token": session_token,
                    "error": str(exc),
                    "health_checked": bool(health_check),
                    "health_ok": health_ok,
                    "duration": time.monotonic() - started,
                }
            )

    def _drain_worker_results(self):
        while True:
            try:
                result = self._worker_results.get_nowait()
            except queue.Empty:
                return
            if result.get("lane") == "join":
                if (
                    result.get("session_token") != self._session_token
                    or not self._joining
                ):
                    room = result.get("room") or {}
                    self._leave_client_in_background(
                        result.get("client"), room.get("id")
                    )
                    continue
                if "error" in result:
                    self._cancel_join(result["error"])
                else:
                    self._finish_join(result["client"], result["room"])
                continue
            if result.get("lane") == "preflight":
                if result.get("session_token") != self._session_token:
                    continue
                self._preflight_running = False
                self._preflight_worker = None
                self._preflight_started_at = 0.0
                self._set_connection_inputs_enabled(
                    not self.connected and not self._joining
                )
                self.join_button.enabled = not self.connected and not self._joining
                self.preflight_button.setText("Check connection")
                if "error" in result:
                    report = finalize_preflight_report(
                        {
                            "transport": self._transport_mode(),
                            "checks": [
                                _preflight_check(
                                    "connection",
                                    "fail",
                                    "Connection",
                                    result["error"],
                                    "Check the address or folder, credentials, firewall, and network route.",
                                )
                            ],
                        }
                    )
                else:
                    report = dict(result.get("report") or {})
                self._show_preflight_report(report)
                if not self.connected and not self._joining:
                    self.timer.stop()
                continue
            if result.get("session_token") != self._session_token or not self.connected:
                continue
            lane = str(result.get("lane") or "sync")
            if lane == "presence":
                self._presence_worker_started_at = 0.0
                self._presence_stall_status_second = -1
            self.session_metrics.record(lane, float(result.get("duration", 0.0)))
            if isinstance(self.client, HybridRoomClient):
                fallback_count = int(self.client.fallback_count)
                if fallback_count > self._last_hybrid_fallback_count:
                    difference = fallback_count - self._last_hybrid_fallback_count
                    self.session_metrics.increment("shared_folder_fallbacks", difference)
                    self._append_activity(
                        "Direct LAN was unavailable; used shared-folder fallback "
                        f"for {difference} request(s)"
                    )
                self._last_hybrid_fallback_count = fallback_count
            if lane == "local-quality":
                self.run_quality_button.enabled = True
                if "error" in result:
                    self.quality_text.setPlainText(
                        f"Quality analysis failed: {result['error']}"
                    )
                else:
                    self.last_quality_report = dict(result.get("quality") or {})
                    issues = self.last_quality_report.get("issues") or []
                    lines = [
                        f"Checked {len(self.last_quality_report.get('labels') or {})} labels — "
                        f"{len(issues)} issue(s)."
                    ]
                    for issue in issues[:30]:
                        issue_type = str(issue.get("type") or "issue")
                        if issue_type == "overlap":
                            lines.append(
                                f"• overlap: {' / '.join(issue.get('segments') or [])} — "
                                f"{issue.get('voxels', 0)} voxels"
                            )
                        else:
                            label = issue.get("segment_id") or "label"
                            detail = ", ".join(
                                f"{key}={value}"
                                for key, value in issue.items()
                                if key not in {"type", "segment_id"}
                            )
                            lines.append(f"• {issue_type}: {label} ({detail})")
                    self.quality_text.setPlainText("\n".join(lines))
                    self.session_metrics.increment("quality_issues", len(issues))
                self._update_performance_label()
                continue
            self._last_transport_result_at = time.monotonic()
            if "error" in result:
                if (
                    _uses_shared_folder(self.client)
                    and result.get("lane") == "maintenance"
                    and result.get("health_checked")
                    and not result.get("health_ok")
                ):
                    self._disconnect_for_connection_loss(result["error"])
                    return
                if result.get("lane") == "maintenance" and (
                    result.get("health_ok") or not result.get("health_checked")
                ):
                    import slicer

                    slicer.util.showStatusMessage(
                        f"Live collaboration maintenance warning: {result['error']}", 6000
                    )
                    continue
                if (
                    _uses_shared_folder(self.client)
                    and _is_transient_shared_read_error(result["error"])
                    and result.get("lane") != "maintenance"
                ):
                    # One SMB read may fail while an atomic replacement becomes
                    # visible. Keep the room live, retry the affected feeds, and
                    # let the dedicated read/write health probe decide whether
                    # this is a real outage. This avoids a red offline message
                    # that disappears as soon as another parallel lane succeeds.
                    self._last_transport_warning = {
                        "message": str(result["error"]),
                        "lane": str(result.get("lane") or "unknown"),
                        "observed_at": _utc_iso(),
                    }
                    if not self._connection_validation_pending:
                        self._connection_validation_started_at = time.monotonic()
                    self._connection_validation_pending = True
                    self._force_health_check = True
                    self._force_sync_refresh = True
                    self._force_realtime_refresh = True
                    continue
                if not self._connection_validation_pending:
                    self._connection_validation_started_at = time.monotonic()
                self._connection_validation_pending = True
                self._force_health_check = True
                show_popup = not self._connection_error_popup_shown
                self._connection_error_popup_shown = True
                self._show_error(result["error"], popup=show_popup)
                continue
            health_confirmed = bool(
                result.get("health_checked") and result.get("health_ok")
            )
            if health_confirmed:
                self._connection_validation_pending = False
                self._connection_validation_started_at = 0.0
                self.connection_healthy = True
                self._connection_error_popup_shown = False
                if self._connection_error_dialog is not None:
                    self._connection_error_dialog.close()
                    self._connection_error_dialog = None
            elif not self._connection_validation_pending:
                self.connection_healthy = True
                self._connection_error_popup_shown = False
                if self._connection_error_dialog is not None:
                    self._connection_error_dialog.close()
                    self._connection_error_dialog = None
            if result.get("lane") in {
                "sync",
                "realtime",
                "edit-push",
                "edit-pull",
                "chat-send",
                "chat-pull",
                "presence",
                "lock-set",
                "lock-pull",
                None,
            }:
                self._last_sync_duration = float(result.get("duration", 0.0))
            sent_ids = set(result.get("outgoing_ids") or [])
            if sent_ids:
                retained = []
                sent_operations = []
                self.outgoing_keys.clear()
                for operation in self.outgoing:
                    operation_id = operation["client_operation_id"]
                    if operation_id not in sent_ids:
                        retained.append(operation)
                    else:
                        sent_operations.append(operation)
                self.outgoing = retained
                for operation in retained:
                    self.outgoing_keys.add(
                        (self.segmentation_node_id, operation["segment_id"])
                    )
                awaiting_ids = {
                    operation["client_operation_id"]
                    for operation in self.awaiting_echo
                }
                for operation in sent_operations:
                    operation_id = operation["client_operation_id"]
                    if operation_id in self._applied_local_operation_ids:
                        self._applied_local_operation_ids.discard(operation_id)
                    elif operation_id not in awaiting_ids:
                        self.awaiting_echo.append(operation)
                        awaiting_ids.add(operation_id)
                self._sync_operation_journal()
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
            operations = result.get("operations") or []
            if operations:
                self._append_history_operations(operations)
            self._apply_operations(operations)
            if operations:
                self._refresh_label_combo()
            if result.get("users") is not None:
                self._update_presence(result["users"])
            if result.get("messages") is not None:
                self._append_chat_messages(result["messages"])
            if result.get("locks") is not None:
                self._update_segment_locks(result["locks"])
                self._refresh_label_combo()
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
                self._force_advanced_refresh = True
                self.session_metrics.increment(
                    "conflicts_detected", len(result["conflicts_detected"])
                )
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
            if self.connected and backup_ok and result.get("lane") != "maintenance":
                if not self.initial_sync_complete:
                    self.status_label.setText(self._initial_sync_status_text())
                    self.status_label.setStyleSheet(
                        "color: #b26a00; font-weight: bold;"
                    )
                elif self._last_sync_duration >= 2.5:
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
            if time.monotonic() - self._last_metrics_display >= 0.5:
                self._last_metrics_display = time.monotonic()
                self._update_performance_label()
            # Applying a received voxel patch to MRML can itself take several
            # seconds on a large volume.  Refresh the transport timestamp after
            # all local result processing so that CPU/rendering time is never
            # misclassified as network silence by the next timer tick.
            if self.connected:
                self._last_transport_result_at = time.monotonic()
                self._transport_stall_status_second = -1

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
            self.session_metrics.operation_queued(queued["client_operation_id"])
            self.session_metrics.increment("snapshot_edits_queued")
        self._sync_operation_journal()

    def _remove_comparison_node(self):
        if not self._comparison_node_id:
            return
        try:
            import slicer

            node = slicer.mrmlScene.GetNodeByID(self._comparison_node_id)
            if node is not None:
                slicer.mrmlScene.RemoveNode(node)
        except Exception:
            pass
        self._comparison_node_id = None

    def _show_revision_comparison(self, historical_operations, sequence):
        """Overlay voxels added/removed since a historical room sequence."""
        import slicer

        node = self._segmentation_node()
        if node is None:
            raise LiveCollaborationError("The shared segmentation is unavailable")
        shape = tuple(int(value) for value in self.volume_shape)
        added = np.zeros(shape, dtype=bool)
        removed = np.zeros(shape, dtype=bool)
        historical = {}
        for operation in historical_operations or []:
            segment_id = str(operation.get("segment_id") or "")
            if segment_id:
                historical[segment_id] = apply_mask_delta(
                    np.zeros(shape, dtype=np.uint8), operation
                ).astype(bool)
        current_ids = set(node.GetSegmentation().GetSegmentIDs())
        for segment_id in current_ids | set(historical):
            current = (
                self._read_mask(node, segment_id).astype(bool)
                if segment_id in current_ids
                else np.zeros(shape, dtype=bool)
            )
            previous = historical.get(segment_id)
            if previous is None:
                previous = np.zeros(shape, dtype=bool)
            added |= current & ~previous
            removed |= previous & ~current

        self._remove_comparison_node()
        comparison = slicer.mrmlScene.AddNewNodeByClass(
            "vtkMRMLSegmentationNode", f"Live comparison — revision {int(sequence)}"
        )
        comparison.SetAttribute("LiveSegmentation.RevisionComparison", "1")
        comparison.CreateDefaultDisplayNodes()
        comparison.SetReferenceImageGeometryParameterFromVolumeNode(
            self.owner.get_volume_node()
        )
        for segment_id, name, color, mask in (
            ("Added", "Added since revision", (0.1, 0.9, 0.2), added),
            ("Removed", "Removed since revision", (0.95, 0.15, 0.15), removed),
        ):
            segment = slicer.vtkSegment()
            segment.SetName(name)
            segment.SetColor(*color)
            comparison.GetSegmentation().AddSegment(segment, segment_id)
            self.owner.update_segment_binary_labelmap_from_array(
                mask.astype(np.uint8),
                comparison,
                segment_id,
                self.owner.get_volume_node(),
            )
        self._comparison_node_id = comparison.GetID()
        display = comparison.GetDisplayNode()
        if display is not None:
            for segment_id in ("Added", "Removed"):
                display.SetSegmentOpacity2DFill(segment_id, 0.25)
                display.SetSegmentOpacity2DOutline(segment_id, 1.0)
        self._append_activity(
            f"Opened comparison: revision {int(sequence)} versus current state"
        )
        slicer.util.showStatusMessage(
            f"Comparison ready: {int(np.count_nonzero(added))} added and "
            f"{int(np.count_nonzero(removed))} removed voxels",
            5000,
        )

    def _queue_collaborative_undo(self, value, sequence):
        target = dict((value or {}).get("target") or {})
        segment_id = str(target.get("segment_id") or "")
        if not segment_id:
            raise LiveCollaborationError("The edit selected for undo has no label")
        before_operation = next(
            (
                operation
                for operation in (value or {}).get("before") or []
                if str(operation.get("segment_id") or "") == segment_id
            ),
            None,
        )
        if target.get("segment_deleted"):
            if before_operation is None:
                raise LiveCollaborationError(
                    "The deleted label did not exist before this edit"
                )
            self._queue_snapshot_operations(
                [{**before_operation, "undo_of_sequence": int(sequence)}]
            )
            return

        node = self._segmentation_node()
        if node is None or node.GetSegmentation().GetSegment(segment_id) is None:
            raise LiveCollaborationError(
                "The label no longer exists; restore a revision instead"
            )
        current = self._read_mask(node, segment_id)
        previous = (
            apply_mask_delta(np.zeros_like(current), before_operation)
            if before_operation is not None
            else np.zeros_like(current)
        )
        changed, target_values = decode_mask_delta(target)
        z0, z1, y0, y1, x0, x1 = [int(value) for value in target["voxel_bbox"]]
        current_region = current[z0:z1, y0:y1, x0:x1]
        previous_region = previous[z0:z1, y0:y1, x0:x1]
        # Only revert voxels that still carry the value written by the target
        # operation. Later edits by collaborators are preserved.
        restore = changed & (current_region == target_values)
        desired = current.copy()
        desired_region = desired[z0:z1, y0:y1, x0:x1]
        desired_region[restore] = previous_region[restore]
        encoded = encode_mask_delta(current, desired, replace=False)
        if encoded is None:
            raise LiveCollaborationError(
                "Nothing can be undone because all affected voxels changed later"
            )
        queued = {
            "client_operation_id": str(uuid.uuid4()),
            "segment_id": segment_id,
            "segment_name": target.get("segment_name") or segment_id,
            "color_hex": target.get("color_hex") or "#4A90E2",
            "base_sequence": int(self.last_sequence),
            "undo_of_sequence": int(sequence),
            **encoded,
        }
        self.outgoing.append(queued)
        self.session_metrics.operation_queued(queued["client_operation_id"])
        self.session_metrics.increment("collaborative_undo_queued")
        self._sync_operation_journal()

    def _handle_action_results(self, results):
        import slicer

        for result in results:
            action = result.get("action")
            value = result.get("value")
            request = result.get("request") or {}
            if result.get("error"):
                if action == "compare_revision":
                    self.compare_revision_button.enabled = True
                elif action == "undo_operation":
                    self.undo_shared_button.enabled = True
                elif action == "benchmark":
                    self.benchmark_button.enabled = True
                self._show_error(
                    f"{str(action or 'Collaboration action').replace('_', ' ')} failed: "
                    f"{result['error']}"
                )
                continue
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
                self._force_advanced_refresh = True
            elif action == "list_backups":
                self._update_backup_tree(value or [])
            elif action == "backup_now":
                if value and self._create_project_backup(value):
                    self.backup_status_label.setText(
                        "Manual project backup completed successfully."
                    )
                    self._queue_action("list_backups")
            elif action == "pin_backup":
                self._queue_action("list_backups")
            elif action == "verify_backup":
                valid = bool((value or {}).get("valid"))
                self.backup_status_label.setText(
                    f"Checksum {'valid' if valid else 'FAILED'}: {(value or {}).get('name', '')}"
                )
            elif action == "diagnostics":
                chunked_baselines = [
                    baseline
                    for baseline in self.baselines.values()
                    if isinstance(baseline, ChunkedMaskBaseline)
                ]
                self.last_diagnostics = dict(value or {})
                self.last_diagnostics.update(
                    {
                        "pending_operations": len(self.outgoing)
                        + len(self.awaiting_echo),
                        "dirty_segments": len(self.dirty_segments),
                        "pending_chat_messages": len(self.pending_chat),
                        "connection_healthy": bool(self.connection_healthy),
                        "last_transport_warning": self._last_transport_warning,
                        "baseline_storage": {
                            "mode": "sparse-64-cubed-chunks",
                            "labels": len(chunked_baselines),
                            "chunks": sum(
                                baseline.chunk_count for baseline in chunked_baselines
                            ),
                            "allocated_bytes": sum(
                                baseline.allocated_bytes
                                for baseline in chunked_baselines
                            ),
                        },
                    }
                )
            elif action == "compare_revision":
                self._show_revision_comparison(value or [], request.get("sequence", 0))
                self.compare_revision_button.enabled = True
            elif action == "undo_operation":
                self._queue_collaborative_undo(value, request.get("sequence", 0))
                self.undo_shared_button.enabled = True
                self._force_sync_refresh = True
                slicer.util.showStatusMessage(
                    f"Collaborative undo queued for sequence {request.get('sequence')}",
                    4000,
                )
            elif action == "benchmark":
                self.last_benchmark = dict(value or {})
                self.benchmark_button.enabled = True
                self._update_performance_label()
                self.diagnostics_text.setPlainText(
                    json.dumps(self.last_benchmark, ensure_ascii=False, indent=2)
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
                self._force_advanced_refresh = True

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

    @staticmethod
    def _local_clock_text(stamp, include_seconds=False):
        try:
            clock = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).astimezone()
            return clock.strftime("%H:%M:%S" if include_seconds else "%H:%M")
        except Exception:
            return "--:--:--" if include_seconds else "--:--"

    def _append_history_operations(self, operations):
        import qt

        known_segments = {
            str(record.get("segment_id") or "")
            for record in self.history_records
            if record.get("segment_id")
        }
        for record in sorted(operations, key=lambda item: int(item.get("sequence", 0))):
            sequence = int(record.get("sequence", 0) or 0)
            if sequence <= 0 or sequence in self.history_sequences:
                continue
            segment_id = str(record.get("segment_id") or "")
            segment_name = str(record.get("segment_name") or segment_id or "label")
            author = str(record.get("author") or "Unknown")
            if record.get("system_snapshot"):
                action = f"{author} created a project checkpoint"
                activity_type = "checkpoint"
                if record.get("snapshot_label"):
                    action += f" “{record.get('snapshot_label')}”"
            elif record.get("undo_of_sequence"):
                action = (
                    f"{author} undid sequence {record.get('undo_of_sequence')} "
                    f"on label “{segment_name}”"
                )
                activity_type = "collaborative undo"
            elif record.get("segment_deleted"):
                action = f"{author} deleted label “{segment_name}”"
                activity_type = "label deleted"
            elif segment_id not in known_segments:
                action = f"{author} created label “{segment_name}”"
                activity_type = "label created"
            elif record.get("metadata_update"):
                action = f"{author} updated label properties for “{segment_name}”"
                activity_type = "label metadata"
            else:
                action = f"{author} edited label “{segment_name}”"
                activity_type = str(record.get("operation_kind") or "patch")
            if record.get("segment_deleted"):
                known_segments.discard(segment_id)
            else:
                known_segments.add(segment_id)
            item = qt.QTreeWidgetItem(
                [
                    self._local_clock_text(record.get("created_at"), include_seconds=True),
                    action,
                    str(sequence),
                    str(record.get("changed_voxels", "")),
                    activity_type,
                ]
            )
            item.setData(0, 32, sequence)
            self.history_tree.insertTopLevelItem(0, item)
            self.history_records.append(dict(record))
            self.history_sequences.add(sequence)
        self.filter_history_tree()

    def _update_advanced_state(self, advanced):
        import qt

        merged_history = {
            int(record.get("sequence", 0)): dict(record)
            for record in (advanced.get("history") or [])
            if int(record.get("sequence", 0) or 0) > 0
        }
        for record in self.history_records:
            sequence = int(record.get("sequence", 0) or 0)
            if sequence > 0:
                merged_history[sequence] = dict(record)
        self.history_records = []
        self.history_sequences = set()
        self.history_tree.clear()
        self._append_history_operations(merged_history.values())
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
        self.review_queue_tree.clear()
        review_order = {
            "changes_requested": 0,
            "ready_for_review": 1,
            "in_progress": 2,
            "draft": 3,
            "approved": 4,
        }
        for segment_id, review in sorted(
            self.review_states_state.items(),
            key=lambda item: (
                review_order.get(str(item[1].get("state") or "draft"), 9),
                str(item[0]).casefold(),
            ),
        ):
            node = self._segmentation_node()
            segment = (
                node.GetSegmentation().GetSegment(segment_id)
                if node is not None
                else None
            )
            item = qt.QTreeWidgetItem(
                [
                    str(review.get("state") or "draft").replace("_", " "),
                    segment.GetName() if segment is not None else segment_id,
                    str(review.get("updated_by") or ""),
                    str(review.get("note") or ""),
                ]
            )
            item.setData(0, 32, segment_id)
            self.review_queue_tree.addTopLevelItem(item)
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
            stamp = str(message.get("created_at") or "")
            clock_text = self._local_clock_text(stamp)
            author = str(message.get("author") or "Unknown")
            text = str(message.get("text") or "")
            anchor = message.get("anchor")
            message_kind = (
                str(anchor.get("message_kind") or "chat")
                if isinstance(anchor, dict)
                else "chat"
            )
            client_message_id = str(message.get("client_message_id") or "")
            message_key = (
                f"{author}\0{client_message_id}"
                if client_message_id
                else f"sequence\0{sequence}"
            )
            already_displayed = message_key in self.displayed_chat_ids
            marker = " 📍" if isinstance(anchor, dict) and anchor else ""
            if not already_displayed:
                self._append_chat_line(f"[{clock_text}] {author}: {text}{marker}")
            if isinstance(anchor, dict) and anchor:
                self.chat_anchors[sequence] = anchor
                if sequence not in self.displayed_chat_sequences:
                    self.chat_location_combo.addItem(
                        f"[{clock_text}] {author}: {text[:60]}", sequence
                    )
                    self.jump_to_chat_button.enabled = True
            if message_kind == "comment":
                self._register_comment(message)
            elif message_kind == "comment-resolution":
                target_comment_id = str(anchor.get("target_comment_id") or "")
                if target_comment_id:
                    self.resolved_comment_ids.add(target_comment_id)
                    if target_comment_id in self.comments:
                        self.comments[target_comment_id]["resolved"] = True
                    self._refresh_comment_tree()
            self.optimistic_chat_ids.discard(client_message_id)
            self.displayed_chat_ids.add(message_key)
            self.displayed_chat_sequences.add(sequence)
            self.last_chat_sequence = max(self.last_chat_sequence, sequence)
        self._scroll_chat_to_end(self.chat_history)
        self._scroll_chat_to_end(self.chat_dock_history)

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
                "updated_by": state.get("updated_by"),
                "expires_at": state.get("expires_at"),
            }
            self._set_segment_collaboration_tags(segment_id)
        self.segment_locks_state = states
        for segment_id in states:
            self._set_segment_collaboration_tags(segment_id)
        self._update_lock_controls()

    def _update_lock_controls(self):
        if not self.connected:
            return
        self._refresh_label_combo()
        segment_id = self._selected_segment_id()
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
        review_note = str(current_review.get("note") or "").strip()
        current_status = self.lock_status_label.text
        current_status = current_status() if callable(current_status) else current_status
        self.lock_status_label.setText(
            f"{current_status} · review: {review_state.replace('_', ' ')}"
            + (f" — {review_note}" if review_note else "")
        )
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
            if _uses_shared_folder(self.client):
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
        created = segment is None
        if segment is None:
            segment = slicer.vtkSegment()
            segmentation.AddSegment(segment, segment_id)
        # Voxel patches carry metadata only as context. Applying it on every
        # patch lets a delayed paint operation revert a newer rename or color
        # change. New segments need initial metadata; existing segments change
        # it only for an explicit metadata update.
        if created or bool(operation.get("metadata_update")):
            segment.SetName(operation.get("segment_name") or segment_id)
            color = str(operation.get("color_hex") or "#4A90E2").lstrip("#")
            if len(color) == 6:
                try:
                    segment.SetColor(
                        *(
                            int(color[index : index + 2], 16) / 255.0
                            for index in (0, 2, 4)
                        )
                    )
                except Exception:
                    pass
        self._remember_segment_metadata(node, segment_id)
        return segment

    def _acknowledge_local_operation(self, operation):
        operation_id = str(operation.get("client_operation_id") or "")
        if not operation_id:
            return
        awaiting_count = len(self.awaiting_echo)
        self.awaiting_echo = [
            queued
            for queued in self.awaiting_echo
            if queued.get("client_operation_id") != operation_id
        ]
        if len(self.awaiting_echo) == awaiting_count and any(
            queued.get("client_operation_id") == operation_id
            for queued in self.outgoing
        ):
            # The pull lane can observe the server echo before the push lane
            # result is drained. Remember that ordering so the sent operation
            # is not re-added to awaiting_echo.
            self._applied_local_operation_ids.add(operation_id)
        self.session_metrics.operation_acknowledged(operation_id)
        self._sync_operation_journal()

    def _apply_operations(self, operations):
        import slicer

        apply_started = time.monotonic()
        applied_count = 0
        if operations:
            self._last_edit_activity_epoch = time.time()
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
                metadata_before = self._current_segment_metadata(node, segment_id)
                if operation.get("segment_deleted"):
                    self._segment_metadata[segment_id] = {
                        "segment_id": segment_id,
                        "segment_name": operation.get("segment_name") or segment_id,
                        "color_hex": operation.get("color_hex") or "#4A90E2",
                    }
                    self._applying_remote = True
                    try:
                        if node.GetSegmentation().GetSegment(segment_id) is not None:
                            node.GetSegmentation().RemoveSegment(segment_id)
                    finally:
                        self._applying_remote = False
                    self.baselines.pop(key, None)
                    self.baseline_bounds.pop(key, None)
                    self.dirty_segments.discard(key)
                    self.force_snapshots.discard(key)
                    self.metadata_updates.discard(key)
                    self.pending_segment_deletions.pop(segment_id, None)
                    self._segment_revisions.pop(key, None)
                    self._segment_verifications.pop(key, None)
                    self._known_segment_ids.discard(segment_id)
                    self.pending_lock_changes.pop(segment_id, None)
                    self.segment_locks_state.pop(segment_id, None)
                    self.review_states_state.pop(segment_id, None)
                    node.Modified()
                    actor = (
                        "You"
                        if operation.get("author") == self.user_name
                        else operation.get("author") or "Collaborator"
                    )
                    self._append_activity(
                        f"{actor} deleted label "
                        f"{operation.get('segment_name') or segment_id}"
                    )
                    if operation.get("author") != self.user_name:
                        slicer.util.showStatusMessage(
                            f"Live: {operation.get('author')} deleted label "
                            f"{operation.get('segment_name') or segment_id}",
                            2500,
                        )
                    else:
                        self._acknowledge_local_operation(operation)
                    self.last_sequence = sequence
                    applied_count += 1
                    continue
                self._applying_remote = True
                self._ensure_segment(node, operation)
                self._ensure_independent_segment_labelmap(node, segment_id)
                if (
                    key in self.metadata_updates
                    and operation.get("author") != self.user_name
                    and metadata_before is not None
                ):
                    # A local rename/color change that has not been published
                    # yet is analogous to an unsent local voxel edit: keep it
                    # visible and let its later ordered operation decide the
                    # final shared state.
                    local_segment = node.GetSegmentation().GetSegment(segment_id)
                    local_segment.SetName(metadata_before["segment_name"])
                    local_color = metadata_before["color_hex"].lstrip("#")
                    local_segment.SetColor(
                        *(
                            int(local_color[index : index + 2], 16) / 255.0
                            for index in (0, 2, 4)
                        )
                    )
                    self._remember_segment_metadata(node, segment_id)
                metadata_after = self._current_segment_metadata(node, segment_id)
                operation_bounds = [
                    int(value) for value in operation["voxel_bbox"]
                ]
                oz0, oz1, oy0, oy1, ox0, ox1 = operation_bounds
                changed, values = decode_mask_delta(operation)
                baseline = self.baselines.get(key)
                if baseline is None:
                    baseline = ChunkedMaskBaseline(self.volume_shape)
                    self.baselines[key] = baseline

                # Ordinary edits only touch the operation crop. Checkpoints may
                # clear old content, so their affected region also covers the
                # previous baseline and the locally visible segment. This keeps
                # the hot path independent of the complete source-volume size.
                current_crop = None
                current_bounds = None
                if operation.get("operation_kind") == "snapshot":
                    current_crop, current_bounds = self._read_mask_crop(
                        node, segment_id
                    )
                    affected_bounds = _bounds_union(
                        self.baseline_bounds.get(key), current_bounds
                    )
                    affected_bounds = _bounds_union(
                        affected_bounds, operation_bounds
                    )
                    current_region = self._crop_region(
                        current_crop, current_bounds, affected_bounds
                    )
                else:
                    affected_bounds = operation_bounds
                    current_region = self._read_mask_region(
                        node, segment_id, affected_bounds
                    )

                az0, az1, ay0, ay1, ax0, ax1 = affected_bounds
                server_before_region = _baseline_region(baseline, affected_bounds)
                local_changes = (
                    current_region != server_before_region
                    if self.initial_sync_complete
                    else np.zeros(current_region.shape, dtype=bool)
                )

                if operation.get("operation_kind") == "snapshot":
                    if isinstance(baseline, ChunkedMaskBaseline):
                        baseline.clear()
                    else:
                        baseline.fill(0)
                if isinstance(baseline, ChunkedMaskBaseline):
                    baseline.set_region(operation_bounds, changed, values)
                else:
                    baseline_operation_crop = baseline[
                        oz0:oz1, oy0:oy1, ox0:ox1
                    ]
                    baseline_operation_crop[changed] = values[changed]
                server_after_region = _baseline_region(baseline, affected_bounds)
                visible_region = server_after_region.copy()
                visible_region[local_changes] = current_region[local_changes]

                applied_incrementally = True
                if np.any(current_region != visible_region):
                    crop_updater = getattr(
                        self.owner, "update_segment_binary_labelmap_crop", None
                    )
                    applied_incrementally = bool(
                        callable(crop_updater)
                        and crop_updater(
                            current_region,
                            visible_region,
                            affected_bounds,
                            node,
                            segment_id,
                            self.owner.get_volume_node(),
                        )
                    )
                if not applied_incrementally:
                    # Geometry with a non-linear parent transform uses Slicer's
                    # general full-volume resampling path for correctness.
                    visible_full = _baseline_dense(baseline)
                    visible_full[
                        az0:az1, ay0:ay1, ax0:ax1
                    ][local_changes] = current_region[local_changes]
                    self.owner.update_segment_binary_labelmap_from_array(
                        visible_full, node, segment_id, self.owner.get_volume_node()
                    )
                self._ensure_independent_segment_labelmap(node, segment_id)
                node.Modified()
                try:
                    self.owner.refresh_segmentation_display(node, segment_id)
                except Exception:
                    pass
                if operation.get("operation_kind") == "snapshot":
                    local_nonzero = _delta_bounds(values != 0)
                    if local_nonzero is None:
                        self.baseline_bounds[key] = None
                    else:
                        lz0, lz1, ly0, ly1, lx0, lx1 = local_nonzero
                        self.baseline_bounds[key] = [
                            oz0 + lz0,
                            oz0 + lz1,
                            oy0 + ly0,
                            oy0 + ly1,
                            ox0 + lx0,
                            ox0 + lx1,
                        ]
                else:
                    self.baseline_bounds[key] = _bounds_union(
                        self.baseline_bounds.get(key),
                        operation.get("voxel_bbox"),
                    )
                if np.any(local_changes):
                    self.dirty_segments.add(key)
                self._known_segment_ids.add(segment_id)
                self._remember_segment_revision(node, segment_id)
                self._set_segment_collaboration_tags(segment_id)
                actor = (
                    "You"
                    if operation.get("author") == self.user_name
                    else operation.get("author") or "Collaborator"
                )
                if operation.get("metadata_update"):
                    if metadata_before is None:
                        activity = (
                            f"{actor} created label “"
                            f"{operation.get('segment_name') or segment_id}”"
                        )
                    else:
                        changes = []
                        if metadata_after and (
                            metadata_before.get("segment_name")
                            != metadata_after.get("segment_name")
                        ):
                            changes.append(
                                f"renamed “{metadata_before.get('segment_name')}” to "
                                f"“{metadata_after.get('segment_name')}”"
                            )
                        if metadata_after and (
                            metadata_before.get("color_hex")
                            != metadata_after.get("color_hex")
                        ):
                            changes.append(
                                f"changed its color to {metadata_after.get('color_hex')}"
                            )
                        activity = (
                            f"{actor} " + " and ".join(changes)
                            if changes
                            else f"{actor} updated label properties for “{operation.get('segment_name') or segment_id}”"
                        )
                else:
                    activity = (
                        f"{actor} updated label "
                        f"{operation.get('segment_name') or segment_id} · sequence {sequence}"
                    )
                self._append_activity(activity)
                if operation.get("author") != self.user_name:
                    highlight_changed = changed
                    if (
                        operation.get("operation_kind") == "snapshot"
                        and not np.any(values)
                    ):
                        highlight_changed = None
                    if highlight_changed is not None and np.any(highlight_changed):
                        try:
                            import qt

                            highlight_crop = highlight_changed.astype(np.uint8).copy()
                            highlight_bounds = list(operation_bounds)
                            highlight_author = str(
                                operation.get("author") or "Collaborator"
                            )
                            qt.QTimer.singleShot(
                                100,
                                lambda crop=highlight_crop, bounds=highlight_bounds, author=highlight_author: (
                                    self.owner.show_remote_change_highlight_crop(
                                        crop, bounds, author
                                    )
                                ),
                            )
                        except Exception:
                            pass
                    slicer.util.showStatusMessage(
                        f"Live: {operation.get('author')} updated "
                        f"{operation.get('segment_name') or segment_id}",
                        1800,
                    )
                else:
                    self._acknowledge_local_operation(operation)
                    self._schedule_segment_verification(key, duration=0.8)
                self.last_sequence = sequence
                applied_count += 1
            except Exception as exc:
                self._show_error(f"Could not apply live edit {sequence}: {exc}")
                return
            finally:
                self._applying_remote = False
        if applied_count:
            self.session_metrics.record("apply", time.monotonic() - apply_started)
            self.session_metrics.increment("operations_applied", applied_count)

    def _update_presence(self, users):
        now = time.monotonic()
        reported = {
            str(entry.get("user")): dict(entry)
            for entry in users
            if entry.get("user") and entry.get("user") != self.user_name
        }
        previous = dict(self.presence_by_user)
        for user, entry in reported.items():
            entry.pop("_presence_delayed", None)
            entry.pop("_presence_delay_seconds", None)
            self._presence_last_observed[user] = now
        for user, entry in previous.items():
            if user in reported:
                continue
            last_observed = float(self._presence_last_observed.get(user, 0.0))
            delay = now - last_observed if last_observed else float("inf")
            if delay <= PRESENCE_DISPLAY_GRACE_SECONDS:
                delayed = dict(entry)
                delayed["_presence_delayed"] = True
                delayed["_presence_delay_seconds"] = round(delay, 1)
                reported[user] = delayed
            else:
                self._presence_last_observed.pop(user, None)
        others = [reported[user] for user in sorted(reported, key=str.casefold)]
        selected = self._combo_current_text(self.collaborator_combo)
        self.presence_by_user = {str(entry["user"]): dict(entry) for entry in others}
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
            online_labels = []
            delayed_labels = []
            for entry in others:
                target = entry.get("active_segment_name") or entry.get("active_segment_id")
                peer_version = str(entry.get("plugin_version") or "unknown version")
                label = f"{entry['user']} (v{peer_version})"
                label = f"{label} — {target}" if target else label
                if entry.get("_presence_delayed"):
                    delayed_labels.append(
                        f"{label} (last seen "
                        f"{float(entry.get('_presence_delay_seconds', 0.0)):.0f} s ago)"
                    )
                else:
                    online_labels.append(label)
            lines = []
            if online_labels:
                lines.append("Online: " + "  •  ".join(online_labels))
            if delayed_labels:
                lines.append(
                    "Presence delayed — waiting for NAS/server: "
                    + "  •  ".join(delayed_labels)
                )
            self.users_label.setText("\n".join(lines))
            mismatched = [
                entry
                for entry in others
                if int(entry.get("protocol_version", 0) or 0)
                != COLLABORATION_PROTOCOL_VERSION
            ]
            if mismatched:
                self.users_label.setText(
                    self._text(self.users_label)
                    + "\n⚠ Protocol mismatch: install the same current plugin release on every computer."
                )
        follow = self.follow_checkbox.checked
        follow = follow() if callable(follow) else follow
        target = self._combo_current_text(self.collaborator_combo)
        if follow and target in self.presence_by_user:
            self._apply_location(self.presence_by_user[target])
        self._update_lock_controls()
