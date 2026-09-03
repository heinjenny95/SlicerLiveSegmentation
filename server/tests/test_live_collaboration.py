from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest

LIVE_CORE = (
    Path(__file__).resolve().parents[2]
    / "LiveSegmentation"
    / "LiveSegmentationLib"
)
sys.path.insert(0, str(LIVE_CORE))

import collaboration as collaboration_module  # noqa: E402
from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from collaboration import (  # noqa: E402
    ChunkedMaskBaseline,
    HybridRoomClient,
    LanRelayServer,
    LanRoomClient,
    LiveCollaborationError,
    LiveRoomClient,
    SharedFolderRoomClient,
    _atomic_temporary_path,
    _is_transient_shared_read_error,
    _read_json_file,
    apply_mask_delta,
    decode_recent_shared_folders,
    encode_chunked_mask_snapshot,
    encode_mask_crop_delta,
    encode_mask_crop_delta_after_operations,
    encode_mask_crop_snapshot,
    encode_mask_delta,
    update_recent_shared_folders,
    validate_remote_server_url,
    volume_signature,
)
from fastapi.testclient import TestClient  # noqa: E402
from features import (  # noqa: E402
    PendingOperationJournal,
    SessionMetrics,
    build_invitation,
    parse_invitation,
    reconstruct_snapshot_operations,
    segmentation_quality_report,
    validate_material_template,
)

from scripts.generate_user_tokens import generate_user_tokens  # noqa: E402


def operation_payload(segment_id, previous, current, replace=False, operation_id="client-op-1"):
    return {
        "client_operation_id": operation_id,
        "segment_id": segment_id,
        "segment_name": segment_id,
        "color_hex": "#37E8B8",
        **encode_mask_delta(previous, current, replace=replace),
    }


def test_shared_folder_watchdogs_allow_institutional_smb_latency():
    assert collaboration_module.SHARED_FOLDER_JOIN_TIMEOUT_SECONDS == 15.0
    assert collaboration_module.SHARED_FOLDER_SLOW_RESPONSE_SECONDS == 10.0
    assert collaboration_module.SHARED_FOLDER_RESPONSE_TIMEOUT_SECONDS == 30.0


def test_shared_folder_watchdog_warns_before_it_disconnects():
    state = collaboration_module.shared_folder_response_state
    assert state(9.999) == "live"
    assert state(10.0) == "slow"
    assert state(29.999) == "slow"
    assert state(30.0) == "offline"


def test_recent_shared_folder_history_is_bounded_and_does_not_touch_paths():
    raw = json.dumps(
        [
            r"\\server\share",
            r"C:/research/live",
            r"c:\research\live",
            "",
            "/mnt/case-sensitive",
            "/mnt/Case-sensitive",
        ]
    )
    assert decode_recent_shared_folders(raw, limit=4) == [
        r"\\server\share",
        r"C:/research/live",
        "/mnt/case-sensitive",
        "/mnt/Case-sensitive",
    ]


def test_recent_shared_folder_history_moves_successful_path_to_front():
    assert update_recent_shared_folders(
        [r"\\server\old", r"D:\shared"],
        r"d:/shared",
        limit=2,
    ) == [r"d:/shared", r"\\server\old"]


def test_remote_server_security_requires_https_except_loopback_or_explicit_test():
    assert validate_remote_server_url("https://collaboration.example.org") == (
        "https://collaboration.example.org"
    )
    assert validate_remote_server_url("http://127.0.0.1:8000") == "http://127.0.0.1:8000"
    with pytest.raises(ValueError, match="must use HTTPS"):
        validate_remote_server_url("http://collaboration.example.org")
    assert validate_remote_server_url(
        "http://192.168.1.20:8000", allow_insecure_http=True
    ) == "http://192.168.1.20:8000"


def test_token_generator_creates_unique_identity_bound_secrets():
    tokens = generate_user_tokens(["Alice", "Bob"])
    assert set(tokens.values()) == {"Alice", "Bob"}
    assert len(tokens) == 2
    assert all(len(token) >= 40 for token in tokens)
    with pytest.raises(ValueError, match="Duplicate"):
        generate_user_tokens(["Alice", " alice "])


def test_shared_folder_preflight_finds_second_computer_without_joining(tmp_path):
    alice = SharedFolderRoomClient(tmp_path, "alice")
    bob = SharedFolderRoomClient(tmp_path, "bob")
    signature = "a" * 64

    first = alice.preflight("publication room", signature)
    assert first["status"] == "warning"
    assert not (alice.rooms_root / alice._room_key("publication room")).exists()

    second = bob.preflight("publication room", signature)
    assert second["status"] == "pass"
    second_peer = next(check for check in second["checks"] if check["id"] == "peer-computer")
    assert second_peer["status"] == "pass"

    refreshed = alice.preflight("publication room", signature)
    assert refreshed["status"] == "pass"


def test_shared_folder_preflight_reports_peer_dataset_mismatch(tmp_path):
    alice = SharedFolderRoomClient(tmp_path, "alice")
    bob = SharedFolderRoomClient(tmp_path, "bob")
    alice.preflight("mismatch room", "a" * 64)
    report = bob.preflight("mismatch room", "b" * 64)
    peer = next(check for check in report["checks"] if check["id"] == "peer-computer")
    assert report["status"] == "fail"
    assert peer["status"] == "fail"


def deletion_payload(segment_id, shape, operation_id="delete-segment-1"):
    return {
        "client_operation_id": operation_id,
        "segment_id": segment_id,
        "segment_name": segment_id,
        "color_hex": "#37E8B8",
        "segment_deleted": True,
        **encode_mask_crop_snapshot(None, None, shape),
    }


def test_atomic_temporary_name_stays_short_for_long_operation_names(tmp_path):
    destination = tmp_path / ("0" * 20 + "--" + "a" * 32 + ".json")
    temporary = _atomic_temporary_path(destination)
    assert temporary.parent == destination.parent
    assert temporary.name.startswith(".tmp-")
    assert len(temporary.name) <= 26
    assert destination.name not in temporary.name


def test_shared_json_read_retries_short_smb_visibility_gap(tmp_path, monkeypatch):
    path = tmp_path / "room.json"
    path.write_text('{"room_id":"retry-room"}\n', encoding="utf-8")
    original_open = Path.open
    attempts = {"count": 0}

    def flaky_open(candidate, *args, **kwargs):
        if candidate == path and attempts["count"] < 2:
            attempts["count"] += 1
            raise PermissionError("temporary SMB sharing violation")
        attempts["count"] += 1
        return original_open(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "open", flaky_open)
    monkeypatch.setattr(collaboration_module.time, "sleep", lambda _delay: None)

    assert _read_json_file(path) == {"room_id": "retry-room"}
    assert attempts["count"] == 3


def test_shared_json_read_failure_is_classified_for_health_confirmation(
    tmp_path, monkeypatch
):
    path = tmp_path / "room.json"
    attempts = {"count": 0}

    def unavailable_open(_candidate, *args, **kwargs):
        del args, kwargs
        attempts["count"] += 1
        raise OSError("network path unavailable")

    monkeypatch.setattr(Path, "open", unavailable_open)
    monkeypatch.setattr(collaboration_module.time, "sleep", lambda _delay: None)

    with pytest.raises(LiveCollaborationError) as captured:
        _read_json_file(path)
    assert attempts["count"] == len(collaboration_module.SHARED_JSON_READ_RETRY_DELAYS) + 1
    assert _is_transient_shared_read_error(str(captured.value))


def test_mask_deltas_preserve_independent_edits_and_order_overlaps():
    empty = np.zeros((5, 6, 7), dtype=np.uint8)
    alice = empty.copy()
    alice[1, 2, 3] = 1
    bob = empty.copy()
    bob[4, 5, 6] = 1

    alice_delta = encode_mask_delta(empty, alice)
    bob_delta = encode_mask_delta(empty, bob)
    combined = apply_mask_delta(apply_mask_delta(empty, alice_delta), bob_delta)
    assert combined[1, 2, 3] == 1
    assert combined[4, 5, 6] == 1
    assert int(combined.sum()) == 2

    bob_removes_alice_voxel = combined.copy()
    bob_removes_alice_voxel[1, 2, 3] = 0
    removal = encode_mask_delta(combined, bob_removes_alice_voxel)
    final = apply_mask_delta(combined, removal)
    assert final[1, 2, 3] == 0
    assert final[4, 5, 6] == 1


def test_snapshot_replaces_preexisting_local_voxels():
    remote = np.zeros((4, 4, 4), dtype=np.uint8)
    remote[1:3, 1:3, 1:3] = 1
    local = np.ones_like(remote)
    snapshot = encode_mask_delta(np.zeros_like(remote), remote, replace=True)
    assert np.array_equal(apply_mask_delta(local, snapshot), remote)
    assert snapshot["voxel_bbox"] == [1, 3, 1, 3, 1, 3]


def test_empty_snapshot_clears_without_a_full_volume_payload():
    empty = np.zeros((40, 50, 60), dtype=np.uint8)
    local = np.ones_like(empty)
    snapshot = encode_mask_delta(empty, empty, replace=True)
    assert snapshot["voxel_bbox"] == [0, 1, 0, 1, 0, 1]
    assert np.array_equal(apply_mask_delta(local, snapshot), empty)


def test_historical_reconstruction_removes_and_can_recreate_a_deleted_label():
    shape = (4, 4, 4)
    empty = np.zeros(shape, dtype=np.uint8)
    mask = empty.copy()
    mask[1:3, 1:3, 1:3] = 1
    created = {
        **operation_payload("Organ", empty, mask, replace=True),
        "sequence": 1,
        "author": "alice",
    }
    deleted = {
        **deletion_payload("Organ", shape),
        "sequence": 2,
        "author": "alice",
    }
    recreated = {
        **operation_payload(
            "Organ", empty, mask, replace=True, operation_id="recreate-segment-1"
        ),
        "sequence": 3,
        "author": "bob",
    }

    before_delete = reconstruct_snapshot_operations(
        [created, deleted, recreated], 1, apply_mask_delta, encode_mask_delta
    )
    after_delete = reconstruct_snapshot_operations(
        [created, deleted, recreated], 2, apply_mask_delta, encode_mask_delta
    )
    after_recreate = reconstruct_snapshot_operations(
        [created, deleted, recreated], 3, apply_mask_delta, encode_mask_delta
    )

    assert [item["segment_id"] for item in before_delete] == ["Organ"]
    assert after_delete == []
    assert [item["segment_id"] for item in after_recreate] == ["Organ"]
    restored = apply_mask_delta(empty, after_recreate[0])
    assert np.array_equal(restored, mask)


def test_cropped_delta_only_compares_effective_segment_region():
    shape = (200, 300, 400)
    previous = np.zeros(shape, dtype=np.uint8)
    previous[90:93, 120:123, 210:213] = 1
    current_crop = np.ones((4, 3, 3), dtype=np.uint8)
    encoded = encode_mask_crop_delta(
        previous,
        current_crop,
        [90, 94, 120, 123, 210, 213],
        [90, 93, 120, 123, 210, 213],
        shape,
    )
    assert encoded["volume_shape"] == list(shape)
    assert encoded["voxel_bbox"] == [93, 94, 120, 123, 210, 213]
    result = apply_mask_delta(previous, encoded)
    assert int(result.sum()) == 36


def test_chunked_baseline_scales_with_touched_chunks_not_volume_size():
    shape = (4096, 4096, 4096)
    baseline = ChunkedMaskBaseline(shape, chunk_size=64)
    components = (
        [10, 14, 20, 24, 30, 34],
        [2000, 2004, 2100, 2104, 2200, 2204],
        [4080, 4084, 4000, 4004, 3900, 3904],
    )
    values = np.ones((4, 4, 4), dtype=np.uint8)
    for bounds in components:
        baseline.set_region(bounds, values != 0, values)

    assert baseline.chunk_count == 3
    assert baseline.allocated_bytes <= 3 * 64**3
    assert int(baseline.region(components[1]).sum()) == 64

    baseline.set_region(
        components[1], np.ones_like(values, dtype=bool), np.zeros_like(values)
    )
    assert baseline.chunk_count == 2
    assert not np.any(baseline.region(components[1]))


def test_chunked_baseline_encodes_a_small_edit_in_huge_geometry():
    shape = (4096, 4096, 4096)
    bounds = [3000, 3004, 2000, 2004, 1000, 1004]
    previous_crop = np.ones((4, 4, 4), dtype=np.uint8)
    previous_crop[3, 3, 3] = 0
    baseline = ChunkedMaskBaseline.from_crop(shape, previous_crop, bounds)
    current_crop = np.ones_like(previous_crop)
    encoded = encode_mask_crop_delta_after_operations(
        baseline,
        current_crop,
        bounds,
        bounds,
        shape,
        [],
    )
    assert encoded["voxel_bbox"] == [3003, 3004, 2003, 2004, 1003, 1004]
    changed, values = collaboration_module.decode_mask_delta(encoded)
    assert int(changed.sum()) == 1
    assert int(values[changed].sum()) == 1
    assert baseline.allocated_bytes == 64**3


def test_chunked_snapshot_never_materializes_the_sparse_global_extent():
    shape = (4096, 4096, 4096)
    baseline = ChunkedMaskBaseline(shape, chunk_size=64)
    components = (
        [4, 8, 8, 12, 12, 16],
        [2048, 2052, 2112, 2116, 2176, 2180],
        [4088, 4092, 4000, 4004, 3900, 3904],
    )
    values = np.ones((4, 4, 4), dtype=np.uint8)
    for bounds in components:
        baseline.set_region(bounds, values != 0, values)

    operations = encode_chunked_mask_snapshot(baseline)

    assert len(operations) == 3
    assert operations[0]["operation_kind"] == "snapshot"
    assert [item["operation_kind"] for item in operations[1:]] == ["patch", "patch"]
    for operation in operations:
        z0, z1, y0, y1, x0, x1 = operation["voxel_bbox"]
        assert max(z1 - z0, y1 - y0, x1 - x0) <= 64

    reconstructed = ChunkedMaskBaseline(shape, chunk_size=64)
    for operation in operations:
        if operation["operation_kind"] == "snapshot":
            reconstructed.clear()
        changed, decoded_values = collaboration_module.decode_mask_delta(operation)
        reconstructed.set_region(operation["voxel_bbox"], changed, decoded_values)
    assert reconstructed.chunk_count == 3
    assert all(int(reconstructed.region(bounds).sum()) == 64 for bounds in components)


def test_shared_snapshot_preserves_chunk_patch_semantics(tmp_path):
    shape = (128, 128, 128)
    baseline = ChunkedMaskBaseline(shape, chunk_size=64)
    first_bounds = [2, 5, 3, 6, 4, 7]
    second_bounds = [100, 103, 110, 113, 120, 123]
    values = np.ones((3, 3, 3), dtype=np.uint8)
    for bounds in (first_bounds, second_bounds):
        baseline.set_region(bounds, values != 0, values)
    segment_operations = [
        {
            "segment_id": "Organ",
            "segment_name": "Organ",
            "color_hex": "#37E8B8",
            **operation,
        }
        for operation in encode_chunked_mask_snapshot(baseline)
    ]
    alice = SharedFolderRoomClient(tmp_path, "alice")
    room = alice.join("chunked snapshot", "c" * 64)
    manifest = alice.publish_room_snapshot(
        room["id"], segment_operations, compact=True
    )

    active = alice.operations(room["id"], 0)
    assert manifest["segment_count"] == 1
    assert [item["operation_kind"] for item in active] == ["snapshot", "patch"]
    reconstructed = np.zeros(shape, dtype=np.uint8)
    for operation in active:
        reconstructed = apply_mask_delta(reconstructed, operation)
    assert int(reconstructed.sum()) == 54


def test_rapid_same_label_components_queue_without_losing_a_stroke():
    shape = (9, 48, 52)
    baseline = np.zeros(shape, dtype=np.uint8)
    states = []
    current = baseline.copy()
    for bounds in (
        (4, 5, 4, 10, 5, 11),
        (4, 5, 18, 24, 22, 28),
        (4, 5, 31, 38, 39, 46),
    ):
        z0, z1, y0, y1, x0, x1 = bounds
        current = current.copy()
        current[z0:z1, y0:y1, x0:x1] = 1
        states.append(current)

    queued = []
    previous_bounds = None
    for state in states:
        nonzero = np.nonzero(state)
        current_bounds = [
            int(nonzero[0].min()),
            int(nonzero[0].max()) + 1,
            int(nonzero[1].min()),
            int(nonzero[1].max()) + 1,
            int(nonzero[2].min()),
            int(nonzero[2].max()) + 1,
        ]
        z0, z1, y0, y1, x0, x1 = current_bounds
        operation = encode_mask_crop_delta_after_operations(
            baseline,
            state[z0:z1, y0:y1, x0:x1],
            current_bounds,
            previous_bounds,
            shape,
            queued,
        )
        assert operation is not None
        queued.append(operation)

    restored = baseline
    for operation in queued:
        restored = apply_mask_delta(restored, operation)
    assert np.array_equal(restored, states[-1])
    assert len(queued) == 3
    assert [int(apply_mask_delta(baseline, item).sum()) for item in queued] == [
        36,
        36,
        49,
    ]


def test_cropped_snapshot_announces_empty_and_nonempty_labels_compactly():
    shape = (50, 60, 70)
    empty = encode_mask_crop_snapshot(None, None, shape)
    assert empty["operation_kind"] == "snapshot"
    assert empty["voxel_bbox"] == [0, 1, 0, 1, 0, 1]
    assert not np.any(collaboration_module.decode_mask_delta(empty)[0])
    assert not np.any(apply_mask_delta(np.ones(shape, dtype=np.uint8), empty))

    crop = np.ones((2, 3, 4), dtype=np.uint8)
    nonempty = encode_mask_crop_snapshot(
        crop, [12, 14, 23, 26, 34, 38], shape
    )
    assert nonempty["voxel_bbox"] == [12, 14, 23, 26, 34, 38]
    restored = apply_mask_delta(np.zeros(shape, dtype=np.uint8), nonempty)
    assert int(restored.sum()) == 24


def test_volume_signature_detects_different_source_content():
    first = np.arange(4 * 5 * 6, dtype=np.uint16).reshape((4, 5, 6))
    second = first.copy()
    second[-1, -1, -1] += 1
    assert volume_signature(first, spacing=(1, 2, 3)) == volume_signature(
        first.copy(), spacing=(1, 2, 3)
    )
    assert volume_signature(first, spacing=(1, 2, 3)) != volume_signature(
        second, spacing=(1, 2, 3)
    )


def test_live_room_join_ordered_idempotent_operations_and_presence(client, headers):
    signature = "a" * 64
    joined = client.post(
        "/api/live/rooms/join",
        headers=headers,
        json={"room_name": "Specimen 01", "volume_signature": signature},
    )
    assert joined.status_code == 200, joined.text
    room = joined.json()
    assert room["created"] is True
    assert room["latest_sequence"] == 0

    bob_headers = {"X-LiveSeg-User": "bob"}
    rejoined = client.post(
        "/api/live/rooms/join",
        headers=bob_headers,
        json={"room_name": "specimen 01", "volume_signature": signature},
    )
    assert rejoined.status_code == 200
    assert rejoined.json()["id"] == room["id"]
    assert rejoined.json()["created"] is False

    mismatch = client.post(
        "/api/live/rooms/join",
        headers=bob_headers,
        json={"room_name": "Specimen 01", "volume_signature": "b" * 64},
    )
    assert mismatch.status_code == 409

    empty = np.zeros((3, 3, 3), dtype=np.uint8)
    first = empty.copy()
    first[0, 0, 0] = 1
    alice_operation = operation_payload("Liver", empty, first, operation_id="alice-op-1")
    created = client.post(
        f"/api/live/rooms/{room['id']}/operations",
        headers=headers,
        json=alice_operation,
    )
    assert created.status_code == 201, created.text
    assert created.json()["sequence"] == 1
    assert created.json()["duplicate"] is False

    duplicate = client.post(
        f"/api/live/rooms/{room['id']}/operations",
        headers=headers,
        json=alice_operation,
    )
    assert duplicate.status_code == 201
    assert duplicate.json()["sequence"] == 1
    assert duplicate.json()["duplicate"] is True

    second = first.copy()
    second[2, 2, 2] = 1
    bob_operation = operation_payload(
        "Liver", first, second, operation_id="bob-operation-1"
    )
    created = client.post(
        f"/api/live/rooms/{room['id']}/operations",
        headers=bob_headers,
        json=bob_operation,
    )
    assert created.status_code == 201, created.text
    assert created.json()["sequence"] == 2

    operations = client.get(
        f"/api/live/rooms/{room['id']}/operations?after=0", headers=headers
    )
    assert operations.status_code == 200
    assert [item["author"] for item in operations.json()] == ["alice", "bob"]
    assert [item["sequence"] for item in operations.json()] == [1, 2]

    alice_presence = client.post(
        f"/api/live/rooms/{room['id']}/presence",
        headers=headers,
        json={
            "active_segment_id": "Liver",
            "active_segment_name": "Leber",
        },
    )
    assert alice_presence.status_code == 200
    bob_presence = client.post(
        f"/api/live/rooms/{room['id']}/presence",
        headers=bob_headers,
        json={"active_segment_id": "Tumor", "active_segment_name": "Tumor"},
    )
    assert bob_presence.status_code == 200
    presence = {item["user"]: item for item in bob_presence.json()}
    assert presence["alice"]["active_segment_name"] == "Leber"
    assert presence["bob"]["active_segment_name"] == "Tumor"


def test_remote_server_preflight_is_non_mutating_and_finds_second_computer(client):
    payload = {
        "room_name": "internet collaboration",
        "volume_signature": "a" * 64,
        "plugin_version": "0.13.1",
        "protocol_version": 2,
    }
    alice = client.post(
        "/api/live/preflight", json=payload, headers={"X-LiveSeg-User": "alice"}
    )
    assert alice.status_code == 200
    assert alice.json()["room_exists"] is False
    assert [item["user"] for item in alice.json()["preflight_participants"]] == ["alice"]

    bob = client.post(
        "/api/live/preflight", json=payload, headers={"X-LiveSeg-User": "bob"}
    )
    assert bob.status_code == 200
    assert {item["user"] for item in bob.json()["preflight_participants"]} == {
        "alice",
        "bob",
    }

    joined = client.post(
        "/api/live/rooms/join",
        json={"room_name": payload["room_name"], "volume_signature": "b" * 64},
        headers={"X-LiveSeg-User": "alice"},
    )
    assert joined.status_code == 200
    mismatch = client.post(
        "/api/live/preflight", json=payload, headers={"X-LiveSeg-User": "alice"}
    )
    assert mismatch.status_code == 200
    assert mismatch.json()["room_exists"] is True
    assert mismatch.json()["room_compatible"] is False


def test_health_advertises_public_server_compatibility_and_security(client):
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["version"] == "0.13.1"
    assert health.json()["protocol_version"] == 2
    assert health.json()["minimum_plugin_version"] == "0.11.2"
    assert health.json()["authentication"] == "open-testing"
    assert isinstance(health.json()["server_time_epoch"], float)


def test_remote_https_client_turns_server_capabilities_into_ready_report(monkeypatch):
    remote = LiveRoomClient("https://collaboration.example.org", "alice", "private-token")

    def response(_method, _path, _payload):
        return {
            "server_version": "0.13.1",
            "protocol_version": 2,
            "minimum_plugin_version": "0.11.2",
            "server_time_epoch": time.time(),
            "authentication": "user-tokens",
            "room_exists": True,
            "room_compatible": True,
            "preflight_participants": [
                {
                    "user": "bob",
                    "plugin_version": "0.13.1",
                    "protocol_version": 2,
                    "volume_signature": "a" * 64,
                }
            ],
        }

    monkeypatch.setattr(remote, "_request", response)
    report = remote.preflight("internet room", "a" * 64)
    assert report["status"] == "pass"
    assert report["transport"] == "remote-https-server"


def test_server_transport_preserves_label_deletion_tombstone(client, headers):
    joined = client.post(
        "/api/live/rooms/join",
        headers=headers,
        json={"room_name": "Deletion API", "volume_signature": "d" * 64},
    )
    assert joined.status_code == 200
    room_id = joined.json()["id"]
    empty = np.zeros((3, 3, 3), dtype=np.uint8)
    mask = empty.copy()
    mask[1, 1, 1] = 1
    created = client.post(
        f"/api/live/rooms/{room_id}/operations",
        headers=headers,
        json=operation_payload("Organ", empty, mask, operation_id="api-create-organ"),
    )
    assert created.status_code == 201, created.text
    deleted = client.post(
        f"/api/live/rooms/{room_id}/operations",
        headers=headers,
        json={
            **deletion_payload("Organ", empty.shape, "api-delete-organ"),
            "base_sequence": 1,
        },
    )
    assert deleted.status_code == 201, deleted.text

    operations = client.get(
        f"/api/live/rooms/{room_id}/operations?after=0", headers=headers
    ).json()
    assert [item["sequence"] for item in operations] == [1, 2]
    assert operations[0]["segment_deleted"] is False
    assert operations[1]["segment_deleted"] is True


def test_shared_folder_room_orders_operations_and_retries_idempotently(tmp_path):
    signature = "c" * 64
    alice = SharedFolderRoomClient(tmp_path, "alice")
    bob = SharedFolderRoomClient(tmp_path, "bob")
    room = alice.join("Specimen 02", signature)
    rejoined = bob.join("specimen 02", signature)
    assert room["created"] is True
    assert rejoined["created"] is False
    assert room["id"] == rejoined["id"]

    empty = np.zeros((3, 3, 3), dtype=np.uint8)
    alice_mask = empty.copy()
    alice_mask[0, 0, 0] = 1
    first = operation_payload("Liver", empty, alice_mask, operation_id="shared-alice-1")
    created = alice.push_operation(room["id"], first)
    duplicate = alice.push_operation(room["id"], first)
    assert created == {"sequence": 1, "duplicate": False}
    assert duplicate == {"sequence": 1, "duplicate": True}

    combined = alice_mask.copy()
    combined[2, 2, 2] = 1
    second = operation_payload("Liver", alice_mask, combined, operation_id="shared-bob-1")
    assert bob.push_operation(room["id"], second)["sequence"] == 2
    operations = alice.operations(room["id"], 0)
    assert [item["sequence"] for item in operations] == [1, 2]
    assert [item["author"] for item in operations] == ["alice", "bob"]

    reconstructed = empty.copy()
    for operation in operations:
        reconstructed = apply_mask_delta(reconstructed, operation)
    assert np.array_equal(reconstructed, combined)


def test_shared_folder_deletion_is_live_historical_and_persistent_on_rejoin(tmp_path):
    alice = SharedFolderRoomClient(tmp_path, "alice")
    bob = SharedFolderRoomClient(tmp_path, "bob")
    room = alice.join("deletion room", "e" * 64)
    bob.join("deletion room", "e" * 64)
    shape = (4, 4, 4)
    empty = np.zeros(shape, dtype=np.uint8)
    mask = empty.copy()
    mask[1:3, 1:3, 1:3] = 1
    alice.push_operation(
        room["id"],
        operation_payload("Organ", empty, mask, operation_id="shared-create-organ"),
    )
    alice.push_operation(
        room["id"],
        {
            **deletion_payload("Organ", shape, "shared-delete-organ"),
            "base_sequence": 1,
        },
    )

    operations = bob.operations(room["id"], 0)
    assert [bool(item.get("segment_deleted")) for item in operations] == [False, True]
    assert [item["segment_id"] for item in bob.state_at_sequence(room["id"], 1)] == [
        "Organ"
    ]
    assert bob.state_at_sequence(room["id"], 2) == []
    assert bob.room_history(room["id"])[-1]["segment_deleted"] is True

    charlie = SharedFolderRoomClient(tmp_path, "charlie")
    rejoined = charlie.join("deletion room", "e" * 64)
    assert rejoined["latest_sequence"] == 2
    assert charlie.operations(rejoined["id"], 0)[-1]["segment_deleted"] is True


def test_shared_folder_room_rejects_a_different_source_volume(tmp_path):
    alice = SharedFolderRoomClient(tmp_path, "alice")
    bob = SharedFolderRoomClient(tmp_path, "bob")
    alice.join("specimen mismatch", "d" * 64)
    with pytest.raises(LiveCollaborationError, match="different source volume"):
        bob.join("SPECIMEN MISMATCH", "e" * 64)


def test_shared_folder_upgrades_old_room_before_deletion_capability_is_used(tmp_path):
    alice = SharedFolderRoomClient(tmp_path, "alice")
    alice.join("legacy room", "f" * 64)
    metadata_path = alice._room_path / "room.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["schema_version"] = 1
    metadata.pop("minimum_plugin_version", None)
    metadata.pop("capabilities", None)
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    bob = SharedFolderRoomClient(tmp_path, "bob")
    bob.join("legacy room", "f" * 64)
    upgraded = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert upgraded["schema_version"] == 2
    assert upgraded["minimum_plugin_version"] == "0.11.2"
    assert "segment-deletion-tombstone-v1" in upgraded["capabilities"]


def test_shared_folder_newer_room_explains_that_every_client_must_update(tmp_path):
    alice = SharedFolderRoomClient(tmp_path, "alice")
    alice.join("future room", "a" * 64)
    metadata_path = alice._room_path / "room.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["schema_version"] = 3
    metadata["minimum_plugin_version"] = "0.12.0"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    bob = SharedFolderRoomClient(tmp_path, "bob")
    with pytest.raises(
        LiveCollaborationError,
        match=r"requires plugin version 0\.12\.0 or newer.*every computer",
    ):
        bob.join("future room", "a" * 64)


def test_shared_folder_concurrent_writers_receive_one_global_order(tmp_path):
    signature = "f" * 64
    clients = [SharedFolderRoomClient(tmp_path, f"user-{index}") for index in range(4)]
    room = clients[0].join("concurrent room", signature)
    for client_instance in clients[1:]:
        client_instance.join("Concurrent Room", signature)

    empty = np.zeros((2, 2, 2), dtype=np.uint8)

    def push(index):
        mask = empty.copy()
        mask.flat[index % mask.size] = 1
        payload = operation_payload(
            "SharedSegment", empty, mask, operation_id=f"concurrent-{index}"
        )
        return clients[index % len(clients)].push_operation(room["id"], payload)[
            "sequence"
        ]

    with ThreadPoolExecutor(max_workers=8) as executor:
        sequences = list(executor.map(push, range(24)))
    assert sorted(sequences) == list(range(1, 25))
    assert [
        item["sequence"] for item in clients[0].operations(room["id"], 0)
    ] == list(range(1, 25))


def test_shared_folder_reads_independent_operation_files_in_parallel(tmp_path, monkeypatch):
    client = SharedFolderRoomClient(tmp_path, "alice")
    room = client.join("parallel operation reads", "9" * 64)
    empty = np.zeros((2, 2, 2), dtype=np.uint8)
    for index in range(8):
        changed = empty.copy()
        changed.flat[index] = 1
        client.push_operation(
            room["id"],
            operation_payload(
                f"Segment-{index}",
                empty,
                changed,
                operation_id=f"parallel-read-{index}",
            ),
        )

    client._artifact_queue.join()
    state_path = client._room_path / "sequence-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.pop("inline_operations", None)
    state_path.write_text(json.dumps(state), encoding="utf-8")

    original_read = collaboration_module._read_json_file
    state_lock = threading.Lock()
    active = 0
    peak_active = 0

    def delayed_operation_read(path):
        nonlocal active, peak_active
        if Path(path).parent.name != "operations":
            return original_read(path)
        with state_lock:
            active += 1
            peak_active = max(peak_active, active)
        try:
            time.sleep(0.03)
            return original_read(path)
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(collaboration_module, "_read_json_file", delayed_operation_read)
    operations = client.operations(room["id"], 0)

    assert [operation["sequence"] for operation in operations] == list(range(1, 9))
    assert peak_active >= 2


def test_recent_operation_feed_avoids_relisting_the_network_directory(tmp_path, monkeypatch):
    client = SharedFolderRoomClient(tmp_path, "alice")
    room = client.join("indexed operation feed", "1" * 64)
    empty = np.zeros((2, 2, 2), dtype=np.uint8)
    changed = empty.copy()
    changed[0, 0, 0] = 1
    client.push_operation(
        room["id"],
        operation_payload("Organ", empty, changed, operation_id="indexed-operation-1"),
    )

    original_glob = Path.glob

    def reject_live_directory_listing(path, pattern):
        if path.name == "operations":
            raise AssertionError("recent operation polling relisted the network directory")
        return original_glob(path, pattern)

    monkeypatch.setattr(Path, "glob", reject_live_directory_listing)
    assert [item["sequence"] for item in client.operations(room["id"], 0)] == [1]


def test_inline_operation_feed_avoids_live_operation_file_roundtrip(tmp_path, monkeypatch):
    client = SharedFolderRoomClient(tmp_path, "alice")
    room = client.join("inline operation feed", "7" * 64)
    empty = np.zeros((2, 2, 2), dtype=np.uint8)
    changed = empty.copy()
    changed[0, 0, 0] = 1
    client.push_operation(
        room["id"],
        operation_payload("Organ", empty, changed, operation_id="inline-operation-1"),
    )

    original_read = collaboration_module._read_json_file

    def reject_operation_file_read(path):
        if Path(path).parent.name == "operations":
            raise AssertionError("hot operation polling opened an archived operation file")
        return original_read(path)

    monkeypatch.setattr(collaboration_module, "_read_json_file", reject_operation_file_read)
    operations = client.operations(room["id"], 0)
    assert [item["sequence"] for item in operations] == [1]
    assert operations[0]["client_operation_id"] == "inline-operation-1"


def test_recent_chat_feed_avoids_relisting_the_network_directory(tmp_path, monkeypatch):
    client = SharedFolderRoomClient(tmp_path, "alice")
    room = client.join("indexed chat feed", "2" * 64)
    client.send_chat(room["id"], "hello", "indexed-chat-message")

    original_glob = Path.glob

    def reject_live_directory_listing(path, pattern):
        if path.name == "chat":
            raise AssertionError("recent chat polling relisted the network directory")
        return original_glob(path, pattern)

    monkeypatch.setattr(Path, "glob", reject_live_directory_listing)
    assert [item["text"] for item in client.chat_messages(room["id"], 0)] == ["hello"]


def test_sequence_lock_retries_transient_windows_permission_error(tmp_path, monkeypatch):
    client = SharedFolderRoomClient(tmp_path, "alice")
    room = client.join("permission race room", "e" * 64)
    real_mkdir = Path.mkdir
    transient_failures = {"remaining": 1}

    def mkdir_with_transient_permission_error(path, *args, **kwargs):
        if path.name == "sequence.lock" and transient_failures["remaining"]:
            transient_failures["remaining"] -= 1
            raise PermissionError(5, "Access is denied", str(path))
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir_with_transient_permission_error)
    empty = np.zeros((2, 2, 2), dtype=np.uint8)
    changed = empty.copy()
    changed.flat[0] = 1

    operation = client.push_operation(
        room["id"],
        operation_payload("SharedSegment", empty, changed, operation_id="permission-race"),
    )

    assert operation["sequence"] == 1
    assert transient_failures["remaining"] == 0


def test_shared_folder_presence_expires_and_partial_files_are_ignored(tmp_path):
    alice = SharedFolderRoomClient(tmp_path, "alice", presence_ttl_seconds=0.3)
    bob = SharedFolderRoomClient(tmp_path, "bob", presence_ttl_seconds=0.3)
    room = alice.join("presence room", "1" * 64)
    bob.join("presence room", "1" * 64)
    alice.presence(room["id"], {"active_segment_name": "Liver"})
    users = bob.presence(room["id"], {"active_segment_name": "Tumor"})
    assert {item["user"] for item in users} == {"alice", "bob"}

    operation_directory = alice._room_path / "operations"
    operation_directory.joinpath("00000000000000000001--broken.json.tmp").write_text(
        "{not-json", encoding="utf-8"
    )
    assert alice.operations(room["id"], 0) == []

    time.sleep(0.5)
    users = alice.presence(room["id"], {})
    assert [item["user"] for item in users] == ["alice"]


def test_presence_bypasses_slow_atomic_replace_on_network_share(tmp_path, monkeypatch):
    client = SharedFolderRoomClient(tmp_path, "network-user")
    room = client.join("restricted smb room", "2" * 64)
    client.presence(room["id"], {"active_segment_name": "First"})
    presence_path = client._presence_path(client._room_path)
    real_replace = collaboration_module.os.replace
    presence_replace_calls = []

    def reject_presence_replace(source, destination):
        if Path(destination) == presence_path:
            presence_replace_calls.append((source, destination))
            raise PermissionError(5, "Access is denied", str(destination))
        return real_replace(source, destination)

    monkeypatch.setattr(collaboration_module.os, "replace", reject_presence_replace)
    users = client.presence(room["id"], {"active_segment_name": "Updated"})

    assert users == [
        {
            "active_segment_name": "Updated",
            "last_seen": users[0]["last_seen"],
            "presence_session_id": client.presence_session_id,
            "user": "network-user",
        }
    ]
    assert presence_replace_calls == []
    assert not list(presence_path.parent.glob(".tmp-*"))


def test_delayed_leave_cannot_delete_newer_presence_session(tmp_path):
    old_client = SharedFolderRoomClient(tmp_path, "alice")
    room = old_client.join("presence reconnect", "3" * 64)
    old_client.presence(room["id"], {})

    new_client = SharedFolderRoomClient(tmp_path, "alice")
    new_client.join("presence reconnect", "3" * 64)
    new_client.presence(room["id"], {"active_segment_name": "New session"})
    old_client.leave(room["id"])

    presence_path = new_client._presence_path(new_client._room_path)
    assert presence_path.is_file()
    current = collaboration_module._read_json_file(presence_path)
    assert current["presence_session_id"] == new_client.presence_session_id
    assert current["active_segment_name"] == "New session"


def test_stale_operation_cache_cannot_allocate_duplicate_sequence(tmp_path):
    alice = SharedFolderRoomClient(tmp_path, "alice")
    bob = SharedFolderRoomClient(tmp_path, "bob")
    room = alice.join("stale operation cache", "4" * 64)
    bob.join("stale operation cache", "4" * 64)
    empty = np.zeros((2, 2, 2), dtype=np.uint8)
    first = empty.copy()
    first[0, 0, 0] = 1
    second = first.copy()
    second[1, 1, 1] = 1
    assert alice.push_operation(
        room["id"],
        operation_payload("Organ", empty, first, operation_id="stale-alice-operation"),
    )["sequence"] == 1

    collaboration_module._write_shared_hot_cache(
        bob._room_path / "sequence-state.json",
        {"latest_sequence": 0, "recent_operations": [], "inline_operations": []},
    )
    assert bob.push_operation(
        room["id"],
        operation_payload("Organ", first, second, operation_id="stale-bob-operation"),
    )["sequence"] == 2
    operation_sequences = sorted(
        bob._operation_sequence(path)
        for path in bob._room_path.joinpath("operations").glob("*.json")
    )
    assert operation_sequences == [1, 2]


def test_existing_duplicate_sequence_fails_safely_with_new_room_guidance(tmp_path):
    alice = SharedFolderRoomClient(tmp_path, "alice")
    room = alice.join("damaged old room", "5" * 64)
    empty = np.zeros((2, 2, 2), dtype=np.uint8)
    changed = empty.copy()
    changed[0, 0, 0] = 1
    alice.push_operation(
        room["id"],
        operation_payload("Organ", empty, changed, operation_id="first-operation"),
    )
    original = next(alice._room_path.joinpath("operations").glob("*.json"))
    duplicate = collaboration_module._read_json_file(original)
    duplicate["client_operation_id"] = "colliding-operation"
    duplicate_path = original.with_name(
        "00000000000000000001--ffffffffffffffffffff.json"
    )
    collaboration_module._write_json_atomic(duplicate_path, duplicate)

    bob = SharedFolderRoomClient(tmp_path, "bob")
    report = bob.preflight("damaged old room", "5" * 64)
    room_check = next(item for item in report["checks"] if item["id"] == "room-format")
    assert room_check["status"] == "fail"
    assert "Duplicate operation sequence(s): 1" in room_check["detail"]
    with pytest.raises(LiveCollaborationError, match="Create a new room"):
        bob.join("damaged old room", "5" * 64)


def test_shared_folder_chat_is_permanent_and_idempotent(tmp_path):
    alice = SharedFolderRoomClient(tmp_path, "alice")
    bob = SharedFolderRoomClient(tmp_path, "bob")
    room = alice.join("persistent chat", "3" * 64)
    bob.join("persistent chat", "3" * 64)

    first = alice.send_chat(room["id"], "Ready to segment?", "message-alice-1")
    duplicate = alice.send_chat(room["id"], "Ready to segment?", "message-alice-1")
    bob.send_chat(room["id"], "Yes", "message-bob-0001")

    assert first["sequence"] == 1
    assert duplicate["duplicate"] is True
    assert [item["text"] for item in bob.chat_messages(room["id"], 0)] == [
        "Ready to segment?",
        "Yes",
    ]

    rejoined = SharedFolderRoomClient(tmp_path, "alice")
    rejoined.join("persistent chat", "3" * 64)
    assert [item["author"] for item in rejoined.chat_messages(room["id"], 0)] == [
        "alice",
        "bob",
    ]


def test_shared_folder_label_owner_can_lock_and_peer_edits_are_rejected(tmp_path):
    alice = SharedFolderRoomClient(tmp_path, "alice")
    bob = SharedFolderRoomClient(tmp_path, "bob")
    room = alice.join("label locks", "4" * 64)
    bob.join("label locks", "4" * 64)
    empty = np.zeros((2, 2, 2), dtype=np.uint8)
    first = empty.copy()
    first[0, 0, 0] = 1
    alice.push_operation(
        room["id"],
        operation_payload("Tumor", empty, first, operation_id="owner-operation-1"),
    )

    locks = alice.segment_locks(room["id"])
    assert locks == [
        {"segment_id": "Tumor", "owner": "alice", "locked": False, "updated_at": None}
    ]
    with pytest.raises(LiveCollaborationError, match="Only alice"):
        bob.set_segment_lock(room["id"], "Tumor", True)

    locked = alice.set_segment_lock(room["id"], "Tumor", True)
    assert locked["locked"] is True
    second = first.copy()
    second[1, 1, 1] = 1
    with pytest.raises(LiveCollaborationError, match="locked by alice"):
        bob.push_operation(
            room["id"],
            operation_payload("Tumor", first, second, operation_id="peer-operation-1"),
        )

    alice.set_segment_lock(room["id"], "Tumor", False)
    assert bob.push_operation(
        room["id"],
        operation_payload("Tumor", first, second, operation_id="peer-operation-2"),
    )["sequence"] == 2


def test_realtime_files_do_not_wait_for_segmentation_sequence_lock(tmp_path):
    alice = SharedFolderRoomClient(tmp_path, "alice", lock_timeout_seconds=0.2)
    bob = SharedFolderRoomClient(tmp_path, "bob", lock_timeout_seconds=0.2)
    room = alice.join("independent realtime lanes", "a" * 64)
    bob.join("independent realtime lanes", "a" * 64)
    empty = np.zeros((2, 2, 2), dtype=np.uint8)
    first = empty.copy()
    first[0, 0, 0] = 1
    alice.push_operation(
        room["id"],
        operation_payload("Organ", empty, first, operation_id="owner-realtime-1"),
    )

    started = time.monotonic()
    with alice._sequence_lock(alice._room_path):
        bob.send_chat(room["id"], "Still live", "chat-with-busy-edit-lock")
        bob.presence(room["id"], {"active_segment_name": "Organ"})
        alice.set_segment_lock(room["id"], "Organ", True)
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert bob.chat_messages(room["id"], 0)[0]["text"] == "Still live"
    assert alice.segment_locks(room["id"])[0]["locked"] is True


def test_segment_owner_index_avoids_repeated_operation_history_scans(tmp_path, monkeypatch):
    alice = SharedFolderRoomClient(tmp_path, "alice")
    bob = SharedFolderRoomClient(tmp_path, "bob")
    room = alice.join("indexed label owners", "b" * 64)
    bob.join("indexed label owners", "b" * 64)
    empty = np.zeros((2, 2, 2), dtype=np.uint8)
    first = empty.copy()
    first[0, 0, 0] = 1
    alice.push_operation(
        room["id"],
        operation_payload("Vessel", empty, first, operation_id="owner-index-1"),
    )
    assert bob.segment_locks(room["id"])[0]["owner"] == "alice"

    original_read = collaboration_module._read_json_file

    def reject_operation_history_reads(path):
        if Path(path).parent.name == "operations":
            raise AssertionError("lock polling rescanned the complete operation history")
        return original_read(path)

    monkeypatch.setattr(
        collaboration_module, "_read_json_file", reject_operation_history_reads
    )
    assert bob.segment_locks(room["id"])[0]["owner"] == "alice"


def test_shared_folder_health_leave_and_backup_reservation(tmp_path):
    alice = SharedFolderRoomClient(tmp_path, "alice")
    bob = SharedFolderRoomClient(tmp_path, "bob")
    room = alice.join("health room", "5" * 64)
    bob.join("health room", "5" * 64)
    alice.presence(room["id"], {})
    bob.presence(room["id"], {})

    assert alice.health_check(room["id"])["status"] == "ok"
    reservation = alice.reserve_project_backup(room["id"], 300)
    assert reservation["path"].endswith(".mrb")
    assert bob.reserve_project_backup(room["id"], 300) is None
    assert bob.reserve_project_backup(room["id"], 300, force=True)["path"].endswith(".mrb")

    metadata_path = bob._room_path / "room.json"
    unavailable_path = bob._room_path / "room.unavailable"
    metadata_path.rename(unavailable_path)
    with pytest.raises(LiveCollaborationError, match="Could not read shared file"):
        bob.health_check(room["id"])
    unavailable_path.rename(metadata_path)

    alice.leave(room["id"])
    users = bob.presence(room["id"], {})
    assert [item["user"] for item in users] == ["bob"]


def test_server_chat_locks_and_immediate_leave(client, headers):
    room = client.post(
        "/api/live/rooms/join",
        headers=headers,
        json={"room_name": "server extras", "volume_signature": "6" * 64},
    ).json()
    bob_headers = {"X-LiveSeg-User": "bob"}
    client.post(
        "/api/live/rooms/join",
        headers=bob_headers,
        json={"room_name": "server extras", "volume_signature": "6" * 64},
    )
    empty = np.zeros((2, 2, 2), dtype=np.uint8)
    first = empty.copy()
    first[0, 0, 0] = 1
    created = client.post(
        f"/api/live/rooms/{room['id']}/operations",
        headers=headers,
        json=operation_payload("Liver", empty, first, operation_id="server-owner-op-1"),
    )
    assert created.status_code == 201

    message = client.post(
        f"/api/live/rooms/{room['id']}/messages",
        headers=headers,
        json={"client_message_id": "server-message-1", "text": "Permanent hello"},
    )
    assert message.status_code == 201
    duplicate = client.post(
        f"/api/live/rooms/{room['id']}/messages",
        headers=headers,
        json={"client_message_id": "server-message-1", "text": "Permanent hello"},
    )
    assert duplicate.json()["duplicate"] is True
    messages = client.get(
        f"/api/live/rooms/{room['id']}/messages?after=0", headers=bob_headers
    ).json()
    assert [(item["author"], item["text"]) for item in messages] == [
        ("alice", "Permanent hello")
    ]

    forbidden = client.put(
        f"/api/live/rooms/{room['id']}/locks/Liver",
        headers=bob_headers,
        json={"locked": True},
    )
    assert forbidden.status_code == 403
    locked = client.put(
        f"/api/live/rooms/{room['id']}/locks/Liver",
        headers=headers,
        json={"locked": True},
    )
    assert locked.status_code == 200
    assert locked.json()["locked"] is True

    peer_edit = first.copy()
    peer_edit[1, 1, 1] = 1
    rejected = client.post(
        f"/api/live/rooms/{room['id']}/operations",
        headers=bob_headers,
        json=operation_payload(
            "Liver", first, peer_edit, operation_id="server-peer-op-1"
        ),
    )
    assert rejected.status_code == 423

    left = client.delete(
        f"/api/live/rooms/{room['id']}/presence", headers=headers
    )
    assert left.status_code == 200
    presence = client.post(
        f"/api/live/rooms/{room['id']}/presence",
        headers=bob_headers,
        json={},
    ).json()
    assert [entry["user"] for entry in presence] == ["bob"]


def test_server_delayed_leave_cannot_remove_newer_presence_session(client, headers):
    room = client.post(
        "/api/live/rooms/join",
        headers=headers,
        json={"room_name": "server reconnect", "volume_signature": "9" * 64},
    ).json()
    presence_url = f"/api/live/rooms/{room['id']}/presence"
    client.post(
        presence_url,
        headers=headers,
        json={"presence_session_id": "old-session"},
    )
    client.post(
        presence_url,
        headers=headers,
        json={
            "presence_session_id": "new-session",
            "active_segment_name": "Current label",
        },
    )

    delayed_leave = client.delete(
        presence_url + "?presence_session_id=old-session", headers=headers
    )
    assert delayed_leave.status_code == 200
    users = client.post(
        presence_url,
        headers={"X-LiveSeg-User": "bob"},
        json={"presence_session_id": "bob-session"},
    ).json()
    alice = next(entry for entry in users if entry["user"] == "alice")
    assert alice["presence_session_id"] == "new-session"
    assert alice["active_segment_name"] == "Current label"


def test_invitation_and_material_template_are_portable_and_secret_free():
    template = validate_material_template(
        {
            "name": "Required labels",
            "segments": [
                {"id": "Liver", "name": "Liver", "color": "#C98468"},
                {
                    "id": "Tumor",
                    "name": "Tumor",
                    "color": "#E31A1C",
                    "terminology": "SlicerGeneralAnatomy~...",
                },
            ],
        }
    )
    invitation = build_invitation(
        "shared-folder", "Room 1", "a" * 64, r"\\server\share", template
    )
    assert parse_invitation(json.dumps(invitation)) == invitation
    assert "api" not in json.dumps(invitation).lower()
    assert [item["id"] for item in invitation["material_template"]["segments"]] == [
        "Liver",
        "Tumor",
    ]


def test_shared_snapshot_compacts_loose_files_but_preserves_history_and_old_clients(
    tmp_path,
):
    alice = SharedFolderRoomClient(tmp_path, "alice")
    room = alice.join("snapshot room", "7" * 64)
    empty = np.zeros((3, 3, 3), dtype=np.uint8)
    current = empty.copy()
    current[1, 1, 1] = 1
    alice.push_operation(
        room["id"],
        operation_payload("Liver", empty, current, operation_id="snapshot-source-1"),
    )
    full = operation_payload(
        "Liver", empty, current, replace=True, operation_id="ignored-by-snapshot"
    )
    manifest = alice.publish_room_snapshot(room["id"], [full], compact=True)

    assert manifest["compacted"] is True
    assert len(list(alice._room_path.joinpath("operation-archives").glob("*.zip"))) == 1
    active = alice.operations(room["id"], 0)
    assert len(active) == 1
    assert active[0]["operation_kind"] == "snapshot"
    assert active[0]["sequence"] > 1
    reconstructed = apply_mask_delta(np.zeros_like(current), active[0])
    assert np.array_equal(reconstructed, current)
    assert [item["sequence"] for item in alice.room_history(room["id"])] == [1, 2]
    historical = alice.state_at_sequence(room["id"], 1)
    assert np.array_equal(apply_mask_delta(empty, historical[0]), current)
    assert alice.segment_locks(room["id"])[0]["owner"] == "alice"


def test_shared_conflicts_roles_reviews_access_requests_and_owner_transfer(tmp_path):
    alice = SharedFolderRoomClient(tmp_path, "alice")
    bob = SharedFolderRoomClient(tmp_path, "bob")
    room = alice.join("governed room", "8" * 64)
    bob.join("governed room", "8" * 64)
    empty = np.zeros((2, 2, 2), dtype=np.uint8)
    base = empty.copy()
    base[0, 0, 0] = 1
    alice.push_operation(
        room["id"], operation_payload("Tumor", empty, base, operation_id="gov-base-1")
    )
    alice_next = base.copy()
    alice_next[1, 1, 1] = 1
    alice_operation = operation_payload(
        "Tumor", base, alice_next, operation_id="gov-alice-2"
    )
    alice_operation["base_sequence"] = 1
    alice.push_operation(room["id"], alice_operation)
    bob_next = base.copy()
    bob_next[1, 1, 1] = 1
    bob_operation = operation_payload("Tumor", base, bob_next, operation_id="gov-bob-2")
    bob_operation["base_sequence"] = 1
    pushed = bob.push_operation(room["id"], bob_operation)
    assert pushed["conflicts"][0]["overlap_voxels"] == 1
    conflict = bob.room_conflicts(room["id"], unresolved_only=True)[0]
    assert bob.resolve_conflict(room["id"], conflict["id"], "union")["resolution"] == "union"

    request = bob.request_segment_access(room["id"], "Tumor", "Please let me edit")
    assert request["status"] == "pending"
    assert alice.segment_access_requests(room["id"], "Tumor")[0]["requester"] == "bob"
    assert alice.set_room_role(room["id"], "bob", "reviewer")["role"] == "reviewer"
    approved = bob.set_review_state(room["id"], "Tumor", "approved", "Looks good")
    assert approved["state"] == "approved"
    assert alice.segment_locks(room["id"])[0]["locked"] is True
    transferred = alice.transfer_segment_owner(room["id"], "Tumor", "bob")
    assert transferred["owner"] == "bob"
    assert any(item["event"] == "label.owner-transfer" for item in alice.audit_events(room["id"]))


def test_shared_anchored_chat_diagnostics_and_backup_management(tmp_path):
    alice = SharedFolderRoomClient(tmp_path, "alice")
    room = alice.join("managed backups", "9" * 64)
    anchor = {"crosshair_ras": [1.0, 2.0, 3.0], "slice_offsets": {"Red": 4.0}}
    alice.send_chat(room["id"], "Review here", "anchored-message-1", anchor)
    assert alice.chat_messages(room["id"], 0)[0]["anchor"] == anchor
    diagnostics = alice.diagnostics(room["id"])
    assert diagnostics["status"] == "ok"
    assert diagnostics["free_bytes"] > 0

    backup = alice._room_path / "backups" / "test.mrb"
    backup.write_bytes(b"PK\x03\x04test-bundle")
    assert alice.set_backup_pinned(room["id"], backup.name, True)["pinned"] is True
    verification = alice.verify_project_backup(room["id"], backup.name)
    assert verification["valid"] is True
    assert verification["sha256"]
    assert alice.list_project_backups(room["id"])[0]["pinned"] is True


def test_server_advanced_collaboration_endpoints(client, headers):
    room = client.post(
        "/api/live/rooms/join",
        headers=headers,
        json={"room_name": "advanced server", "volume_signature": "a1" * 32},
    ).json()
    bob_headers = {"X-LiveSeg-User": "bob"}
    client.post(
        "/api/live/rooms/join",
        headers=bob_headers,
        json={"room_name": "advanced server", "volume_signature": "a1" * 32},
    )
    role = client.put(
        f"/api/live/rooms/{room['id']}/roles/bob",
        headers=headers,
        json={"role": "reviewer"},
    )
    assert role.status_code == 200
    empty = np.zeros((2, 2, 2), dtype=np.uint8)
    first = empty.copy()
    first[0, 0, 0] = 1
    created = client.post(
        f"/api/live/rooms/{room['id']}/operations",
        headers=headers,
        json=operation_payload("Liver", empty, first, operation_id="advanced-owner-1"),
    )
    assert created.status_code == 201
    review = client.put(
        f"/api/live/rooms/{room['id']}/reviews/Liver",
        headers=bob_headers,
        json={"state": "approved", "note": "Verified"},
    )
    assert review.status_code == 200
    assert client.get(
        f"/api/live/rooms/{room['id']}/locks", headers=headers
    ).json()[0]["locked"] is True
    anchored = client.post(
        f"/api/live/rooms/{room['id']}/messages",
        headers=bob_headers,
        json={
            "client_message_id": "advanced-anchor-1",
            "text": "See this point",
            "anchor": {"crosshair_ras": [1, 2, 3]},
        },
    )
    assert anchored.status_code == 201
    assert anchored.json()["anchor"]["crosshair_ras"] == [1, 2, 3]
    template = client.put(
        f"/api/live/rooms/{room['id']}/material-template",
        headers=headers,
        json={
            "name": "Labels",
            "segments": [{"id": "Liver", "name": "Liver", "color": "#C98468"}],
        },
    )
    assert template.status_code == 200
    assert client.get(
        f"/api/live/rooms/{room['id']}/material-template", headers=bob_headers
    ).json()["segments"][0]["id"] == "Liver"
    access = client.post(
        f"/api/live/rooms/{room['id']}/access-requests",
        headers=bob_headers,
        json={"segment_id": "Liver", "message": "Need changes"},
    )
    assert access.status_code == 201
    assert client.get(
        f"/api/live/rooms/{room['id']}/audit", headers=headers
    ).json()


def test_server_detects_overlapping_concurrent_edits(client, headers):
    room = client.post(
        "/api/live/rooms/join",
        headers=headers,
        json={"room_name": "server conflicts", "volume_signature": "b1" * 32},
    ).json()
    bob_headers = {"X-LiveSeg-User": "bob"}
    client.post(
        "/api/live/rooms/join",
        headers=bob_headers,
        json={"room_name": "server conflicts", "volume_signature": "b1" * 32},
    )
    empty = np.zeros((2, 2, 2), dtype=np.uint8)
    base = empty.copy()
    base[0, 0, 0] = 1
    client.post(
        f"/api/live/rooms/{room['id']}/operations",
        headers=headers,
        json=operation_payload("Tumor", empty, base, operation_id="server-conflict-base"),
    )
    alice_next = base.copy()
    alice_next[1, 1, 1] = 1
    alice_operation = operation_payload(
        "Tumor", base, alice_next, operation_id="server-conflict-alice"
    )
    alice_operation["base_sequence"] = 1
    client.post(
        f"/api/live/rooms/{room['id']}/operations", headers=headers, json=alice_operation
    )
    bob_operation = operation_payload(
        "Tumor", base, alice_next, operation_id="server-conflict-bob"
    )
    bob_operation["base_sequence"] = 1
    conflicting = client.post(
        f"/api/live/rooms/{room['id']}/operations",
        headers=bob_headers,
        json=bob_operation,
    )
    assert conflicting.status_code == 201
    assert conflicting.json()["conflicts"][0]["overlap_voxels"] == 1
    conflicts = client.get(
        f"/api/live/rooms/{room['id']}/conflicts", headers=headers
    ).json()
    assert conflicts[0]["resolution"] == "unresolved"


def test_server_per_user_tokens_bind_identity(tmp_path):
    settings = Settings(
        database_path=tmp_path / "tokens.sqlite3",
        user_tokens={"token-alice": "alice"},
    )
    with TestClient(create_app(settings)) as token_client:
        missing = token_client.get("/health")
        assert missing.status_code == 200
        mismatch = token_client.post(
            "/api/live/rooms/join",
            headers={
                "Authorization": "Bearer token-alice",
                "X-LiveSeg-User": "mallory",
            },
            json={"room_name": "token room", "volume_signature": "c1" * 32},
        )
        assert mismatch.status_code == 401
        joined = token_client.post(
            "/api/live/rooms/join",
            headers={
                "Authorization": "Bearer token-alice",
                "X-LiveSeg-User": "alice",
            },
            json={"room_name": "token room", "volume_signature": "c1" * 32},
        )
        assert joined.status_code == 200


def test_public_preflight_requires_forwarded_https_and_individual_token(tmp_path):
    settings = Settings(
        database_path=tmp_path / "public.sqlite3",
        user_tokens={"alice-secret": "Alice"},
        require_https=True,
    )
    payload = {
        "room_name": "public room",
        "volume_signature": "a" * 64,
        "plugin_version": "0.13.1",
        "protocol_version": 2,
    }
    with TestClient(create_app(settings)) as public_client:
        rejected = public_client.post(
            "/api/live/preflight",
            headers={
                "Authorization": "Bearer alice-secret",
                "X-LiveSeg-User": "Alice",
                "X-Forwarded-Proto": "http",
            },
            json=payload,
        )
        assert rejected.status_code == 426
        accepted = public_client.post(
            "/api/live/preflight",
            headers={
                "Authorization": "Bearer alice-secret",
                "X-LiveSeg-User": "Alice",
                "X-Forwarded-Proto": "https",
            },
            json=payload,
        )
        assert accepted.status_code == 200
        assert accepted.json()["authentication"] == "user-tokens"
        assert accepted.json()["https_required"] is True


def test_direct_lan_invitation_round_trip_contains_fallback_and_session_code():
    invitation = build_invitation(
        "direct-lan",
        "insect-head",
        "dataset-signature",
        "http://192.168.1.20:8765",
        fallback_shared_folder=r"\\research\live",
        access_code="temporary-code",
    )
    assert invitation["format"] == "live-segmentation-room-v2"
    assert invitation["fallback_shared_folder"] == r"\\research\live"
    assert invitation["access_code"] == "temporary-code"
    assert parse_invitation(json.dumps(invitation)) == invitation


def test_session_metrics_and_crash_journal_are_sanitized_and_context_bound(tmp_path):
    metrics = SessionMetrics()
    metrics.record("chat-send", 0.012, byte_count=120)
    metrics.operation_queued("operation-1", started=time.monotonic() - 0.01)
    metrics.operation_acknowledged("operation-1")
    summary = metrics.summary()
    assert summary["stages"]["chat-send"]["last_ms"] == 12.0
    assert summary["stages"]["edit_roundtrip"]["last_ms"] >= 1.0
    assert "user" not in json.dumps(summary).lower()

    journal = PendingOperationJournal(tmp_path / "pending.json")
    context = {"room_name": "room-a", "volume_signature": "signature-a"}
    operation = {"client_operation_id": "operation-1", "segment_id": "organ"}
    journal.write(context, [operation])
    assert journal.read(context) == [operation]
    assert journal.read({**context, "room_name": "room-b"}) == []
    journal.clear()
    assert not journal.path.exists()


def test_segmentation_quality_report_finds_empty_disconnected_and_overlap():
    first = np.zeros((8, 8, 8), dtype=np.uint8)
    first[1, 1, 1] = 1
    first[6, 6, 6] = 1
    second = np.zeros_like(first)
    second[1, 1, 1] = 1
    empty = np.zeros_like(first)
    report = segmentation_quality_report(
        {"first": first, "second": second, "empty": empty},
        min_component_voxels=2,
    )
    issue_types = {item["type"] for item in report["issues"]}
    assert {"empty", "disconnected", "small-components", "overlap"} <= issue_types


def test_direct_lan_relay_supports_two_clients_and_rejects_wrong_code(tmp_path):
    relay = LanRelayServer(
        tmp_path,
        access_code="correct-code",
        host="127.0.0.1",
        port=0,
    )
    relay.start()
    port = relay._httpd.server_address[1]
    url = f"http://127.0.0.1:{port}"
    try:
        alice = LanRoomClient(url, "alice", "correct-code")
        bob = LanRoomClient(url, "bob", "correct-code")
        first_check = alice.preflight("lan-room", "dataset-signature")
        assert first_check["status"] == "warning"
        second_check = bob.preflight("lan-room", "dataset-signature")
        peer_check = next(
            check
            for check in second_check["checks"]
            if check["id"] == "peer-computer"
        )
        assert peer_check["status"] == "pass"
        alice_room = alice.join("lan-room", "dataset-signature")
        bob_room = bob.join("lan-room", "dataset-signature")
        assert alice_room["id"] == bob_room["id"]

        empty = np.zeros((4, 4, 4), dtype=np.uint8)
        mask = empty.copy()
        mask[1:3, 1:3, 1:3] = 1
        alice.push_operation(
            alice_room["id"], operation_payload("organ", empty, mask)
        )
        received = bob.operations(bob_room["id"], 0)
        assert len(received) == 1
        assert received[0]["author"] == "alice"

        wrong = LanRoomClient(url, "mallory", "wrong-code")
        with pytest.raises(LiveCollaborationError, match="session code"):
            wrong.join("lan-room", "dataset-signature")
    finally:
        relay.stop()


def test_hybrid_client_falls_back_to_shared_store_when_lan_is_unavailable(tmp_path):
    fallback = SharedFolderRoomClient(tmp_path, "alice")
    unavailable = LanRoomClient(
        "http://127.0.0.1:9",
        "alice",
        "unused-code",
        timeout_seconds=0.05,
    )
    hybrid = HybridRoomClient(unavailable, fallback, retry_seconds=60)
    room = hybrid.join("fallback-room", "dataset-signature")
    assert hybrid.fallback_count == 1

    empty = np.zeros((3, 3, 3), dtype=np.uint8)
    mask = empty.copy()
    mask[1, 1, 1] = 1
    hybrid.push_operation(room["id"], operation_payload("organ", empty, mask))
    assert hybrid.fallback_count == 2
    assert len(fallback.operations(room["id"], 0)) == 1


def test_hybrid_client_mirrors_fast_lan_edits_then_fails_over(tmp_path):
    relay = LanRelayServer(
        tmp_path / "local-relay",
        access_code="hybrid-code",
        host="127.0.0.1",
        port=0,
    )
    relay.start()
    port = relay._httpd.server_address[1]
    lan = LanRoomClient(f"http://127.0.0.1:{port}", "alice", "hybrid-code")
    fallback = SharedFolderRoomClient(tmp_path / "fallback", "alice")
    hybrid = HybridRoomClient(lan, fallback, retry_seconds=60)
    try:
        room = hybrid.join("mirrored-room", "dataset-signature")
        empty = np.zeros((3, 3, 3), dtype=np.uint8)
        first = empty.copy()
        first[1, 1, 1] = 1
        hybrid.push_operation(
            room["id"], operation_payload("organ", empty, first, operation_id="mirror-1")
        )
        deadline = time.time() + 2.0
        mirrored = []
        while time.time() < deadline:
            mirrored = fallback.operations(room["id"], 0)
            if mirrored:
                break
            time.sleep(0.02)
        assert len(mirrored) == 1

        relay.stop()
        second = first.copy()
        second[2, 2, 2] = 1
        hybrid.push_operation(
            room["id"], operation_payload("organ", first, second, operation_id="mirror-2")
        )
        assert hybrid.fallback_count == 1
        assert len(fallback.operations(room["id"], 0)) == 2
    finally:
        relay.stop()


def test_server_preserves_collaborative_undo_metadata(client, headers):
    room = client.post(
        "/api/live/rooms/join",
        headers=headers,
        json={"room_name": "undo metadata", "volume_signature": "d1" * 32},
    ).json()
    empty = np.zeros((3, 3, 3), dtype=np.uint8)
    first = empty.copy()
    first[1, 1, 1] = 1
    created = client.post(
        f"/api/live/rooms/{room['id']}/operations",
        headers=headers,
        json=operation_payload("organ", empty, first, operation_id="undo-source-1"),
    ).json()
    reverted = operation_payload(
        "organ", first, empty, operation_id="undo-inverse-1"
    )
    reverted["undo_of_sequence"] = created["sequence"]
    response = client.post(
        f"/api/live/rooms/{room['id']}/operations",
        headers=headers,
        json=reverted,
    )
    assert response.status_code == 201
    assert response.json()["undo_of_sequence"] == created["sequence"]
    operations = client.get(
        f"/api/live/rooms/{room['id']}/operations?after=0",
        headers=headers,
    ).json()
    assert operations[-1]["undo_of_sequence"] == created["sequence"]
