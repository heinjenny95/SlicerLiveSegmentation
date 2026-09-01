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
    LiveCollaborationError,
    SharedFolderRoomClient,
    _atomic_temporary_path,
    apply_mask_delta,
    encode_mask_crop_delta,
    encode_mask_crop_snapshot,
    encode_mask_delta,
    volume_signature,
)
from fastapi.testclient import TestClient  # noqa: E402
from features import (  # noqa: E402
    build_invitation,
    parse_invitation,
    validate_material_template,
)


def operation_payload(segment_id, previous, current, replace=False, operation_id="client-op-1"):
    return {
        "client_operation_id": operation_id,
        "segment_id": segment_id,
        "segment_name": segment_id,
        "color_hex": "#37E8B8",
        **encode_mask_delta(previous, current, replace=replace),
    }


def test_atomic_temporary_name_stays_short_for_long_operation_names(tmp_path):
    destination = tmp_path / ("0" * 20 + "--" + "a" * 32 + ".json")
    temporary = _atomic_temporary_path(destination)
    assert temporary.parent == destination.parent
    assert temporary.name.startswith(".tmp-")
    assert len(temporary.name) <= 26
    assert destination.name not in temporary.name


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


def test_shared_folder_room_rejects_a_different_source_volume(tmp_path):
    alice = SharedFolderRoomClient(tmp_path, "alice")
    bob = SharedFolderRoomClient(tmp_path, "bob")
    alice.join("specimen mismatch", "d" * 64)
    with pytest.raises(LiveCollaborationError, match="different source volume"):
        bob.join("SPECIMEN MISMATCH", "e" * 64)


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


def test_presence_falls_back_when_share_rejects_atomic_replace(tmp_path, monkeypatch):
    client = SharedFolderRoomClient(tmp_path, "network-user")
    room = client.join("restricted smb room", "2" * 64)
    client.presence(room["id"], {"active_segment_name": "First"})
    presence_path = client._presence_path(client._room_path)
    real_replace = collaboration_module.os.replace

    def reject_presence_replace(source, destination):
        if Path(destination) == presence_path:
            raise PermissionError(5, "Access is denied", str(destination))
        return real_replace(source, destination)

    monkeypatch.setattr(collaboration_module.os, "replace", reject_presence_replace)
    users = client.presence(room["id"], {"active_segment_name": "Updated"})

    assert users == [
        {
            "active_segment_name": "Updated",
            "last_seen": users[0]["last_seen"],
            "user": "network-user",
        }
    ]
    assert not list(presence_path.parent.glob(".tmp-*"))


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
