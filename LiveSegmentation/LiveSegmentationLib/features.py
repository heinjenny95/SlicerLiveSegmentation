"""Advanced collaboration data helpers shared by transports and Slicer UI.

The module intentionally has no Slicer dependency.  It can therefore be tested
with regular Python and used by the optional FastAPI service as well.
"""

from __future__ import annotations

import hashlib
import json
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


def operation_changed_voxel_count(operation, decode_mask_delta):
    """Return the number of explicitly changed voxels in an operation."""
    changed, _ = decode_mask_delta(operation)
    return int(np.count_nonzero(changed))


def operation_overlap_count(first, second, decode_mask_delta):
    """Return changed-voxel overlap for two operations in global IJK space."""
    if str(first.get("segment_id")) != str(second.get("segment_id")):
        return 0
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
        "client_operation_id": operation.get("client_operation_id"),
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
    shapes = {}
    for operation in ordered:
        segment_id = str(operation.get("segment_id") or "")
        if not segment_id:
            continue
        metadata[segment_id] = {
            "segment_id": segment_id,
            "segment_name": operation.get("segment_name") or segment_id,
            "color_hex": operation.get("color_hex") or "#4A90E2",
        }
        shapes[segment_id] = tuple(int(value) for value in operation["volume_shape"])
    masks = {
        segment_id: np.zeros(shape, dtype=np.uint8)
        for segment_id, shape in shapes.items()
    }
    target_sequence = int(target_sequence)
    for operation in ordered:
        if int(operation.get("sequence", 0)) > target_sequence:
            break
        segment_id = str(operation.get("segment_id") or "")
        if segment_id in masks:
            masks[segment_id] = apply_mask_delta(masks[segment_id], operation)
    result = []
    for segment_id in sorted(masks, key=str.casefold):
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


def build_invitation(transport, room_name, volume_signature, location, template=None):
    """Create a secret-free portable room invitation payload."""
    if transport not in {"shared-folder", "server"}:
        raise ValueError("Unsupported invitation transport")
    result = {
        "format": INVITATION_FORMAT,
        "transport": transport,
        "room_name": str(room_name or "").strip(),
        "volume_signature": str(volume_signature or "").strip(),
        "location": str(location or "").strip(),
    }
    if not result["room_name"] or not result["volume_signature"] or not result["location"]:
        raise ValueError("Invitation is missing room, dataset signature, or location")
    if template is not None:
        result["material_template"] = validate_material_template(template)
    return result


def parse_invitation(payload):
    if isinstance(payload, (str, bytes)):
        payload = json.loads(payload)
    if not isinstance(payload, dict) or payload.get("format") != INVITATION_FORMAT:
        raise ValueError("This is not a Live Segmentation invitation")
    return build_invitation(
        payload.get("transport"),
        payload.get("room_name"),
        payload.get("volume_signature"),
        payload.get("location"),
        payload.get("material_template"),
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
