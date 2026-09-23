"""Local removal must not change globally ordered voxel ownership (L-17)."""

import numpy as np
import pytest

from tests.controller_harness import SHAPE, Peer, RoomLog, install_fake_slicer
from tests.controller_harness import collaboration as c
from tests.test_rejected_operations import LABEL, revert_locked_edit, seeded_room


@pytest.mark.parametrize("queue_deletion", [False, True])
def test_hidden_label_still_takes_its_ordered_turn_before_deletion(monkeypatch, queue_deletion):
    scene, _ = install_fake_slicer(monkeypatch)
    alice, bob, dave = [Peer(name, scene) for name in ("alice", "bob", "dave")]
    peers = (alice, bob, dave)
    log = RoomLog()
    empty = np.zeros(SHAPE, np.uint8)
    for label, voxel in (("A", (5, 5, 5)), ("B", (6, 6, 6))):
        mask = empty.copy()
        mask[voxel] = 1
        seed = log.commit({
            "client_operation_id": f"seed-{label}", "segment_id": label,
            "segment_name": label, "color_hex": "#FF0000",
            **c.encode_mask_delta(empty, mask, replace=True),
        }, "alice")
        for peer in peers:
            peer.apply(seed)
    dave.delete_label("A")
    if queue_deletion:
        dave.prepare()
    voxel = (1, 1, 1)
    for peer, label in ((bob, "B"), (alice, "A")):
        peer.paint(label, voxel)
        peer.prepare()
    for author in (bob, alice):
        for operation in list(author.controller.outgoing):
            ordered = log.commit(operation, author.name)
            for peer in peers:
                peer.apply(ordered)
    assert dave.mask("A") is None
    dave.prepare()
    for operation in list(dave.controller.outgoing):
        ordered = log.commit(operation, "dave")
        for peer in peers:
            peer.apply(ordered)
    late = Peer("late", scene)
    for operation in log.operations:
        late.apply(operation)
    for peer in (*peers, late):
        assert peer.mask("A") is None
        assert peer.mask("B")[voxel] == 0, peer.name
        assert c._baseline_region(peer.controller.baselines[(peer.node.GetID(), "B")],
                                  [1, 2, 1, 2, 1, 2]).item() == 0, peer.name


def test_refused_deletion_restores_the_updated_not_the_old_baseline(monkeypatch):
    log, alice, bob, _ = seeded_room(monkeypatch)
    revert_locked_edit(alice)
    alice.delete_label(LABEL)
    alice.prepare()
    deletion, = alice.controller.outgoing
    mask = np.zeros(SHAPE, np.uint8)
    mask[5, 5, 5] = 1
    claim = log.commit({
        "client_operation_id": "claim", "segment_id": "Other", "segment_name": "Other",
        "color_hex": "#0000FF", **c.encode_mask_delta(np.zeros_like(mask), mask),
    }, "bob")
    alice.apply(claim)
    bob.apply(claim)
    assert alice.mask(LABEL) is None
    alice.finish_push(rejected=[deletion], errors=["Label is locked by bob"])
    np.testing.assert_array_equal(alice.mask(LABEL), bob.mask(LABEL))
    assert alice.mask(LABEL)[5, 5, 5] == 0
