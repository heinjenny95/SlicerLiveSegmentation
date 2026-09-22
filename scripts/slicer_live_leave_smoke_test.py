"""Isolated Slicer probe for pending edits on leave/disconnection; synthetic data only."""

from __future__ import annotations

import json
import os
import tempfile
import traceback
from pathlib import Path

import numpy as np
import slicer
from LiveSegmentationLib.features import PendingOperationJournal


def run_probe():
    os.environ["LIVE_SEGMENTATION_NO_ERROR_POPUPS"] = "1"
    widget = slicer.modules.livesegmentation.widgetRepresentation().self()
    controller = widget.live_collaboration
    volume = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "Leave test source")
    slicer.util.updateVolumeFromArray(volume, np.zeros((8, 8, 8), np.int16))
    widget.source_volume_selector.setCurrentNode(volume)
    results = {}
    with tempfile.TemporaryDirectory(prefix="liveseg-leave-") as directory:
        try:
            for case in ("clean", "dirty", "queued-event", "encoding", "pending", "awaiting", "connection-loss"):
                try:
                    node = widget.prepare_shared_segmentation({"id": "test", "name": "Test"})
                    node_id = node.GetID()
                    segment_id = node.GetSegmentation().AddEmptySegment("LiveSeg-A", "A")
                    mask = np.zeros((8, 8, 8), np.uint8)
                    mask[2, 2, 2] = 1
                    slicer.util.updateSegmentBinaryLabelmapFromArray(mask, node, segment_id, volume)
                    controller.connected = True
                    controller.room_id = "test"
                    controller.segmentation_node_id = node_id
                    controller.volume_shape = mask.shape
                    controller.initial_sync_complete = True
                    widget.set_live_session_active(True)
                    journal = PendingOperationJournal(Path(directory) / f"{case}.json")
                    controller._operation_journal = journal
                    controller._journal_context = {"room": "test"}
                    controller.recovery_enabled_checkbox.checked = True
                    pending = {"client_operation_id": "pending-1", "segment_id": segment_id}
                    if case in ("dirty", "connection-loss"):
                        controller.dirty_segments.add((node_id, segment_id))
                    elif case == "queued-event":
                        controller._pending_segmentation_event_ids.add(segment_id)
                    elif case == "encoding":
                        controller._local_encode_pending = True
                    elif case == "pending":
                        controller.outgoing.append(pending)
                    elif case == "awaiting":
                        controller.awaiting_echo.append(pending)
                    if case == "connection-loss":
                        controller._disconnect_for_connection_loss("Synthetic disconnected folder")
                    else:
                        controller.leave()
                    retained = slicer.mrmlScene.GetNodeByID(node_id)
                    assert bool(retained) == (case != "clean"), "Pending local work was deleted, or clean room was retained"
                    assert widget.get_segmentation_node() is None
                    assert widget._standard_segment_editor_widget().segmentationNode() is None
                    if retained is not None:
                        assert retained.GetAttribute("LiveSegmentation.SharedReplica") != "1"
                        assert retained.GetAttribute("LiveSegmentation.LocalRecovery") == "1"
                        assert not retained.GetDisplayNode().GetVisibility()
                        editor = widget._standard_segment_editor_widget()
                        editor.setSegmentationNode(retained)
                        assert editor.readOnly, "Offline recovery can accidentally be edited"
                        editor.setSegmentationNode(None)
                        assert not editor.readOnly, "Recovery guard locked other editor work"
                        np.testing.assert_array_equal(slicer.util.arrayFromSegmentBinaryLabelmap(retained, segment_id, volume), mask)
                        widget.clear_live_segmentation()
                        assert slicer.mrmlScene.GetNodeByID(node_id) is not None, "New join would erase a recovery node"
                        assert slicer.util.saveNode(retained, str(Path(directory) / f"{case}.seg.nrrd"))
                    writer = journal._writer_thread
                    if writer:
                        writer.join(timeout=3)
                    if case in ("pending", "awaiting"):
                        assert journal.read({"room": "test"}) == [pending], "Explicit Leave erased pending journal"
                    results[case] = {"ok": True}
                except Exception:
                    results[case] = {"ok": False, "error": traceback.format_exc()}
        finally:
            widget.cleanup()
    return {"ok": all(item["ok"] for item in results.values()), "cases": results}


result = run_probe()
Path(os.environ["LIVE_SEGMENTATION_SLICER_PROBE_OUTPUT"]).write_text(json.dumps(result, indent=2), encoding="utf-8")
slicer.app.exit(0 if result["ok"] else 1)
