"""Locked siblings must survive claims, even with stale UI lock state (L-17)."""

import numpy as np
import pytest

from tests.controller_harness import SHAPE, Peer, RoomLog, install_fake_slicer
from tests.controller_harness import collaboration as c


def operation(label, op_id, *voxels):
    empty = np.zeros(SHAPE, np.uint8)
    mask = empty.copy()
    for voxel in voxels:
        mask[voxel] = 1
    return {
        "client_operation_id": op_id + "-operation", "segment_id": label,
        "segment_name": label, "color_hex": "#FF0000",
        **c.encode_mask_delta(empty, mask, replace=True),
    }


@pytest.mark.parametrize("optimistic", [False, True])
@pytest.mark.parametrize("chunk_size", [2, 64])
def test_ordered_protection_clips_claims_not_the_whole_stroke(monkeypatch, optimistic, chunk_size):
    original_init = c.ChunkedMaskBaseline.__init__
    monkeypatch.setattr(c.ChunkedMaskBaseline, "__init__",
                        lambda self, shape, *args, **kwargs: original_init(self, shape, chunk_size))
    scene, messages = install_fake_slicer(monkeypatch)
    alice, bob = Peer("alice", scene), Peer("bob", scene)
    log = RoomLog()
    for seed in (operation("A", "seed-a", (5, 5, 5)), operation("B", "seed-b")):
        stored = log.commit(seed, "alice")
        alice.apply(stored)
        bob.apply(stored)
    # Intentionally disagreeing, stale UI caches must not decide the outcome.
    alice.controller.segment_locks_state = {"A": {"locked": False, "owner": "alice"}}
    bob.controller.segment_locks_state = {}
    if optimistic:
        bob.paint("B", (5, 5, 5))
        bob.paint("B", (1, 1, 1))
        # Native overwrite may already have cleared the sibling locally.
        bob.mask("A")[5, 5, 5] = 0
        bob.prepare()
        outgoing, = bob.controller.outgoing
        bob.finish_push(accepted=[outgoing])
    else:
        outgoing = operation("B", "stroke", (5, 5, 5), (1, 1, 1))
    stored = log.commit(dict(outgoing, protected_segment_ids=["A"]), "bob")
    for peer in (alice, bob):
        peer.apply(stored)
    late = Peer("late", scene)
    for stored in log.operations:
        late.apply(stored)
    for peer in (alice, bob, late):
        assert peer.mask("A")[5, 5, 5] == 1, peer.name
        assert peer.mask("B")[5, 5, 5] == 0, peer.name
        assert peer.mask("B")[1, 1, 1] == 1, peer.name
    bob.prepare()
    assert bob.controller.outgoing == bob.controller.awaiting_echo == []
    assert any("protected" in message.lower() for message in messages)


def test_folder_captures_authoritative_locks_and_ignores_forged_protection(tmp_path):
    alice, bob = [c.SharedFolderRoomClient(tmp_path, name) for name in ("alice", "bob")]
    room = alice.join("locked", "v" * 64)["id"]
    bob.join("locked", "v" * 64)
    alice.push_operation(room, operation("A", "seed", (5, 5, 5)))
    alice._artifact_queue.join()
    alice.set_segment_lock(room, "A", True)
    bob.push_operation(room, dict(operation("B", "paint", (5, 5, 5)),
                                 protected_segment_ids=[]))
    assert bob.operations(room, 0)[-1].get("protected_segment_ids") == ["A"]
    alice.set_segment_lock(room, "A", False)
    bob.push_operation(room, dict(operation("B", "later", (5, 5, 5)),
                                 protected_segment_ids=["B", "forged"]))
    assert bob.operations(room, 0)[-1].get("protected_segment_ids") == []
    # Review approval re-locks the label and must invalidate the hot lock index.
    alice.set_review_state(room, "A", "approved")
    bob.push_operation(room, operation("B", "review", (5, 5, 5)))
    assert bob.operations(room, 0)[-1].get("protected_segment_ids") == ["A"]
    alice.transfer_segment_owner(room, "A", "bob")
    bob.push_operation(room, operation("B", "owner", (5, 5, 5)))
    assert bob.operations(room, 0)[-1].get("protected_segment_ids") == []


def test_http_records_protection_inside_the_operation_transaction(client, headers):
    room = client.post("/api/live/rooms/join", headers=headers,
                       json={"room_name": "locked", "volume_signature": "v" * 64}).json()["id"]
    bob = {"X-LiveSeg-User": "bob"}
    client.post("/api/live/rooms/join", headers=bob,
                json={"room_name": "locked", "volume_signature": "v" * 64})
    base = f"/api/live/rooms/{room}"
    assert client.post(base + "/operations", headers=headers,
                       json=operation("A", "seed", (5, 5, 5))).status_code == 201
    assert client.put(base + "/locks/A", headers=headers, json={"locked": True}).status_code == 200
    assert client.post(base + "/operations", headers=bob,
                       json=operation("B", "paint", (5, 5, 5))).status_code == 201
    records = client.get(base + "/operations", headers=bob).json()
    assert records[-1].get("protected_segment_ids") == ["A"]
    assert client.put(base + "/locks/A", headers=headers, json={"locked": False}).status_code == 200
    # Previously sequenced protection is immutable even after unlocking.
    assert client.get(base + "/operations", headers=bob).json()[-1].get("protected_segment_ids") == ["A"]


def test_lock_index_is_warm_but_expiry_and_failed_mutations_are_fresh(tmp_path, monkeypatch):
    alice, bob = [c.SharedFolderRoomClient(tmp_path, name) for name in ("alice", "bob")]
    room = alice.join("expiry", "v" * 64)["id"]
    bob.join("expiry", "v" * 64)
    alice.push_operation(room, operation("A", "seed", (5, 5, 5)))
    alice._artifact_queue.join()
    lock = alice.set_segment_lock(room, "A", True, expires_minutes=1)
    assert bob._protected_segments(bob._room_path) == ["A"]
    original_read = c._read_json_file

    def without_per_label_reads(path):
        assert path.parent.name != "locks", "Warm strokes must not read every lock on the NAS"
        return original_read(path)

    monkeypatch.setattr(c, "_read_json_file", without_per_label_reads)
    assert bob._protected_segments(bob._room_path) == ["A"]
    monkeypatch.setattr(c.time, "time", lambda: lock["expires_epoch"] + 1)
    assert bob._protected_segments(bob._room_path) == []
    monkeypatch.setattr(c, "_read_json_file", original_read)
    # Approval must clear an old expiry, not silently approve an unlocked label.
    alice.set_review_state(room, "A", "approved")
    assert bob._protected_segments(bob._room_path) == ["A"]
    with pytest.raises(c.LiveCollaborationError, match="Only alice"):
        bob.set_segment_lock(room, "A", False)
    assert bob._protected_segments(bob._room_path) == ["A"]


def test_lock_mutations_use_the_same_mutex_as_publication(tmp_path, monkeypatch):
    from contextlib import contextmanager

    alice = c.SharedFolderRoomClient(tmp_path, "alice")
    room = alice.join("mutex", "v" * 64)["id"]
    alice.push_operation(room, operation("A", "seed", (5, 5, 5)))
    alice._artifact_queue.join()
    original_lock, original_write = alice._sequence_lock, c._write_json_atomic
    held = []

    @contextmanager
    def observe_lock(path):
        with original_lock(path) as lock:
            held.append(True)
            try:
                yield lock
            finally:
                held.pop()

    def observe_write(path, *args, **kwargs):
        if path.parent.name == "locks":
            assert held, "Changing a lock must serialize with operation acceptance"
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(alice, "_sequence_lock", observe_lock)
    monkeypatch.setattr(c, "_write_json_atomic", observe_write)
    alice.set_segment_lock(room, "A", True)
    alice.set_review_state(room, "A", "approved")
    alice.transfer_segment_owner(room, "A", "bob")


def test_protection_also_applies_while_the_target_is_locally_deleted(monkeypatch):
    scene, _ = install_fake_slicer(monkeypatch)
    peer, log = Peer("alice", scene), RoomLog()
    for item in (operation("A", "seed-a", (5, 5, 5)), operation("B", "seed-b")):
        peer.apply(log.commit(item, "bob"))
    peer.delete_label("B")
    peer.apply(log.commit(dict(operation("B", "claim", (5, 5, 5)),
                               protected_segment_ids=["A"]), "bob"))
    assert peer.mask("B") is None
    assert peer.mask("A")[5, 5, 5] == 1


def test_protected_claim_checks_only_relevant_locked_crops(monkeypatch):
    scene, _ = install_fake_slicer(monkeypatch)
    peer, log = Peer("alice", scene), RoomLog()
    for label, voxel in (("near", (0, 0, 0)), ("far", (5, 5, 5)), ("target", (1, 1, 1))):
        peer.apply(log.commit(operation(label, label, voxel), "bob"))
    controller = peer.controller
    bounds = [0, 2, 0, 2, 0, 2]
    key = (peer.node.GetID(), "far")
    controller.baselines[key] = c.ChunkedMaskBaseline.from_crop(
        SHAPE, peer.mask("far"), [0, SHAPE[0], 0, SHAPE[1], 0, SHAPE[2]], chunk_size=2,
    )
    decoded_far = []
    original_region = controller.baselines[key].region
    monkeypatch.setattr(controller.baselines[key], "region", lambda box: decoded_far.append(box) or original_region(box))
    native_reads = []
    original_read = controller._read_mask_region
    monkeypatch.setattr(controller, "_read_mask_region",
                        lambda node, sid, box: native_reads.append(sid) or original_read(node, sid, box))
    record = {"author": "alice", "segment_id": "target", "sequence": 4, "voxel_bbox": bounds,
              "protected_segment_ids": ["near", "far"]}
    changed = np.ones((2, 2, 2), dtype=bool)
    values, blocked = controller._protect_incoming_claims(peer.node, record, changed, changed.astype(np.uint8))
    assert blocked.sum() == 1 and blocked[0, 0, 0]
    assert values.sum() == 7
    assert decoded_far == []
    assert native_reads == ["near"]
