"""Fresh room readers bootstrap committed state without replaying loose history."""

import json
from pathlib import Path

import numpy as np

from tests.test_room_snapshot_safety import SHAPE, SIGNATURE, operation_payload
from tests.test_room_snapshot_safety import collaboration_module as c


def room_history(tmp_path, count=25):
    writer = c.SharedFolderRoomClient(tmp_path, "writer")
    room_id = writer.join("room", SIGNATURE)["id"]
    current = np.zeros(SHAPE, np.uint8)
    for index in range(count):
        previous = current.copy()
        current[1, 1, 1] ^= 1
        writer.push_operation(room_id, operation_payload("A", previous, current, operation_id=f"edit-{index}"))
    writer._artifact_queue.join()
    return writer, room_id, current


def checkpoint(writer, room_id, current, *, parts=3, compact=False):
    manifest = writer.publish_room_snapshot(
        room_id,
        [operation_payload("A", np.zeros(SHAPE, np.uint8), current, replace=True) for _ in range(parts)],
        compact=compact,
        expected_sequence=writer._latest_sequence(writer._room_path),
    )
    writer._artifact_queue.join()
    return manifest


def clear_hot_feed(writer):
    path = writer._room_path / "sequence-state.json"
    state = c._read_json_file(path)
    state.update(recent_operations=[], inline_operations=[])
    c._write_json_atomic(path, state)


def watch_history_scans(monkeypatch, operations_path):
    calls = []
    original = Path.glob

    def glob(path, pattern, *args, **kwargs):
        if path == operations_path and pattern == "*.json":
            calls.append(pattern)
        return original(path, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "glob", glob)
    return calls


def test_fresh_reader_prefers_committed_checkpoint_while_old_files_remain(tmp_path):
    writer, room_id, current = room_history(tmp_path)
    manifest = checkpoint(writer, room_id, current)
    reader = c.SharedFolderRoomClient(tmp_path, "reader")
    reader.join("room", SIGNATURE)
    received = reader.operations(room_id, 0, limit=1)
    assert [item["sequence"] for item in received] == [manifest["first_sequence"]]
    assert received[0]["system_snapshot"] and received[0]["snapshot_group_index"] == 0


def test_checkpoint_pages_use_exact_paths_without_listing_loose_history(tmp_path, monkeypatch):
    writer, room_id, current = room_history(tmp_path)
    manifest = checkpoint(writer, room_id, current, parts=5)
    clear_hot_feed(writer)
    reader = c.SharedFolderRoomClient(tmp_path, "reader")
    reader.join("room", SIGNATURE)
    scans = watch_history_scans(monkeypatch, writer._room_path / "operations")
    received, cursor = [], 0
    while cursor < manifest["last_sequence"]:
        page = reader.operations(room_id, cursor, limit=2)
        assert page
        received.extend(page)
        cursor = page[-1]["sequence"]
    assert [item["snapshot_group_index"] for item in received] == list(range(5))
    assert scans == []


def test_cold_history_catalog_is_scanned_once_for_many_small_pages(tmp_path, monkeypatch):
    writer, room_id, _ = room_history(tmp_path)
    clear_hot_feed(writer)
    reader = c.SharedFolderRoomClient(tmp_path, "reader")
    scans = watch_history_scans(monkeypatch, writer._room_path / "operations")
    reader.join("room", SIGNATURE)
    sequences, cursor = [], 0
    while cursor < 25:
        page = reader.operations(room_id, cursor, limit=3)
        assert page
        sequences.extend(item["sequence"] for item in page)
        cursor = page[-1]["sequence"]
    assert sequences == list(range(1, 26))
    assert len(scans) == 1


def test_cached_cold_catalog_extends_from_new_contiguous_hot_tail(tmp_path, monkeypatch):
    writer, room_id, current = room_history(tmp_path)
    clear_hot_feed(writer)
    reader = c.SharedFolderRoomClient(tmp_path, "reader")
    reader.join("room", SIGNATURE)
    assert [item["sequence"] for item in reader.operations(room_id, 0, limit=3)] == [1, 2, 3]
    previous = current.copy()
    current[2, 2, 2] = 1
    monkeypatch.setattr(c, "RECENT_FEED_LIMIT", 2)
    writer.push_operation(room_id, operation_payload("A", previous, current, operation_id="new-tail"))
    writer._artifact_queue.join()
    scans = watch_history_scans(monkeypatch, writer._room_path / "operations")
    assert [item["sequence"] for item in reader.operations(room_id, 3, limit=3)] == [4, 5, 6]
    assert scans == []


def test_compaction_during_cold_catchup_recovers_at_complete_checkpoint(tmp_path):
    writer, room_id, current = room_history(tmp_path)
    clear_hot_feed(writer)
    reader = c.SharedFolderRoomClient(tmp_path, "reader")
    reader.join("room", SIGNATURE)
    assert [item["sequence"] for item in reader.operations(room_id, 0, limit=3)] == [1, 2, 3]
    manifest = checkpoint(writer, room_id, current, compact=True)
    page = reader.operations(room_id, 3, limit=3)
    assert page[0]["sequence"] == manifest["first_sequence"]
    assert page[0]["snapshot_group_index"] == 0


def test_pinned_checkpoint_finishes_from_archive_if_newer_compaction_moves_it(tmp_path):
    writer, room_id, current = room_history(tmp_path)
    first = checkpoint(writer, room_id, current, parts=3)
    reader = c.SharedFolderRoomClient(tmp_path, "reader")
    reader.join("room", SIGNATURE)
    page = reader.operations(room_id, 0, limit=1)
    assert page[0]["sequence"] == first["first_sequence"]
    checkpoint(writer, room_id, current, parts=2, compact=True)
    remaining = reader.operations(room_id, page[-1]["sequence"], limit=2)
    assert [item["snapshot_group_id"] for item in remaining] == [first["id"]]*2
    assert [item["snapshot_group_index"] for item in remaining] == [1, 2]


def test_malformed_checkpoint_manifest_does_not_skip_ordinary_history(tmp_path):
    writer, room_id, current = room_history(tmp_path)
    manifest = checkpoint(writer, room_id, current)
    manifest_path = next((writer._room_path / "snapshots").glob("*.json"))
    c._write_json_atomic(manifest_path, {**manifest, "first_sequence": -1})
    reader = c.SharedFolderRoomClient(tmp_path, "reader")
    reader.join("room", SIGNATURE)
    page = reader.operations(room_id, 0, limit=1)
    assert page[0]["sequence"] == 1
    assert not page[0].get("system_snapshot")


def test_operation_pages_are_bounded_by_encoded_bytes_and_still_converge(tmp_path):
    writer = c.SharedFolderRoomClient(tmp_path, "writer")
    room_id = writer.join("room", SIGNATURE)["id"]
    empty = np.zeros(SHAPE, np.uint8)
    operation = operation_payload("A", empty, empty, replace=True)
    # Model valid large legacy payloads without allocating a full-volume bitset.
    # Additional metadata is transported unchanged; immutable files are authority.
    for sequence in range(1, 65):
        c._write_json_atomic(
            writer._room_path / "operations" / f"{sequence:020d}--{sequence:020x}.json",
            {**operation, "sequence": sequence, "legacy_payload": "x" * (1024 * 1024)},
            durable=False,
        )
    writer._write_sequence_head(writer._room_path, 64)
    reader = c.SharedFolderRoomClient(tmp_path, "reader")
    reader.join("room", SIGNATURE)
    cursor, received = 0, []
    while cursor < 64:
        page = reader.operations(room_id, cursor, limit=64)
        assert page
        assert len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= 8 * 1024 * 1024
        received.extend(item["sequence"] for item in page)
        cursor = page[-1]["sequence"]
    assert received == list(range(1, 65))
