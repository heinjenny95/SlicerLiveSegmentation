"""History must replay the same ownership/protection rules as live rooms."""

import numpy as np
import pytest

from tests.controller_harness import SHAPE, Peer, RoomLog, install_fake_slicer
from tests.controller_harness import collaboration as c
from tests.test_protected_claims import operation


@pytest.mark.parametrize("protected", [False, True])
def test_history_matches_live_after_cross_label_claims(monkeypatch, protected):
    scene, _ = install_fake_slicer(monkeypatch)
    peer, log = Peer("observer", scene), RoomLog()
    for item in (operation("A", "seed", (5, 5, 5)),
                 dict(operation("B", "paint", (5, 5, 5), (1, 1, 1)),
                      protected_segment_ids=["A"] if protected else [])):
        peer.apply(log.commit(item, "painter"))
    snapshots = c.reconstruct_snapshot_operations(log.operations, 2, c.apply_mask_delta, c.encode_mask_delta)
    masks = {}
    for item in snapshots:
        masks[item["segment_id"]] = c.apply_mask_delta(
            masks.get(item["segment_id"], np.zeros(SHAPE, np.uint8)), item)
    for label, mask in masks.items():
        np.testing.assert_array_equal(mask, peer.mask(label), err_msg=label)


def test_large_sparse_history_never_allocates_a_dense_volume(monkeypatch):
    shape = (2000, 2000, 2000)
    first = {"sequence": 1, "segment_id": "A", "segment_name": "Organ",
             **c.encode_mask_crop_snapshot(np.ones((1, 1, 1), np.uint8), [0, 1, 0, 1, 0, 1], shape)}
    second = {"sequence": 2, "segment_id": "A", "segment_name": "Organ",
              **c.encode_mask_crop_snapshot(np.ones((1, 1, 1), np.uint8),
                                            [1999, 2000, 1999, 2000, 1999, 2000], shape),
              "operation_kind": "patch"}
    original_zeros = np.zeros

    def bounded_zeros(shape, *args, **kwargs):
        assert int(np.prod(shape)) <= 64 ** 3, "History allocated a dense source volume"
        return original_zeros(shape, *args, **kwargs)

    monkeypatch.setattr(np, "zeros", bounded_zeros)
    snapshots = c.reconstruct_snapshot_operations([first, second], 2, c.apply_mask_delta, c.encode_mask_delta)
    assert snapshots[0]["operation_kind"] == "snapshot"
    assert all(item["operation_kind"] == "patch" for item in snapshots[1:])
    assert sum(int(c.decode_mask_delta(item)[1].sum()) for item in snapshots) == 2


def test_queueing_a_tiled_restore_keeps_all_patches(monkeypatch):
    scene, _ = install_fake_slicer(monkeypatch)
    peer, other, log = Peer("alice", scene), Peer("bob", scene), RoomLog()
    seed = log.commit(operation("A", "seed", (5, 5, 5)), "alice")
    peer.apply(seed)
    other.apply(seed)
    restore = [operation("A", "clear"),
               dict(operation("A", "tile-one", (1, 1, 1)), operation_kind="patch"),
               dict(operation("A", "tile-two", (6, 6, 6)), operation_kind="patch")]
    peer.controller._queue_snapshot_operations(restore)
    peer.prepare()
    pending = list(peer.controller.outgoing)
    assert [item["operation_kind"] for item in pending] == ["snapshot", "patch", "patch"]
    peer.finish_push(accepted=pending)
    for item in pending:
        stored = log.commit(item, "alice")
        peer.apply(stored)
        other.apply(stored)
    assert peer.mask("A").sum() == 2
    np.testing.assert_array_equal(peer.mask("A"), other.mask("A"))


def test_history_metadata_after_deletion_recreation_and_stale_paint():
    seed = dict(operation("A", "first", (1, 1, 1)), sequence=1, segment_name="Old")
    deletion = dict(seed, sequence=2, segment_deleted=True)
    restored = dict(operation("A", "restored", (6, 6, 6)), sequence=3,
                    segment_name="Restored", color_hex="#00FF00")
    paint = dict(restored, sequence=4, segment_name="Stale", color_hex="#FF0000", operation_kind="patch")
    snapshots = c.reconstruct_snapshot_operations([seed, deletion, restored, paint], 4,
                                                  c.apply_mask_delta, c.encode_mask_delta)
    assert snapshots[0]["segment_name"] == "Restored"
    assert snapshots[0]["color_hex"] == "#00FF00"


def test_randomized_history_matches_a_late_live_peer_at_each_revision(monkeypatch):
    scene, _ = install_fake_slicer(monkeypatch)
    peer, log = Peer("observer", scene), RoomLog()
    rng = np.random.default_rng(1742)
    for index in range(35):
        label = "ABC"[index % 3]
        voxel = tuple(int(x) for x in rng.integers(0, 4, size=3))
        item = operation(label, str(index), voxel)
        if index % 5:
            item["operation_kind"] = "patch"
        if index % 7 == 0:
            item["protected_segment_ids"] = ["ABC"[(index + 1) % 3]]
        if index % 11 == 0:
            item["segment_deleted"] = True
        peer.apply(log.commit(item, "painter"))
        snapshots = c.reconstruct_snapshot_operations(log.operations, len(log.operations),
                                                      c.apply_mask_delta, c.encode_mask_delta)
        reconstructed = c.LiveCollaborationController._historical_label_baselines(snapshots)
        assert set(reconstructed) == set(peer.node.GetSegmentation().GetSegmentIDs())
        for segment_id, baseline in reconstructed.items():
            np.testing.assert_array_equal(baseline.to_dense(), peer.mask(segment_id))


def test_undo_deleted_label_queues_every_historical_chunk(monkeypatch):
    scene, _ = install_fake_slicer(monkeypatch)
    peer = Peer("alice", scene)
    snapshots = [operation("A", "clear"),
                 dict(operation("A", "one", (1, 1, 1)), operation_kind="patch"),
                 dict(operation("A", "two", (6, 6, 6)), operation_kind="patch")]
    peer.controller._queue_collaborative_undo({
        "target": {"segment_id": "A", "segment_deleted": True}, "before": snapshots,
    }, 42)
    assert len(peer.controller.outgoing) == 3
    assert all(item["undo_of_sequence"] == 42 for item in peer.controller.outgoing)


def test_folder_history_before_and_after_compaction_keeps_protected_ownership(tmp_path):
    alice, bob = [c.SharedFolderRoomClient(tmp_path, name) for name in ("alice", "bob")]
    room = alice.join("history", "v" * 64)["id"]
    bob.join("history", "v" * 64)
    alice.push_operation(room, operation("A", "seed", (5, 5, 5)))
    alice._artifact_queue.join()
    alice.set_segment_lock(room, "A", True)
    bob.push_operation(room, operation("B", "paint", (5, 5, 5), (1, 1, 1)))
    bob._artifact_queue.join()
    state = alice.state_at_sequence(room, 2)
    alice.publish_room_snapshot(room, state, expected_sequence=2)
    # Lock changes now must not alter the meaning of earlier revisions.
    alice.set_segment_lock(room, "A", False)
    for sequence in (2, alice._latest_sequence(alice._room_path)):
        masks = c.LiveCollaborationController._historical_label_baselines(alice.state_at_sequence(room, sequence))
        assert masks["A"].to_dense()[5, 5, 5] == 1
        assert masks["B"].to_dense()[5, 5, 5] == 0
        assert masks["B"].to_dense().sum() == 1
