"""Room actions not yet visible locally must not be undone by the diff encoder."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import numpy as np
import pytest

from tests.controller_harness import SHAPE, Peer, RoomLog, collaboration, install_fake_slicer

LABEL = "LiveSeg-S1"


def room_with_stroke(monkeypatch):
    scene, _ = install_fake_slicer(monkeypatch)
    alice, bob = Peer("alice", scene), Peer("bob", scene)
    log = RoomLog()
    before = np.zeros(SHAPE, np.uint8)
    before[5, 5, 5] = 1
    seed = log.commit({
        "client_operation_id": "seed", "segment_id": LABEL,
        "segment_name": "S1", "color_hex": "#FF0000",
        **collaboration.encode_mask_delta(np.zeros_like(before), before, replace=True),
    }, "bob")
    alice.apply(seed)
    bob.apply(seed)
    alice.paint(LABEL, (2, 2, slice(2, 5)))
    alice.prepare()
    (pending,) = alice.controller.outgoing
    alice.finish_push(accepted=[pending])
    stroke = log.commit(pending, "alice")
    alice.apply(stroke)
    bob.apply(stroke)
    return alice, bob, log, seed, stroke


@pytest.mark.parametrize("action", ["undo", "restore", "recovery"])
@pytest.mark.parametrize("awaiting_echo", [False, True])
def test_verification_does_not_cancel_a_deferred_room_action(monkeypatch, action, awaiting_echo):
    alice, bob, log, seed, stroke = room_with_stroke(monkeypatch)
    controller = alice.controller
    if action == "undo":
        controller._queue_collaborative_undo({"target": stroke, "before": [seed]}, stroke["sequence"])
    elif action == "restore":
        controller._queue_snapshot_operations([seed])
    else:
        recovered = dict(seed, client_operation_id="recovered")
        controller._journal_recovery = [recovered]
        controller.recover_edits_button = MagicMock()
        controller.discard_recovery_button = MagicMock()
        controller.on_timer = lambda: None
        controller.recover_pending_edits()
    (pending,) = controller.outgoing
    if awaiting_echo:
        alice.finish_push(accepted=[pending])
    # A real trailing verification runs before a slow room echoes the action.
    controller._queue_due_segment_verifications(time.monotonic() + 5)
    alice.prepare()
    all_pending = [*controller.awaiting_echo, *controller.outgoing]
    assert [item["client_operation_id"] for item in all_pending] == [pending["client_operation_id"]]
    if not awaiting_echo:
        alice.finish_push(accepted=[pending])
    echoed = log.commit(pending, "alice")
    alice.apply(echoed)
    bob.apply(echoed)
    assert alice.mask(LABEL)[2, 2, 2:5].sum() == 0
    np.testing.assert_array_equal(alice.mask(LABEL), bob.mask(LABEL))
    assert controller.awaiting_echo == []


def test_paint_while_waiting_for_undo_is_not_lost(monkeypatch):
    alice, bob, log, seed, stroke = room_with_stroke(monkeypatch)
    controller = alice.controller
    controller._queue_collaborative_undo({"target": stroke, "before": [seed]}, stroke["sequence"])
    alice.paint(LABEL, (6, 6, 6))
    alice.prepare()
    pending = list(controller.outgoing)
    assert len(pending) == 2
    alice.finish_push(accepted=pending)
    for operation in pending:
        echoed = log.commit(operation, "alice")
        alice.apply(echoed)
        bob.apply(echoed)
    assert alice.mask(LABEL)[6, 6, 6] == 1
    assert alice.mask(LABEL)[2, 2, 2:5].sum() == 0
    np.testing.assert_array_equal(alice.mask(LABEL), bob.mask(LABEL))
