"""Run with Slicer's --testing --python-script, never in a working user scene."""

from __future__ import annotations

import json
import os
import traceback
from pathlib import Path

import numpy as np
import slicer


def run_probe():
    widget = slicer.modules.livesegmentation.widgetRepresentation().self()
    editor = widget._standard_segment_editor_widget()
    report = {"default_undo_limit": int(editor.maximumNumberOfUndoStates)}
    volume = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "Undo test source")
    slicer.util.updateVolumeFromArray(volume, np.zeros((8, 8, 8), np.int16))
    widget.source_volume_selector.setCurrentNode(volume)

    def seed():
        node = widget._create_segmentation_node("Undo test segmentation")
        widget._configure_standard_segment_editor(editor, node, volume)
        segment_id = node.GetSegmentation().AddEmptySegment("LiveSeg-A", "A")
        mask = np.zeros((8, 8, 8), np.uint8)
        mask[2, 2, 2] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(mask, node, segment_id, volume)
        editor.saveStateForUndo()
        remote_id = node.GetSegmentation().AddEmptySegment("LiveSeg-B", "Remote B")
        mask[4, 4, 4] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(mask, node, remote_id, volume)
        return node, remote_id

    try:
        # Positive control: offline Undo really restores the complete old state.
        node, remote_id = seed()
        editor.undo()
        report["offline_undo_removes_new_label"] = node.GetSegmentation().GetSegment(remote_id) is None
        assert report["offline_undo_removes_new_label"], "Positive control did not exercise native Undo"
        editor.setSegmentationNode(None)
        slicer.mrmlScene.RemoveNode(node)

        node, remote_id = seed()
        node.SetAttribute("LiveSegmentation.SharedReplica", "1")
        widget.set_live_session_active(True)
        widget._configure_standard_segment_editor(editor, node, volume)
        report["live_undo_limit"] = int(editor.maximumNumberOfUndoStates)
        editor.undo()  # Must be harmless even with an old full-scene Undo state.
        report["live_remote_label_preserved"] = node.GetSegmentation().GetSegment(remote_id) is not None
        assert report["live_remote_label_preserved"], "Native Undo removed a collaborator's label in a live session"
        widget.clear_live_segmentation(node.GetID())
        widget.set_live_session_active(False)
        report["undo_limit_after_leave"] = int(editor.maximumNumberOfUndoStates)
        assert report["undo_limit_after_leave"] == report["default_undo_limit"], "Offline Undo setting was not restored"
        node, remote_id = seed()
        editor.undo()
        report["offline_undo_works_after_leave"] = node.GetSegmentation().GetSegment(remote_id) is None
        assert report["offline_undo_works_after_leave"]
        report["ok"] = True
    except Exception:
        report["ok"] = False
        report["error"] = traceback.format_exc()
    finally:
        widget.set_live_session_active(False)
        widget.cleanup()
    return report


result = run_probe()
Path(os.environ["LIVE_SEGMENTATION_SLICER_PROBE_OUTPUT"]).write_text(json.dumps(result, indent=2), encoding="utf-8")
slicer.app.exit(0 if result["ok"] else 1)
