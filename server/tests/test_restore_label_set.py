"""A room revision includes its label set, not just the surviving masks."""

import time

from tests.controller_harness import Peer, RoomLog, install_fake_slicer
from tests.controller_harness import collaboration as c
from tests.test_protected_claims import operation


def setup_room(monkeypatch):
    scene, _ = install_fake_slicer(monkeypatch)
    alice, bob, log = Peer("alice", scene), Peer("bob", scene), RoomLog()
    for item in (dict(operation("A", "old", (1, 1, 1)), segment_name="Original", color_hex="#00FF00"),
                 operation("B", "new", (6, 6, 6))):
        stored = log.commit(item, "alice")
        alice.apply(stored)
        bob.apply(stored)
    # Finish the normal trailing verification before requesting a restore.
    alice.controller._queue_due_segment_verifications(time.monotonic() + 10)
    alice.prepare()
    return alice, bob, log


def deliver_restore(peer, value, expected=2):
    peer.controller._handle_action_results([{
        "action": "restore_revision", "value": value,
        "request": {"sequence": 1, "expected_sequence": expected},
    }])


def test_restore_removes_later_labels_and_restores_metadata(monkeypatch):
    alice, bob, log = setup_room(monkeypatch)
    for peer in (alice, bob):
        segment = peer.node.GetSegmentation().GetSegment("A")
        segment.SetName("Renamed")
        segment.SetColor(1, 0, 0)
        peer.controller._remember_segment_metadata(peer.node, "A")
    historical = c.reconstruct_snapshot_operations(log.operations, 1, c.apply_mask_delta, c.encode_mask_delta)
    deliver_restore(alice, historical)
    pending = list(alice.controller.outgoing)
    assert any(item.get("segment_deleted") and item["segment_id"] == "B" for item in pending)
    alice.prepare()  # Trailing verification must not undo the queued restore.
    assert alice.controller.outgoing == pending
    alice.finish_push(accepted=pending)
    for item in pending:
        stored = log.commit(item, "alice")
        alice.apply(stored)
        bob.apply(stored)
    for peer in (alice, bob):
        assert peer.mask("B") is None
        assert peer.mask("A").sum() == 1
        segment = peer.node.GetSegmentation().GetSegment("A")
        assert segment.GetName() == "Original"
        assert segment.GetColor() == (0.0, 1.0, 0.0)


def test_restoring_an_empty_revision_deletes_every_current_label(monkeypatch):
    alice, _, _ = setup_room(monkeypatch)
    deliver_restore(alice, [])
    assert {item["segment_id"] for item in alice.controller.outgoing if item.get("segment_deleted")} == {"A", "B"}


def test_restore_refuses_to_overwrite_local_work_created_during_fetch(monkeypatch):
    alice, _, _ = setup_room(monkeypatch)
    alice.paint("A", (2, 2, 2))
    deliver_restore(alice, [])
    assert not alice.controller.outgoing
    assert alice.errors
    assert alice.mask("A")[2, 2, 2] == 1


def test_restore_refuses_a_stale_room_view(monkeypatch):
    alice, _, _ = setup_room(monkeypatch)
    deliver_restore(alice, [], expected=1)
    assert not alice.controller.outgoing
    assert alice.errors


def test_invalid_history_does_not_partially_queue_deletions(monkeypatch):
    alice, _, _ = setup_room(monkeypatch)
    damaged = dict(operation("A", "invalid", (1, 1, 1)), payload="not-a-payload")
    deliver_restore(alice, [damaged])
    assert not alice.controller.outgoing
    assert "Invalid historical state" in alice.errors[-1]


def test_restore_request_captures_the_applied_sequence(monkeypatch):
    alice, _, _ = setup_room(monkeypatch)
    controller = alice.controller
    controller._selected_history_sequence = lambda: 1
    controller.on_timer = lambda: None
    controller.restore_selected_revision()
    assert controller.pending_actions[-1]["expected_sequence"] == 2
    assert controller.pending_actions[-1]["sequence"] == 1


def test_restore_request_does_not_start_during_local_paint(monkeypatch):
    alice, _, _ = setup_room(monkeypatch)
    controller = alice.controller
    controller._selected_history_sequence = lambda: 1
    alice.paint("A", (3, 3, 3))
    controller.restore_selected_revision()
    assert not controller.pending_actions
    assert alice.errors
