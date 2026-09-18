"""Mutual exclusion of the shared-folder sequence lock (review finding L-01)."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

LIVE_CORE = Path(__file__).resolve().parents[2] / "LiveSegmentation" / "LiveSegmentationLib"
sys.path.insert(0, str(LIVE_CORE))

import collaboration as collaboration_module  # noqa: E402
from collaboration import (  # noqa: E402
    LiveCollaborationError,
    SharedFolderRoomClient,
    encode_mask_delta,
)

SIGNATURE = "s" * 64


def operation_payload(segment_id, previous, current, replace=False, operation_id="op"):
    return {
        "client_operation_id": operation_id,
        "segment_id": segment_id,
        "segment_name": segment_id,
        "color_hex": "#37E8B8",
        **encode_mask_delta(previous, current, replace=replace),
    }


def abandoned_lock(room_path, age_seconds=120.0):
    lock = room_path / "sequence.lock"
    lock.mkdir()
    (lock / "owner.json").write_text('{"token": "dead"}', encoding="utf-8")
    old = time.time() - age_seconds
    os.utime(lock / "owner.json", (old, old))
    os.utime(lock, (old, old))
    return lock


def test_two_clients_breaking_the_same_stale_lock_never_hold_it_together(
    tmp_path, monkeypatch
):
    alice = SharedFolderRoomClient(tmp_path, "alice")
    bob = SharedFolderRoomClient(tmp_path, "bob")
    alice.join("room", SIGNATURE)
    bob.join("room", SIGNATURE)
    room_path = alice._room_path
    abandoned_lock(room_path)

    alice_inside = threading.Event()
    bob_may_rename = threading.Event()
    inside = []
    overlaps = []
    guard = threading.Lock()
    real_replace = os.replace

    def delayed_replace(source, destination):
        # Bob judged the abandoned lock stale, then stalls before his rename
        # while Alice breaks it and acquires a fresh lock under the same name.
        if threading.current_thread().name == "bob" and str(source).endswith(
            "sequence.lock"
        ):
            bob_may_rename.wait(5)
        return real_replace(source, destination)

    monkeypatch.setattr(collaboration_module.os, "replace", delayed_replace)

    def hold(client, name, seconds, entered=None):
        with client._sequence_lock(room_path):
            with guard:
                inside.append(name)
                if len(inside) > 1:
                    overlaps.append(tuple(inside))
            if entered is not None:
                entered.set()
            time.sleep(seconds)
            with guard:
                inside.remove(name)

    bob_thread = threading.Thread(target=hold, args=(bob, "bob", 0.2), name="bob")
    bob_thread.start()
    time.sleep(0.2)
    alice_thread = threading.Thread(
        target=hold, args=(alice, "alice", 0.6, alice_inside), name="alice"
    )
    alice_thread.start()
    assert alice_inside.wait(5)
    bob_may_rename.set()
    alice_thread.join(10)
    bob_thread.join(10)

    assert overlaps == []
    assert not list(room_path.glob("sequence.lock*"))


def test_lock_holder_slower_than_the_stale_limit_keeps_its_lock(tmp_path, monkeypatch):
    options = {"stale_lock_seconds": 1.0, "lock_timeout_seconds": 10.0}
    alice = SharedFolderRoomClient(tmp_path, "alice", **options)
    bob = SharedFolderRoomClient(tmp_path, "bob", **options)
    room = alice.join("room", SIGNATURE)
    bob.join("room", SIGNATURE)
    empty = np.zeros((4, 4, 4), np.uint8)
    painted = empty.copy()
    painted[0, 0, 0] = 1
    alice.push_operation(
        room["id"], operation_payload("S1", empty, painted, operation_id="a-1")
    )
    alice._artifact_queue.join()

    real_write = collaboration_module._write_json_atomic

    def slow_snapshot_write(path, payload, durable=True):
        # A large snapshot operation on a slow NAS.
        if threading.current_thread().name == "snapshot" and "operations" in str(path):
            time.sleep(0.7)
        return real_write(path, payload, durable)

    monkeypatch.setattr(collaboration_module, "_write_json_atomic", slow_snapshot_write)
    snapshot_operations = [
        operation_payload(f"S{index}", empty, painted, replace=True)
        for index in range(1, 5)
    ]
    result = {}
    snapshot_thread = threading.Thread(
        target=lambda: result.update(
            manifest=alice.publish_room_snapshot(room["id"], snapshot_operations)
        ),
        name="snapshot",
    )
    snapshot_thread.start()
    time.sleep(1.6)  # The snapshot has now held the lock longer than the stale limit.
    other = empty.copy()
    other[3, 3, 3] = 1
    result["bob"] = bob.push_operation(
        room["id"], operation_payload("S1", empty, other, operation_id="b-1")
    )
    snapshot_thread.join(20)

    assert alice._duplicate_operation_sequences(alice._room_path) == []
    assert result["bob"]["sequence"] == result["manifest"]["last_sequence"] + 1
    SharedFolderRoomClient(tmp_path, "carol").join("room", SIGNATURE)
    sequences = [item["sequence"] for item in alice.operations(room["id"], 0, limit=100)]
    assert sequences[-1] == result["bob"]["sequence"]


@pytest.mark.parametrize("skew_seconds", [90.0, -90.0])
def test_local_clock_skew_does_not_make_a_held_lock_look_stale(
    tmp_path, monkeypatch, skew_seconds
):
    alice = SharedFolderRoomClient(tmp_path, "alice")
    bob = SharedFolderRoomClient(tmp_path, "bob", lock_timeout_seconds=0.5)
    alice.join("room", SIGNATURE)
    bob.join("room", SIGNATURE)
    real_time = time.time
    with alice._sequence_lock(alice._room_path):
        # Bob's PC clock differs from the file server that stamps the lock.
        monkeypatch.setattr(
            collaboration_module.time, "time", lambda: real_time() + skew_seconds
        )
        with pytest.raises(LiveCollaborationError, match="busy"):
            with bob._sequence_lock(bob._room_path):
                pytest.fail("Bob entered while Alice still held the lock")
    monkeypatch.setattr(collaboration_module.time, "time", real_time)


def test_abandoned_lock_is_still_broken_despite_clock_skew(tmp_path, monkeypatch):
    bob = SharedFolderRoomClient(tmp_path, "bob", lock_timeout_seconds=2.0)
    bob.join("room", SIGNATURE)
    abandoned_lock(bob._room_path)
    real_time = time.time
    monkeypatch.setattr(collaboration_module.time, "time", lambda: real_time() - 90.0)
    with bob._sequence_lock(bob._room_path) as held:
        held.verify()


def test_displaced_holder_refuses_to_write(tmp_path):
    alice = SharedFolderRoomClient(tmp_path, "alice")
    room = alice.join("room", SIGNATURE)
    empty = np.zeros((4, 4, 4), np.uint8)
    painted = empty.copy()
    painted[0, 0, 0] = 1
    with alice._sequence_lock(alice._room_path) as held:
        held.verify()
        # Another computer took the lock over (for example after a suspend).
        (alice._room_path / "sequence.lock" / "owner.json").write_text(
            '{"token": "someone-else"}', encoding="utf-8"
        )
        with pytest.raises(LiveCollaborationError, match="taken over"):
            held.verify()
    # The foreign lock is left alone on release.
    assert (alice._room_path / "sequence.lock" / "owner.json").is_file()
    (alice._room_path / "sequence.lock" / "owner.json").unlink()
    (alice._room_path / "sequence.lock").rmdir()
    assert alice.push_operation(
        room["id"], operation_payload("S1", empty, painted, operation_id="a-1")
    )["sequence"] == 1
