"""Advanced collaboration data helpers shared by transports and Slicer UI.

The module intentionally has no Slicer dependency.  It can therefore be tested
with regular Python and used by the optional FastAPI service as well.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import numpy as np

ROOM_ROLES = ("viewer", "editor", "reviewer", "admin")
REVIEW_STATES = (
    "draft",
    "in_progress",
    "ready_for_review",
    "changes_requested",
    "approved",
)
INVITATION_FORMAT = "live-segmentation-invitation-v1"
INVITATION_FORMAT_V2 = "live-segmentation-room-v2"


class SessionMetrics:
    """Small in-memory latency/counter collector with sanitized export."""

    def __init__(self, sample_limit=200):
        self.sample_limit = max(10, int(sample_limit))
        self.started_at_epoch = time.time()
        self.samples = {}
        self.counters = {}
        self.bytes = {}
        self._queued_operations = {}

    def record(self, stage, duration_seconds, byte_count=0):
        stage = str(stage or "unknown")
        duration = max(0.0, float(duration_seconds or 0.0))
        values = self.samples.setdefault(stage, [])
        values.append(duration)
        del values[: max(0, len(values) - self.sample_limit)]
        self.counters[stage] = int(self.counters.get(stage, 0)) + 1
        self.bytes[stage] = int(self.bytes.get(stage, 0)) + max(0, int(byte_count or 0))

    def increment(self, name, amount=1):
        name = str(name or "unknown")
        self.counters[name] = int(self.counters.get(name, 0)) + int(amount)

    def operation_queued(self, operation_id, started=None):
        operation_id = str(operation_id or "")
        if operation_id:
            self._queued_operations[operation_id] = float(started or time.monotonic())

    def operation_acknowledged(self, operation_id, finished=None):
        operation_id = str(operation_id or "")
        started = self._queued_operations.pop(operation_id, None)
        if started is None:
            return None
        duration = max(0.0, float(finished or time.monotonic()) - started)
        self.record("edit_roundtrip", duration)
        return duration

    def summary(self):
        stages = {}
        for stage, values in sorted(self.samples.items()):
            if not values:
                continue
            ordered = sorted(values)
            p95_index = min(len(ordered) - 1, max(0, int(round(0.95 * len(ordered) - 1))))
            stages[stage] = {
                "count": len(values),
                "last_ms": round(values[-1] * 1000.0, 2),
                "mean_ms": round(sum(values) * 1000.0 / len(values), 2),
                "p95_ms": round(ordered[p95_index] * 1000.0, 2),
                "max_ms": round(max(values) * 1000.0, 2),
                "bytes": int(self.bytes.get(stage, 0)),
            }
        return {
            "format": "live-segmentation-session-metrics-v1",
            "session_seconds": round(max(0.0, time.time() - self.started_at_epoch), 2),
            "stages": stages,
            "counters": {key: int(value) for key, value in sorted(self.counters.items())},
            "pending_roundtrips": len(self._queued_operations),
        }


class PendingOperationJournal:
    """Crash-safe local journal for unacknowledged operations.

    The journal root is local application storage. Loading the journal never
    resolves or touches a collaboration path contained in its context.
    """

    FORMAT = "live-segmentation-pending-operations-v1"

    def __init__(self, path):
        self.path = Path(path)

    def write(self, context, operations):
        operations = [dict(item) for item in operations or []]
        if not operations:
            self.clear()
            return
        payload = {
            "format": self.FORMAT,
            "updated_at_epoch": time.time(),
            "context": dict(context or {}),
            "operations": operations,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{time.time_ns()}.tmp"
        )
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def read(self, expected_context=None):
        if not self.path.is_file():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if payload.get("format") != self.FORMAT:
            return []
        if expected_context is not None:
            actual = payload.get("context") or {}
            for key, value in dict(expected_context).items():
                if str(actual.get(key) or "") != str(value or ""):
                    return []
        return [dict(item) for item in payload.get("operations") or [] if isinstance(item, dict)]

    def clear(self):
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


def segmentation_quality_report(masks, min_component_voxels=20):
    """Return deterministic empty/component/overlap checks for label masks."""
    normalized = {
        str(segment_id): np.asarray(mask, dtype=bool)
        for segment_id, mask in dict(masks or {}).items()
    }
    issues = []
    labels = {}
    try:
        from scipy import ndimage
    except ImportError:  # Slicer normally ships SciPy; keep a bounded fallback.
        ndimage = None
    for segment_id, mask in normalized.items():
        voxel_count = int(np.count_nonzero(mask))
        item = {"segment_id": segment_id, "voxels": voxel_count, "components": 0}
        if voxel_count == 0:
            issues.append({"type": "empty", "segment_id": segment_id, "voxels": 0})
            labels[segment_id] = item
            continue
        if ndimage is not None:
            component_map, component_count = ndimage.label(mask)
            sizes = np.bincount(component_map.ravel())[1:]
        else:
            remaining = {tuple(int(value) for value in point) for point in np.argwhere(mask)}
            sizes = []
            while remaining:
                pending = [remaining.pop()]
                size = 0
                while pending:
                    point = pending.pop()
                    size += 1
                    for axis in range(mask.ndim):
                        for direction in (-1, 1):
                            neighbor = list(point)
                            neighbor[axis] += direction
                            neighbor = tuple(neighbor)
                            if neighbor in remaining:
                                remaining.remove(neighbor)
                                pending.append(neighbor)
                sizes.append(size)
            component_count = len(sizes)
        small = [int(value) for value in sizes if int(value) < int(min_component_voxels)]
        item["components"] = int(component_count)
        item["small_components"] = len(small)
        if component_count > 1:
            issues.append(
                {
                    "type": "disconnected",
                    "segment_id": segment_id,
                    "components": int(component_count),
                }
            )
        if small:
            issues.append(
                {
                    "type": "small-components",
                    "segment_id": segment_id,
                    "count": len(small),
                    "smallest_voxels": min(small),
                }
            )
        labels[segment_id] = item
    segment_ids = sorted(normalized, key=str.casefold)
    for index, first_id in enumerate(segment_ids):
        for second_id in segment_ids[index + 1 :]:
            if normalized[first_id].shape != normalized[second_id].shape:
                continue
            overlap = int(np.count_nonzero(normalized[first_id] & normalized[second_id]))
            if overlap:
                issues.append(
                    {
                        "type": "overlap",
                        "segments": [first_id, second_id],
                        "voxels": overlap,
                    }
                )
    return {
        "format": "live-segmentation-quality-report-v1",
        "labels": labels,
        "issues": issues,
        "issue_count": len(issues),
        "min_component_voxels": int(min_component_voxels),
    }


def operation_changed_voxel_count(operation, decode_mask_delta):
    """Return the number of explicitly changed voxels in an operation."""
    changed, _ = decode_mask_delta(operation)
    return int(np.count_nonzero(changed))


def operation_overlap_count(first, second, decode_mask_delta):
    """Return changed-voxel overlap for two operations in global IJK space."""
    if str(first.get("segment_id")) != str(second.get("segment_id")):
        return 0
    # Deleting a label conflicts semantically with every concurrent edit of
    # that label, even though the compact tombstone has no changed mask voxels.
    if first.get("segment_deleted") or second.get("segment_deleted"):
        return 1
    first_bounds = [int(value) for value in first["voxel_bbox"]]
    second_bounds = [int(value) for value in second["voxel_bbox"]]
    overlap = (
        max(first_bounds[0], second_bounds[0]),
        min(first_bounds[1], second_bounds[1]),
        max(first_bounds[2], second_bounds[2]),
        min(first_bounds[3], second_bounds[3]),
        max(first_bounds[4], second_bounds[4]),
        min(first_bounds[5], second_bounds[5]),
    )
    if overlap[0] >= overlap[1] or overlap[2] >= overlap[3] or overlap[4] >= overlap[5]:
        return 0
    first_changed, _ = decode_mask_delta(first)
    second_changed, _ = decode_mask_delta(second)

    def crop(changed, bounds):
        return changed[
            overlap[0] - bounds[0] : overlap[1] - bounds[0],
            overlap[2] - bounds[2] : overlap[3] - bounds[2],
            overlap[4] - bounds[4] : overlap[5] - bounds[4],
        ]

    return int(np.count_nonzero(crop(first_changed, first_bounds) & crop(second_changed, second_bounds)))


def operation_summary(operation, decode_mask_delta=None):
    """Return a payload-free audit/timeline representation."""
    result = {
        "sequence": int(operation.get("sequence", 0)),
        "author": str(operation.get("author") or ""),
        "segment_id": str(operation.get("segment_id") or ""),
        "segment_name": str(operation.get("segment_name") or operation.get("segment_id") or ""),
        "operation_kind": str(operation.get("operation_kind") or "patch"),
        "created_at": operation.get("created_at"),
        "snapshot_group_id": operation.get("snapshot_group_id"),
        "snapshot_label": operation.get("snapshot_label"),
        "system_snapshot": bool(operation.get("system_snapshot", False)),
        "segment_deleted": bool(operation.get("segment_deleted", False)),
        "metadata_update": bool(operation.get("metadata_update", False)),
        "client_operation_id": operation.get("client_operation_id"),
        "undo_of_sequence": operation.get("undo_of_sequence"),
    }
    if operation.get("changed_voxels") is not None:
        result["changed_voxels"] = int(operation["changed_voxels"])
    elif decode_mask_delta is not None:
        result["changed_voxels"] = operation_changed_voxel_count(
            operation, decode_mask_delta
        )
    return result


def reconstruct_snapshot_operations(
    operations,
    target_sequence,
    apply_mask_delta,
    encode_mask_delta,
):
    """Reconstruct all known segments at a historical global sequence."""
    ordered = sorted(operations, key=lambda item: int(item.get("sequence", 0)))
    metadata = {}
    masks = {}
    active_segments = set()
    target_sequence = int(target_sequence)
    for operation in ordered:
        if int(operation.get("sequence", 0)) > target_sequence:
            break
        segment_id = str(operation.get("segment_id") or "")
        if not segment_id:
            continue
        if segment_id not in metadata or operation.get("metadata_update"):
            metadata[segment_id] = {
                "segment_id": segment_id,
                "segment_name": operation.get("segment_name") or segment_id,
                "color_hex": operation.get("color_hex") or "#4A90E2",
            }
        if operation.get("segment_deleted"):
            masks.pop(segment_id, None)
            active_segments.discard(segment_id)
            continue
        shape = tuple(int(value) for value in operation["volume_shape"])
        current = masks.get(segment_id)
        if current is None or current.shape != shape:
            current = np.zeros(shape, dtype=np.uint8)
        masks[segment_id] = apply_mask_delta(current, operation)
        active_segments.add(segment_id)
    result = []
    for segment_id in sorted(active_segments, key=str.casefold):
        empty = np.zeros_like(masks[segment_id])
        encoded = encode_mask_delta(empty, masks[segment_id], replace=True)
        if encoded is None:
            continue
        result.append({**metadata[segment_id], **encoded})
    return result


def validate_material_template(payload):
    """Normalize a portable room material/label template."""
    if not isinstance(payload, dict):
        raise ValueError("Material template must be a JSON object")
    segments = payload.get("segments")
    if not isinstance(segments, list):
        raise ValueError("Material template must contain a segments list")
    normalized = []
    seen = set()
    for item in segments:
        if not isinstance(item, dict):
            raise ValueError("Each template segment must be an object")
        segment_id = str(item.get("id") or item.get("segment_id") or "").strip()
        name = str(item.get("name") or segment_id).strip()
        color = str(item.get("color") or item.get("color_hex") or "#4A90E2").upper()
        if not segment_id or not name:
            raise ValueError("Template segments require an id and name")
        if segment_id in seen:
            raise ValueError(f"Duplicate template segment id: {segment_id}")
        if len(color) != 7 or not color.startswith("#"):
            raise ValueError(f"Invalid color for {name}; use #RRGGBB")
        int(color[1:], 16)
        seen.add(segment_id)
        normalized.append(
            {
                "id": segment_id,
                "name": name,
                "color": color,
                "terminology": str(item.get("terminology") or ""),
            }
        )
    return {
        "format": "live-segmentation-material-template-v1",
        "name": str(payload.get("name") or "Room material template"),
        "segments": normalized,
    }


def build_invitation(
    transport,
    room_name,
    volume_signature,
    location,
    template=None,
    fallback_shared_folder=None,
    access_code=None,
):
    """Create a portable room invitation payload.

    Server API keys are never included. Direct-LAN invitations intentionally
    carry their temporary session code and must therefore be shared privately.
    """
    if transport not in {"shared-folder", "server", "direct-lan"}:
        raise ValueError("Unsupported invitation transport")
    result = {
        "format": INVITATION_FORMAT_V2 if transport == "direct-lan" else INVITATION_FORMAT,
        "transport": transport,
        "room_name": str(room_name or "").strip(),
        "volume_signature": str(volume_signature or "").strip(),
        "location": str(location or "").strip(),
    }
    if not result["room_name"] or not result["volume_signature"] or not result["location"]:
        raise ValueError("Invitation is missing room, dataset signature, or location")
    if template is not None:
        result["material_template"] = validate_material_template(template)
    if fallback_shared_folder:
        result["fallback_shared_folder"] = str(fallback_shared_folder).strip()
    if access_code:
        result["access_code"] = str(access_code).strip()
    return result


def parse_invitation(payload):
    if isinstance(payload, (str, bytes)):
        payload = json.loads(payload)
    if not isinstance(payload, dict) or payload.get("format") not in {
        INVITATION_FORMAT,
        INVITATION_FORMAT_V2,
    }:
        raise ValueError("This is not a Live Segmentation invitation")
    return build_invitation(
        payload.get("transport"),
        payload.get("room_name"),
        payload.get("volume_signature"),
        payload.get("location"),
        payload.get("material_template"),
        payload.get("fallback_shared_folder"),
        payload.get("access_code"),
    )


def stable_user_color(user_name):
    digest = hashlib.sha256(str(user_name or "user").encode("utf-8")).digest()
    return tuple(0.25 + (value / 255.0) * 0.65 for value in digest[:3])


def sha256_file(path, chunk_size=1024 * 1024):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while True:
            block = source.read(int(chunk_size))
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()
