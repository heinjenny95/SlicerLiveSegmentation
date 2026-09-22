"""All phases of a local edit count as unsettled, including before encoding."""

from __future__ import annotations

import pytest

from tests.controller_harness import collaboration


@pytest.mark.parametrize(("field", "value"), [
    ("outgoing", [{"client_operation_id": "queued"}]),
    ("awaiting_echo", [{"client_operation_id": "accepted"}]),
    ("dirty_segments", {("node", "label")}),
    ("metadata_updates", {("node", "label")}),
    ("pending_segment_deletions", {"label": {}}),
    ("_local_capture", ("label", object())),
    ("_local_encode_pending", True),
    ("_pending_segmentation_event_ids", {"label"}),
    ("_pending_segmentation_event_unspecified", True),
    ("_segment_verifications", {"label": {}}),
])
def test_pending_edit_phases_are_preserved_on_leave(field, value):
    controller = collaboration.LiveCollaborationController(object())
    assert not controller._has_unsettled_local_work()
    setattr(controller, field, value)
    assert controller._has_unsettled_local_work()


def test_clean_room_needs_no_offline_recovery_copy():
    controller = collaboration.LiveCollaborationController(object())
    assert not controller._has_unsettled_local_work()
