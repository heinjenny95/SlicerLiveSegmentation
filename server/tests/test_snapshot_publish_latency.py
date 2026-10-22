"""Archive compression is maintenance, never part of the live sequence lock."""
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from tests.controller_harness import collaboration as c
from tests.test_room_snapshot_safety import SIGNATURE, operation_payload


@pytest.mark.parametrize("shape", [(1, 1, 1), (7, 9, 11), (33, 32, 31)])
def test_snapshot_count_uses_packed_bits_not_dense_decode(shape):
    empty = np.zeros(shape, np.uint8)
    painted = (np.random.default_rng(4).random(shape) > .75).astype(np.uint8)
    operation = c.encode_mask_delta(empty, painted, replace=True)
    assert c.operation_changed_voxel_count(operation) == int(np.count_nonzero(painted))


def test_failed_background_archive_keeps_complete_loose_history(tmp_path, monkeypatch):
    client = c.SharedFolderRoomClient(tmp_path, "alice", background_compaction=True)
    room = client.join("room", SIGNATURE)
    empty = np.zeros((4,)*3, np.uint8)
    painted = empty.copy()
    painted[1, 1, 1] = 1
    client.push_operation(room["id"], operation_payload("A", empty, painted))
    client._artifact_queue.join()
    def failed_archive(*args, **kwargs):
        raise OSError("Simulated archive storage failure")
    monkeypatch.setattr(c.zipfile.ZipFile, "write", failed_archive)
    client.publish_room_snapshot(room["id"], [operation_payload("A", empty, painted, replace=True)], expected_sequence=1)
    client._compaction_queue.join()
    assert len(list((client._room_path/"operations").glob("*.json"))) == 2
    assert [op["sequence"] for op in client._all_operation_records(client._room_path)] == [1, 2]
    assert not list((client._room_path/"operation-archives").glob("*.zip"))


def test_background_snapshot_returns_and_peer_can_paint_while_archive_is_slow(tmp_path, monkeypatch):
    alice = c.SharedFolderRoomClient(tmp_path, "alice")
    alice.background_compaction = True
    bob = c.SharedFolderRoomClient(tmp_path, "bob", lock_timeout_seconds=.3)
    room = alice.join("room", SIGNATURE)
    bob.join("room", SIGNATURE)
    empty = np.zeros((4,)*3, np.uint8)
    painted = empty.copy()
    painted[1, 1, 1] = 1
    alice.push_operation(room["id"], operation_payload("A", empty, painted))
    alice._artifact_queue.join()
    entered, release = threading.Event(), threading.Event()
    real_write = c.zipfile.ZipFile.write
    def slow_archive(bundle, *args, **kwargs):
        entered.set()
        assert release.wait(5)
        return real_write(bundle, *args, **kwargs)
    monkeypatch.setattr(c.zipfile.ZipFile, "write", slow_archive)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(alice.publish_room_snapshot, room["id"],
                             [operation_payload("A", empty, painted, replace=True)], expected_sequence=1)
        try:
            assert entered.wait(3)
            manifest = future.result(timeout=.5)
            result = bob.push_operation(room["id"], operation_payload("B", empty, painted, operation_id="peer"))
            assert result["sequence"] == manifest["last_sequence"]+1
        finally:
            release.set()
    alice._compaction_queue.join()
    assert [op["sequence"] for op in alice._all_operation_records(alice._room_path)] == [1, 2, 3]


def test_checkpoint_payload_staging_does_not_block_paint_or_another_join(tmp_path, monkeypatch):
    alice = c.SharedFolderRoomClient(tmp_path, "alice")
    bob = c.SharedFolderRoomClient(tmp_path, "bob", lock_timeout_seconds=.2)
    room = alice.join("room", SIGNATURE)
    bob.join("room", SIGNATURE)
    empty = np.zeros((4,)*3, np.uint8)
    painted = np.ones((4,)*3, np.uint8)
    entered, release = threading.Event(), threading.Event()
    real_write = c._write_json_atomic
    def slow_payload(path, value, **kwargs):
        if isinstance(value, dict) and value.get("system_snapshot"):
            entered.set()
            assert release.wait(5)
        return real_write(path, value, **kwargs)
    monkeypatch.setattr(c, "_write_json_atomic", slow_payload)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(alice.publish_room_snapshot, room["id"],
                             [operation_payload("A", empty, painted, replace=True)], expected_sequence=0)
        try:
            assert entered.wait(3)
            # Competing maintenance skips instead of holding a second lane.
            second = bob.publish_room_snapshot(room["id"], [operation_payload("A", empty, painted, replace=True)], expected_sequence=0)
            assert second["skipped"] == "busy"
            c.SharedFolderRoomClient(tmp_path, "late", lock_timeout_seconds=.2).join("room", SIGNATURE)
            result = bob.push_operation(room["id"], operation_payload("B", empty, painted, operation_id="peer"))
            assert result["sequence"] == 1
        finally:
            release.set()
        assert future.result(timeout=3)["skipped"] == "stale"
    assert not list((alice._room_path/"snapshots").glob("*.json"))
    assert [op["segment_id"] for op in alice.operations(room["id"], 0)] == ["B"]
