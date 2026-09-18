"""An operation the room refuses must not be waited for forever (review finding L-07)."""

from __future__ import annotations

import numpy as np

from tests.controller_harness import (
    SHAPE,
    Peer,
    RoomLog,
    collaboration,
    install_fake_slicer,
)

LABEL = "LiveSeg-S1"


def seeded_room(monkeypatch):
    scene, status_messages = install_fake_slicer(monkeypatch)
    log = RoomLog()
    alice = Peer("alice", scene)
    bob = Peer("bob", scene)
    mask = np.zeros(SHAPE, np.uint8)
    mask[5, 5, 5] = 1
    seed = log.commit(
        {
            "client_operation_id": "seed",
            "segment_id": LABEL,
            "segment_name": "S1",
            "color_hex": "#FF0000",
            "metadata_update": True,
            **collaboration.encode_mask_delta(np.zeros_like(mask), mask, replace=True),
        },
        "bob",
    )
    alice.apply(seed)
    bob.apply(seed)
    return log, alice, bob, status_messages


def revert_locked_edit(peer):
    """Stand-in for the Slicer write that restores a locked label from its baseline."""

    def restore(node, segment_id, baseline):
        segment = node.GetSegmentation().GetSegment(segment_id)
        segment.mask[...] = 0 if baseline is None else baseline.to_dense()

    peer.controller._restore_locked_segment = restore


def test_stroke_rejected_by_a_lock_does_not_block_the_same_voxels_later(monkeypatch):
    log, alice, bob, _ = seeded_room(monkeypatch)
    revert_locked_edit(alice)
    voxel = (2, 2, 2)

    # Bob locks the label; Alice has not received that yet and paints.
    alice.paint(LABEL, voxel)
    alice.prepare()
    (stroke,) = alice.controller.outgoing
    alice.finish_push(rejected=[stroke], errors=["Label is locked by bob"])

    assert alice.controller.outgoing == []
    assert alice.controller.awaiting_echo == []
    # The lock state arrives and the local stroke is reverted to the room state.
    alice.controller.segment_locks_state[LABEL] = {"locked": True, "owner": "bob"}
    alice.prepare()
    assert alice.mask(LABEL)[voxel] == 0

    # Bob unlocks; Alice paints the same voxel again: it must be published now.
    alice.controller.segment_locks_state[LABEL] = {"locked": False, "owner": "bob"}
    alice.paint(LABEL, voxel)
    alice.prepare()
    (repainted,) = alice.controller.outgoing
    alice.finish_push(accepted=[repainted])
    echoed = log.commit(repainted, "alice")
    alice.apply(echoed)
    bob.apply(echoed)

    assert bob.mask(LABEL)[voxel] == 1
    assert alice.controller.awaiting_echo == []


def test_accepted_stroke_still_waits_for_its_echo(monkeypatch):
    _, alice, _, _ = seeded_room(monkeypatch)
    alice.paint(LABEL, (2, 2, 2))
    alice.prepare()
    (stroke,) = alice.controller.outgoing
    alice.finish_push(accepted=[stroke])
    assert alice.controller.outgoing == []
    assert [item["client_operation_id"] for item in alice.controller.awaiting_echo] == [
        stroke["client_operation_id"]
    ]


def test_deletion_rejected_by_a_lock_restores_the_label(monkeypatch):
    _, alice, bob, _ = seeded_room(monkeypatch)
    revert_locked_edit(alice)

    alice.delete_label(LABEL)
    alice.prepare()
    (deletion,) = alice.controller.outgoing
    assert deletion["segment_deleted"] is True
    assert alice.mask(LABEL) is None
    alice.finish_push(rejected=[deletion], errors=["Label is locked by bob"])

    # The label is back with the voxels everybody else still sees.
    assert alice.controller.awaiting_echo == []
    assert alice.mask(LABEL) is not None
    assert int(alice.mask(LABEL).sum()) == int(bob.mask(LABEL).sum()) == 1
    assert alice.mask(LABEL)[5, 5, 5] == 1
    assert not alice.controller._segment_has_pending_deletion(alice.node, LABEL)
    assert any("deletion was refused" in entry for entry in alice.activity)
    assert alice.controller._deleted_baselines() == {}


def test_confirmed_deletion_forgets_the_kept_baseline(monkeypatch):
    log, alice, bob, _ = seeded_room(monkeypatch)
    alice.delete_label(LABEL)
    alice.prepare()
    (deletion,) = alice.controller.outgoing
    assert LABEL in alice.controller._deleted_baselines()
    alice.finish_push(accepted=[deletion])
    echoed = log.commit(deletion, "alice")
    alice.apply(echoed)
    bob.apply(echoed)
    assert alice.controller._deleted_baselines() == {}
    assert alice.mask(LABEL) is None
    assert bob.mask(LABEL) is None
