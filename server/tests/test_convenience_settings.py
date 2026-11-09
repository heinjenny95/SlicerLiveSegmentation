"""Convenience preferences take effect without restarting a live session."""
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from tests import controller_harness as harness
from tests.controller_harness import collaboration


def test_disabling_remote_highlights_discards_queued_visuals(monkeypatch):
    callbacks = []
    monkeypatch.setitem(sys.modules, "qt", SimpleNamespace(
        QTimer=SimpleNamespace(singleShot=lambda delay, callback: callbacks.append(callback)),
    ))
    owner = SimpleNamespace(clear_remote_highlights=Mock(), show_remote_change_highlight_crop=Mock())
    controller = collaboration.LiveCollaborationController(owner)
    controller.connected = True
    controller._queue_remote_highlight([1], [0, 1, 0, 1, 0, 1], "Peer")
    assert len(callbacks) == 1

    controller.set_remote_highlights_enabled(False)
    owner.clear_remote_highlights.assert_called_once_with()
    assert controller._pending_remote_highlight is None
    controller._queue_remote_highlight([1], [0, 1, 0, 1, 0, 1], "Peer")
    assert controller._pending_remote_highlight is None and len(callbacks) == 1
    controller.set_remote_highlights_enabled(True)
    callbacks.pop()()
    owner.show_remote_change_highlight_crop.assert_not_called()
    controller._queue_remote_highlight([1], [0, 1, 0, 1, 0, 1], "Peer")
    callbacks.pop()()
    owner.show_remote_change_highlight_crop.assert_called_once()
    assert controller.connected


def test_disabling_view_navigation_stops_follow_but_keeps_presence_and_chat():
    controller = collaboration.LiveCollaborationController(SimpleNamespace())
    controller.follow_checkbox = Mock()
    controller.jump_to_user_button = Mock()
    controller.jump_to_chat_button = Mock()
    controller.jump_to_comment_button = Mock()
    controller.presence_by_user = {"Peer": {"slice_offsets": {"Red": 10}}}
    controller.chat_anchors = {12: {"slice_offsets": {"Red": 20}}}
    controller._show_error = Mock()

    controller.set_view_navigation_enabled(False)
    controller.follow_checkbox.setChecked.assert_called_once_with(False)
    for widget in (controller.follow_checkbox, controller.jump_to_user_button,
                   controller.jump_to_chat_button, controller.jump_to_comment_button):
        widget.setVisible.assert_called_once_with(False)
    controller._apply_location(controller.presence_by_user["Peer"])
    controller._show_error.assert_not_called()
    assert controller.presence_by_user and controller.chat_anchors


def test_disabling_highlights_keeps_non_tiled_peer_edit_without_overlay(monkeypatch):
    scene, status_messages = harness.install_fake_slicer(monkeypatch)
    peer = harness.Peer("receiver", scene)
    controller = peer.controller
    controller.owner.clear_remote_highlights = Mock()
    controller.owner.show_remote_change_highlight_crop = Mock()
    callbacks = []
    monkeypatch.setattr(sys.modules["qt"].QTimer, "singleShot", lambda delay, callback: callbacks.append(callback))
    before = np.zeros(harness.SHAPE, np.uint8)
    after = before.copy()
    after[1, 2, 3] = 1
    operation = {
        **collaboration.encode_mask_delta(before, after), "sequence": 1, "author": "sender",
        "segment_id": "A", "segment_name": "A", "color_hex": "#FF0000", "client_operation_id": "direct",
    }

    controller.set_remote_highlights_enabled(False)
    assert controller._apply_operations([operation])
    assert np.array_equal(peer.mask("A"), after) and controller.last_sequence == 1
    assert status_messages and not callbacks
    controller.owner.show_remote_change_highlight_crop.assert_not_called()
