"""A peer's incoming edit must not consume a queued local rename event."""

import numpy as np
import pytest

from tests import controller_harness as h
from tests.test_protected_claims import operation


@pytest.mark.parametrize("remote_metadata", [False, True])
def test_local_rename_survives_remote_apply_before_deferred_vtk_event(monkeypatch, remote_metadata):
    scene, _ = h.install_fake_slicer(monkeypatch)
    local, remote, log = h.Peer("local", scene), h.Peer("remote", scene), h.RoomLog()
    seed = log.commit(dict(operation("A", "seed", (1, 1, 1)), segment_name="Original"), "remote")
    for peer in (local, remote):
        peer.apply(seed)

    controller = local.controller
    controller._observed_node = local.node
    controller._observed_segmentation = local.node.GetSegmentation()
    segment = controller._observed_segmentation.GetSegment("A")
    segment.SetName("First Enter")
    segment.SetColor(0, 1, 0)
    controller._on_segmentation_modified(controller._observed_segmentation, "SegmentModifiedEvent", "A")
    assert controller._segmentation_event_flush_scheduled

    if remote_metadata:
        incoming = {**h.collaboration.encode_metadata_update(h.SHAPE), "segment_id": "A",
                    "client_operation_id": "remote-rename", "segment_name": "Peer name",
                    "color_hex": "#FF0000", "metadata_update": True}
    else:
        incoming = dict(operation("A", "remote-paint", (2, 2, 2)), operation_kind="patch",
                        segment_name="Original", metadata_update=False)
    stored = log.commit(incoming, "remote")
    for peer in (local, remote):
        peer.apply(stored)

    controller._flush_segmentation_events(controller._segmentation_event_generation)
    local.prepare()
    assert segment.GetName() == "First Enter"
    updates = [item for item in controller.outgoing if item.get("metadata_update")]
    assert [(item["segment_id"], item["segment_name"], item["color_hex"]) for item in updates] == [
        ("A", "First Enter", "#00FF00")
    ]
    published = log.commit(updates[0], "local")
    for peer in (local, remote):
        peer.apply(published)
        assert peer.node.GetSegmentation().GetSegment("A").GetName() == "First Enter"
        assert not peer.errors
    np.testing.assert_array_equal(local.mask("A"), remote.mask("A"))
    assert local.mask("A").sum() == (1 if remote_metadata else 2)


def test_rename_between_incoming_tiles_uses_current_metadata_at_completion(monkeypatch):
    scene, _ = h.install_fake_slicer(monkeypatch)
    peer, log = h.Peer("local", scene), h.RoomLog()
    peer.apply(log.commit(operation("A", "seed", (1, 1, 1)), "remote"))
    controller = peer.controller
    controller._observed_node = peer.node
    controller._observed_segmentation = peer.node.GetSegmentation()
    segment = controller._observed_segmentation.GetSegment("A")
    apply_operations = controller._apply_operations
    renamed = False

    def apply_piece(operations, **kwargs):
        nonlocal renamed
        result = apply_operations(operations, **kwargs)
        if operations[0].get("_partial_operation") and not renamed:
            renamed = True
            segment.SetName("Edited between tiles")
            controller._on_segmentation_modified(controller._observed_segmentation, "SegmentModifiedEvent", "A")
            # Simulate the deferred VTK callback running before the next GUI turn.
            controller._flush_segmentation_events(controller._segmentation_event_generation)
        return result

    monkeypatch.setattr(controller, "_apply_operations", apply_piece)
    patch = dict(operation("A", "remote-paint", (2, 2, 2)), operation_kind="patch")
    peer.apply(log.commit(patch, "remote"))
    peer.prepare()
    assert renamed
    assert segment.GetName() == "Edited between tiles"
    assert [item["segment_name"] for item in controller.outgoing if item.get("metadata_update")] == [
        "Edited between tiles"
    ]
