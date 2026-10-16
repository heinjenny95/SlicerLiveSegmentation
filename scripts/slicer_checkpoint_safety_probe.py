"""Native visibility, target-preservation and checkpoint commit regression."""
import json
import os
import time
import traceback
from pathlib import Path

import numpy as np
import slicer

KEEP_ALIVE = []
print("CHECKPOINT_PROBE_SCRIPT_LOADED", os.environ.get("LIVE_CHECKPOINT_PROBE_OUTPUT"), flush=True)


def run():
    result = {"ok": False}
    print("CHECKPOINT_PROBE_RUNNING", flush=True)
    try:
        from LiveSegmentationLib.collaboration import (
            ChunkedMaskBaseline,
            PackedOperationMask,
            encode_chunked_mask_snapshot,
        )

        widget = slicer.app.moduleManager().module("LiveSegmentation").widgetRepresentation().self()
        controller = widget.live_collaboration
        KEEP_ALIVE.extend([widget, controller])
        controller.timer.stop()
        if widget.diagnostics_action is not None:
            controller._record_diagnostic_error("Native probe: readable diagnostic history", lane="probe")
            controller.show_diagnostic_history()
            assert "readable diagnostic history" in controller._diagnostic_text.toPlainText()
            assert "Errors (1)" == widget.diagnostics_action.text
            result["global_diagnostic_history_readable"] = True
        shape = (128, 128, 128)
        volume = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "Checkpoint source")
        slicer.util.updateVolumeFromArray(volume, np.zeros(shape, np.uint8))
        node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Checkpoint segmentation")
        node.CreateDefaultDisplayNodes()
        node.SetReferenceImageGeometryParameterFromVolumeNode(volume)
        node.GetSegmentation().AddEmptySegment("A", "A", (1, 0, 0))
        mask = np.zeros(shape, np.uint8)
        mask[1, 1, 1] = mask[126, 126, 126] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(mask, node, "A", volume)
        widget.source_volume_selector.setCurrentNode(volume)
        widget.segmentation_selector.setCurrentNode(node)
        controller.connected = True
        controller.initial_sync_complete = True
        controller.volume_shape = shape
        controller.segmentation_node_id = node.GetID()
        controller.user_name = "native-observer"
        controller.room_id = "isolated-native-probe"
        controller.last_sequence = 1
        controller._known_segment_ids = {"A"}
        controller.baselines[(node.GetID(), "A")] = ChunkedMaskBaseline.from_crop(shape, mask, [0, 128, 0, 128, 0, 128])
        controller.baseline_bounds[(node.GetID(), "A")] = [0, 128, 0, 128, 0, 128]
        errors = []
        controller._show_error = lambda message, popup=False: errors.append(str(message))
        controller._append_history_operations = lambda *a: None
        standard = widget._standard_segment_editor_widget()
        standard.setSegmentationNode(node)
        standard.setCurrentSegmentID("A")
        ir = None
        ir_module = slicer.app.moduleManager().module("SlicerNNInteractiveIR")
        if ir_module is not None and os.environ.get("LIVE_PROBE_SKIP_IR") != "1":
            ir = ir_module.widgetRepresentation().self()
            KEEP_ALIVE.append(ir)
            ir.set_editor_segmentation_node(node, "A")
            ir.store_segment_editor_target(node, "A", "native-probe")
        controller._ensure_segment(node, {"segment_id": "remote-B", "segment_name": "remote-B", "author": "remote", "color_hex": "#0000FF"})
        slicer.app.processEvents()
        assert standard.currentSegmentID() == "A"
        if ir is not None:
            assert ir.get_selected_segmentation_node_and_segment_id() == (node, "A")
        result["remote_label_preserves_standard_target"] = True
        result["remote_label_preserves_ir_target"] = ir is not None

        def drain(operation):
            operation = {**operation, "_packed": PackedOperationMask(operation)}
            controller._incoming_operations.append(operation)
            deadline = time.monotonic() + 30
            while controller._incoming_operations or (controller._checkpoint_group is not None and controller._checkpoint_group.complete):
                controller._incoming_retry_at = 0
                controller._drain_incoming_operations()
                slicer.app.processEvents()
                if errors:
                    raise AssertionError(errors)
                if time.monotonic() > deadline:
                    raise TimeoutError("Checkpoint did not converge")

        def group(target, first):
            baseline = ChunkedMaskBaseline.from_crop(shape, target, [0, 128, 0, 128, 0, 128])
            ops = encode_chunked_mask_snapshot(baseline)
            return [{**op, "segment_id": "A", "segment_name": "A", "author": "remote",
                     "color_hex": "#FF0000", "client_operation_id": f"probe-{first}-{i}",
                     "sequence": first+i, "system_snapshot": True, "snapshot_group_id": f"group-{first}",
                     "snapshot_group_index": i, "snapshot_group_count": len(ops)} for i, op in enumerate(ops)]

        ops = group(mask, 2)
        before_image = node.GetSegmentation().GetSegment("A").GetRepresentation("Binary labelmap")
        for op in ops:
            drain(op)
            assert np.array_equal(slicer.util.arrayFromSegmentBinaryLabelmap(node, "A", volume), mask)
        assert node.GetSegmentation().GetSegment("A").GetRepresentation("Binary labelmap") == before_image
        result["unchanged_checkpoint_no_native_rewrite"] = True
        if os.environ.get("LIVE_PROBE_SKIP_CHANGED") == "1":
            result.update(ok=True, changed_checkpoint_skipped_for_control=True)
            return
        changed = mask.copy()
        changed[1, 1, 1] = 0
        changed[2, 2, 2] = 1
        ops = group(changed, ops[-1]["sequence"]+1)
        for op in ops[:-1]:
            drain(op)
            assert np.array_equal(slicer.util.arrayFromSegmentBinaryLabelmap(node, "A", volume), mask)
        # Local work made after the checkpoint's authoritative baseline.
        local = mask.copy()
        local[60, 60, 60] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(local, node, "A", volume)
        drain(ops[-1])
        changed[60, 60, 60] = 1
        assert np.array_equal(slicer.util.arrayFromSegmentBinaryLabelmap(node, "A", volume), changed)
        assert standard.currentSegmentID() == "A"
        result.update(ok=True, native_changed_checkpoint_correct=True, local_stroke_preserved=True,
                      final_sequence=controller.last_sequence, errors=errors)
    except Exception:
        result["error"] = traceback.format_exc()
    finally:
        if "controller" in locals():
            # This probe installs a replica directly, without a transport or a
            # room join. Do not run joined-room teardown on that test fixture.
            controller.connected = False
            controller.segmentation_node_id = None
            controller.client = None
        if "ir" in locals() and ir is not None:
            ir.cleanup()
        if "widget" in locals():
            widget.cleanup()
        if "controller" in locals() and controller.diagnostic_log is not None:
            log = controller.diagnostic_log
            records = [json.loads(line) for path in log.directory.glob(log.path.name+"*")
                       for line in path.read_text(encoding="utf-8").splitlines()]
            assert any(r["event"] == "checkpoint.committed" for r in records)
            result["persistent_diagnostic_records"] = len(records)
        slicer.mrmlScene.Clear(0)
        output = Path(os.environ["LIVE_CHECKPOINT_PROBE_OUTPUT"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
        print(json.dumps(result))
        slicer.app.exit(0 if result["ok"] else 1)


run()
