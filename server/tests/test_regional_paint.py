"""Native rasterization plus bounded claims must keep partition/lock semantics."""
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from tests import controller_harness as h
from tests.test_protected_claims import operation


@pytest.fixture
def room(monkeypatch):
    scene, _ = h.install_fake_slicer(monkeypatch)
    peers = [h.Peer(name, scene) for name in ("alice", "bob")]
    log = h.RoomLog()
    for item in (operation("A", "a", (1, 1, 1)), operation("B", "b", (5, 5, 5))):
        stored = log.commit(item, "alice")
        for peer in peers:
            peer.apply(stored)
    peers[0].controller.owner.regional_paint_target = lambda node: "A"
    return peers, log


def paint(peer):
    peer.paint("A", (5, 5, 5))
    peer.paint("A", (4, 4, 4))
    peer.controller._process_segmentation_events(peer.node, {"A"})


def test_native_paint_clears_touched_sibling_immediately_and_peers_converge(room):
    (alice, bob), log = room
    before = alice.controller.baselines[(alice.node.GetID(), "B")].to_dense()
    paint(alice)
    assert alice.mask("B")[5, 5, 5] == 0
    assert alice.mask("A")[1, 1, 1] == 1
    assert np.array_equal(alice.controller.baselines[(alice.node.GetID(), "B")].to_dense(), before)
    alice.prepare()
    outgoing = list(alice.controller.outgoing)
    alice.finish_push(accepted=outgoing)
    for item in outgoing:
        stored = log.commit(item, "alice")
        for peer in (alice, bob):
            peer.apply(stored)
    assert np.array_equal(alice.mask("A"), bob.mask("A"))
    assert np.array_equal(alice.mask("B"), bob.mask("B"))
    assert not np.any(alice.mask("A") & alice.mask("B"))


@pytest.mark.parametrize("mode,holder,blocked", [("all", "alice", True),
                                                ("others", "bob", True),
                                                ("others", "alice", False)])
def test_native_paint_respects_both_lock_modes_and_keeps_rest_of_stroke(room, mode, holder, blocked):
    (alice, _), _ = room
    alice.controller.segment_locks_state["B"] = {
        "locked": True, "lock_mode": mode, "lock_holder": holder, "owner": "alice"}
    paint(alice)
    assert bool(alice.mask("B")[5, 5, 5]) == blocked
    assert bool(alice.mask("A")[5, 5, 5]) != blocked
    assert alice.mask("A")[4, 4, 4] == 1


def test_failed_sibling_write_rolls_back_claim_without_changing_baselines(room):
    (alice, _), _ = room
    updater = alice.controller.owner.update_segment_binary_labelmap_crop
    attempts = []

    def fail_once(*args):
        attempts.append(args[4])
        return False if len(attempts) == 1 else updater(*args)

    alice.controller.owner.update_segment_binary_labelmap_crop = fail_once
    paint(alice)
    assert alice.mask("B")[5, 5, 5] == 1
    assert alice.mask("A")[5, 5, 5] == 0
    assert alice.mask("A")[4, 4, 4] == 0
    assert alice.errors


def test_failed_sibling_read_rejects_only_new_claims(room):
    (alice, _), _ = room
    alice.controller._read_mask_region = Mock(side_effect=RuntimeError("bad geometry"))
    paint(alice)
    assert alice.mask("B")[5, 5, 5] == 1
    assert alice.mask("A")[5, 5, 5] == 0
    assert alice.mask("A")[1, 1, 1] == 1
    assert alice.errors


def test_native_guard_is_scoped_and_restores_modes_and_observer(room):
    from regional_paint import NativePaintGuard

    (peer, _), _ = room
    node = peer.node
    node.EditAllowedEverywhere = 0
    node.GetSegmentation().GetNumberOfSegments = lambda: 200
    state = {"mode": 0, "effect": "Paint", "mask": 0, "intensity": False}
    parameter = Mock(OverwriteNone=2, OverwriteAllSegments=0)
    parameter.GetOverwriteMode.side_effect = lambda: state["mode"]
    parameter.SetOverwriteMode.side_effect = lambda mode: state.update(mode=mode)
    parameter.GetActiveEffectName.side_effect = lambda: state["effect"]
    parameter.GetMaskMode.side_effect = lambda: state["mask"]
    parameter.GetSourceVolumeIntensityMask.side_effect = lambda: state["intensity"]
    image = Mock()
    image.GetNumberOfPoints.return_value = 4_113_032_904
    volume = Mock()
    volume.GetImageData.return_value = image
    editor = Mock()
    editor.mrmlSegmentEditorNode.return_value = parameter
    editor.segmentationNode.return_value = node
    editor.sourceVolumeNode.return_value = volume
    editor.currentSegmentID.return_value = "A"
    owner = SimpleNamespace(live_collaboration=peer.controller)
    guard = NativePaintGuard(owner, editor)
    assert guard.active and state["mode"] == 2 and guard.target(node) == "A"
    second_view = Mock()
    second_view.mrmlSegmentEditorNode.return_value = parameter
    second_view.segmentationNode.return_value = node
    assert guard.protects_editor(second_view, node, 0), "Shared MRML parameter was treated as an unrelated editor"
    second_view.mrmlSegmentEditorNode.return_value = Mock()
    assert not guard.protects_editor(second_view, node, 0)
    for key, value in (("effect", "Erase"), ("intensity", True)):
        previous = state[key]
        state[key] = value
        guard.refresh()
        assert not guard.active and state["mode"] == 0
        state[key] = previous
        guard.refresh()
        assert guard.active
    state.update(mask=3, mode=2)
    guard.refresh()
    assert not guard.active and state["mode"] == 2, "Overrode the IR lock guard"
    state.update(mask=0, mode=0)
    guard.refresh()
    owner._reference_ijk_to_segmentation_ras_matrix = lambda *args: None
    guard.refresh()
    assert not guard.active, "Unsupported geometry entered the bounded path"
    guard.close()
    parameter.RemoveObserver.assert_called_once_with(guard.observer)
    assert state["mode"] == 0
