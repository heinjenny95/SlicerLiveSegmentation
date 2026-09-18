"""A room snapshot must never erase a collaborator's work (review finding L-02)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

LIVE_CORE = Path(__file__).resolve().parents[2] / "LiveSegmentation" / "LiveSegmentationLib"
sys.path.insert(0, str(LIVE_CORE))

import collaboration as collaboration_module  # noqa: E402
from collaboration import (  # noqa: E402
    SharedFolderRoomClient,
    apply_mask_delta,
    encode_mask_delta,
    encode_metadata_update,
)

SIGNATURE = "s" * 64
SHAPE = (4, 4, 4)


def operation_payload(segment_id, previous, current, replace=False, operation_id="op"):
    return {
        "client_operation_id": operation_id,
        "segment_id": segment_id,
        "segment_name": segment_id,
        "color_hex": "#37E8B8",
        **encode_mask_delta(previous, current, replace=replace),
    }


def replay(client, room_id, cursor, state):
    operations = client.operations(room_id, cursor, limit=100)
    for operation in operations:
        if not operation.get("segment_deleted"):
            state = apply_mask_delta(state, operation)
    return operations, state


def test_snapshot_built_before_a_collaborators_edit_is_not_published(tmp_path):
    alice = SharedFolderRoomClient(tmp_path, "alice")
    bob = SharedFolderRoomClient(tmp_path, "bob")
    dave = SharedFolderRoomClient(tmp_path, "dave")
    room_id = alice.join("room", SIGNATURE)["id"]
    bob.join("room", SIGNATURE)
    dave.join("room", SIGNATURE)
    empty = np.zeros(SHAPE, np.uint8)
    first = empty.copy()
    first[0, 0, 0] = 1
    alice.push_operation(room_id, operation_payload("Liver", empty, first, operation_id="a-1"))

    # Alice is idle at sequence 1 and builds a snapshot of what she knows ...
    alice_snapshot = operation_payload("Liver", empty, first, replace=True)
    # ... while Bob paints before her snapshot reaches the shared folder.
    second = first.copy()
    second[3, 3, 3] = 1
    bob.push_operation(room_id, operation_payload("Liver", first, second, operation_id="b-1"))
    bob._artifact_queue.join()

    result = alice.publish_room_snapshot(
        room_id, [alice_snapshot], compact=True, expected_sequence=1
    )

    assert result == {"skipped": "stale", "expected_sequence": 1, "latest_sequence": 2}
    assert not list((alice._room_path / "snapshots").glob("*.json"))
    assert not list((alice._room_path / "operation-archives").glob("*.zip"))
    carol = SharedFolderRoomClient(tmp_path, "carol")
    carol.join("room", SIGNATURE)
    for client, cursor, start in (
        (alice, 1, first),
        (dave, 1, first),
        (bob, 2, second),
        (carol, 0, empty),
    ):
        _, state = replay(client, room_id, cursor, start.copy())
        assert state[3, 3, 3] == 1, client.user_name
    assert not list(alice._room_path.glob("sequence.lock*"))


def test_snapshot_at_the_current_sequence_is_published_and_compacts(tmp_path):
    alice = SharedFolderRoomClient(tmp_path, "alice")
    room_id = alice.join("room", SIGNATURE)["id"]
    empty = np.zeros(SHAPE, np.uint8)
    first = empty.copy()
    first[0, 0, 0] = 1
    alice.push_operation(room_id, operation_payload("Liver", empty, first, operation_id="a-1"))
    alice._artifact_queue.join()
    manifest = alice.publish_room_snapshot(
        room_id,
        [operation_payload("Liver", empty, first, replace=True)],
        compact=True,
        expected_sequence=1,
    )
    assert manifest["before_sequence"] == 1
    assert manifest["first_sequence"] == 2
    assert len(list((alice._room_path / "operation-archives").glob("*.zip"))) == 1


def test_participant_behind_the_compaction_point_still_learns_of_a_deletion(tmp_path):
    alice = SharedFolderRoomClient(tmp_path, "alice")
    bob = SharedFolderRoomClient(tmp_path, "bob")
    room_id = alice.join("room", SIGNATURE)["id"]
    bob.join("room", SIGNATURE)
    empty = np.zeros(SHAPE, np.uint8)
    first = empty.copy()
    first[0, 0, 0] = 1
    alice.push_operation(room_id, operation_payload("Keep", empty, first, operation_id="a-1"))
    alice.push_operation(room_id, operation_payload("Gone", empty, first, operation_id="a-2"))
    bob_cursor = 2  # Bob has both labels on screen.
    alice.push_operation(
        room_id,
        {
            **encode_metadata_update(SHAPE),
            "client_operation_id": "a-3",
            "segment_id": "Gone",
            "segment_name": "Gone",
            "segment_deleted": True,
        },
    )
    alice._artifact_queue.join()
    manifest = alice.publish_room_snapshot(
        room_id,
        [operation_payload("Keep", empty, first, replace=True)],
        compact=True,
        expected_sequence=3,
    )

    received = bob.operations(room_id, bob_cursor, limit=100)
    deletions = [item for item in received if item.get("segment_deleted")]
    assert [item["segment_id"] for item in deletions] == ["Gone"]
    assert deletions[0]["carried_tombstone"] is True
    assert deletions[0]["deleted_by"] == "alice"
    assert deletions[0]["sequence"] == manifest["first_sequence"]
    assert [item["segment_id"] for item in received if not item.get("segment_deleted")] == [
        "Keep"
    ]

    # The next compaction carries it again for participants that are further behind.
    alice._artifact_queue.join()
    alice.publish_room_snapshot(
        room_id,
        [operation_payload("Keep", empty, first, replace=True)],
        compact=True,
        expected_sequence=manifest["last_sequence"],
    )
    later = bob.operations(room_id, bob_cursor, limit=100)
    assert [item["segment_id"] for item in later if item.get("segment_deleted")] == ["Gone"]


def test_recreated_label_is_not_deleted_again_by_a_carried_tombstone(tmp_path):
    alice = SharedFolderRoomClient(tmp_path, "alice")
    room_id = alice.join("room", SIGNATURE)["id"]
    empty = np.zeros(SHAPE, np.uint8)
    first = empty.copy()
    first[0, 0, 0] = 1
    alice.push_operation(
        room_id,
        {
            **encode_metadata_update(SHAPE),
            "client_operation_id": "a-1",
            "segment_id": "Back",
            "segment_deleted": True,
        },
    )
    alice._artifact_queue.join()
    alice.publish_room_snapshot(
        room_id,
        [operation_payload("Back", empty, first, replace=True)],
        compact=True,
        expected_sequence=1,
    )
    received = alice.operations(room_id, 0, limit=100)
    assert not any(item.get("segment_deleted") for item in received)


def test_reader_does_not_resume_at_an_ordinary_label_replace(tmp_path, monkeypatch):
    alice = SharedFolderRoomClient(tmp_path, "alice")
    bob = SharedFolderRoomClient(tmp_path, "bob")
    room_id = alice.join("room", SIGNATURE)["id"]
    bob.join("room", SIGNATURE)
    empty = np.zeros(SHAPE, np.uint8)
    first = empty.copy()
    first[0, 0, 0] = 1
    alice.push_operation(room_id, operation_payload("A", empty, first, operation_id="a-1"))
    alice.push_operation(room_id, operation_payload("B", empty, first, operation_id="a-2"))
    # An ordinary full replace of one label also has operation_kind "snapshot".
    alice.push_operation(
        room_id, operation_payload("A", empty, first, replace=True, operation_id="a-3")
    )
    alice._artifact_queue.join()

    # Bob's SMB client does not show operation 2 yet (directory cache lag).
    real_read = collaboration_module._read_json_file
    hidden = next((alice._room_path / "operations").glob(f"{2:020d}--*.json"))
    state_path = alice._room_path / "sequence-state.json"

    def lagging_read(path, *args, **kwargs):
        record = real_read(path, *args, **kwargs)
        if Path(path) == state_path:
            record = {
                **record,
                "inline_operations": [
                    item for item in record.get("inline_operations", []) if item["sequence"] != 2
                ],
                "recent_operations": [
                    item for item in record.get("recent_operations", []) if item["sequence"] != 2
                ],
            }
        return record

    monkeypatch.setattr(collaboration_module, "_read_json_file", lagging_read)
    real_glob = Path.glob
    monkeypatch.setattr(
        Path,
        "glob",
        lambda self, pattern: (item for item in real_glob(self, pattern) if item != hidden),
    )
    sequences = [item["sequence"] for item in bob.operations(room_id, 1, limit=100)]
    # Waiting for operation 2 is correct; resuming at 3 would lose label B for good.
    assert sequences == []


def test_snapshot_helper_falls_back_for_a_host_without_the_new_argument():
    calls = []

    class OldHost:
        def publish_room_snapshot(self, room_id, operations, compact=True, label=""):
            calls.append((room_id, len(operations), compact, label))
            return {"last_sequence": 7}

    class CurrentHost:
        def publish_room_snapshot(
            self, room_id, operations, compact=True, label="", expected_sequence=None
        ):
            calls.append(("current", expected_sequence))
            return {"skipped": "stale"}

    publish = collaboration_module._publish_snapshot_at
    assert publish(OldHost(), "room", [{}], "label", 5) == {"last_sequence": 7}
    assert publish(CurrentHost(), "room", [{}], "label", 5) == {"skipped": "stale"}
    assert publish(CurrentHost(), "room", [{}], "label", None) == {"skipped": "stale"}
    assert calls == [("room", 1, True, "label"), ("current", 5), ("current", None)]