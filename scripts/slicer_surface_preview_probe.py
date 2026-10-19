"""Native asynchronous preview, labels, transform, metadata and cleanup checks."""
import hashlib
import json
import os
import threading
import time
import traceback
from pathlib import Path

import numpy as np
import qt
import slicer

KEEP_ALIVE = []


def pump_until(predicate, timeout=30):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        slicer.app.processEvents()
        if predicate():
            return
        time.sleep(.003)
    raise TimeoutError("Preview probe did not converge")


def digest_off_thread(array):
    result = []
    threading.Thread(target=lambda: result.append(hashlib.sha256(memoryview(array).cast("B")).hexdigest()), daemon=True).start()
    pump_until(lambda: bool(result))
    return result[0]


def run():
    report = {"ok": False, "model_initialized": False}
    widget = None
    beat = qt.QTimer()
    beats = []
    beat.setInterval(10)
    beat.timeout.connect(lambda: beats.append(time.monotonic()))
    try:
        widget = slicer.app.moduleManager().module("LiveSegmentation").widgetRepresentation().self()
        KEEP_ALIVE.append(widget)
        widget.live_collaboration.timer.stop()
        volume = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "Preview source")
        size = int(os.environ.get("LIVE_PREVIEW_PROBE_SIZE", "256"))
        slicer.util.updateVolumeFromArray(volume, np.zeros((size,)*3, np.uint8))
        node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Preview materials")
        node.CreateDefaultDisplayNodes()
        node.SetReferenceImageGeometryParameterFromVolumeNode(volume)
        node.GetSegmentation().AddEmptySegment("A", "A", (1, 0, 0))
        import vtkSegmentationCorePython as segmentation_core
        from vtk.util.numpy_support import numpy_to_vtk

        node.GetSegmentation().AddEmptySegment("0-empty", "Empty material")
        empty_image = segmentation_core.vtkOrientedImageData()
        empty_image.GetPointData().SetScalars(numpy_to_vtk(np.empty(0, np.uint8), deep=True))
        node.GetSegmentation().GetSegment("0-empty").AddRepresentation("Binary labelmap", empty_image)
        shape = tuple(int(v) for v in os.environ.get("LIVE_PREVIEW_PROBE_LABEL_SHAPE", f"{size},{size},{size}").split(","))
        mask = np.zeros(shape, np.uint8)
        mask[20:40, 25:45, 30:50] = 1
        mask[-2, -2, -2] = 1
        if shape == (size,)*3:
            slicer.util.updateSegmentBinaryLabelmapFromArray(mask, node, "A", volume)
        else:
            # A legacy full-size layer, without a full-volume utility conversion
            # or an extra native duplicate. Only this private fixture is changed.
            import vtk
            import vtkSegmentationCorePython as core
            from vtk.util.numpy_support import numpy_to_vtk

            image = core.vtkOrientedImageData()
            image.SetExtent(0, shape[2]-1, 0, shape[1]-1, 0, shape[0]-1)
            image.GetPointData().SetScalars(numpy_to_vtk(mask.reshape(-1), deep=False))
            matrix = vtk.vtkMatrix4x4()
            volume.GetIJKToRASMatrix(matrix)
            image.SetGeometryFromImageToWorldMatrix(matrix)
            node.GetSegmentation().GetSegment("A").AddRepresentation("Binary labelmap", image)
            KEEP_ALIVE.extend([image, mask])
        native = node.GetSegmentation().GetSegment("A").GetRepresentation("Binary labelmap")
        before_revision = native.GetPointData().GetScalars().GetMTime()
        before_hash = digest_off_thread(mask)
        editor = widget._standard_segment_editor_widget()
        editor.setSegmentationNode(node)
        preview = widget.surface_preview
        preview.bind(node)
        preview.scan_editors()
        assert slicer.util.findChild(editor, "Show3DButton").isHidden()
        assert preview.proxies and not preview.proxies[0][1].isHidden()
        before = time.monotonic()
        beat.start()
        preview.action.setChecked(True)
        pump_until(lambda: "A" in preview.models and preview.job is None)
        report["first_preview_s"] = time.monotonic()-before
        report["native_label_shape"] = shape
        assert native.GetPointData().GetScalars().GetMTime() == before_revision
        assert digest_off_thread(slicer.util.arrayFromSegmentInternalBinaryLabelmap(node, "A")) == before_hash
        assert not node.GetSegmentation().ContainsRepresentation("Closed surface")
        model = preview.models["A"]
        assert model.GetPolyData().GetNumberOfPolys() > 0
        assert "0-empty" not in preview.models and preview.active
        report["empty_material_does_not_abort_other_meshes"] = True
        assert not model.GetSaveWithScene() and model.GetHideFromEditors()
        report["polygons"] = model.GetPolyData().GetNumberOfPolys()
        segment = node.GetSegmentation().GetSegment("A")
        segment.SetColor(0, 0, 1)
        preview.tick()
        assert tuple(model.GetDisplayNode().GetColor()) == (0., 0., 1.)
        node.GetDisplayNode().SetSegmentVisibility("A", False)
        preview.tick()
        assert not model.GetDisplayNode().GetVisibility()
        node.GetDisplayNode().SetSegmentVisibility("A", True)
        transform = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLinearTransformNode", "Preview transform")
        node.SetAndObserveTransformNodeID(transform.GetID())
        preview.tick()
        assert model.GetTransformNodeID() == transform.GetID()
        # The native scalar buffer stays authoritative; worker output is only a view.
        revision_before_edit = preview.revision(segment)
        update_started = time.monotonic()
        mask[60:65, 60:65, 60:65] = 1
        slicer.util.arrayFromSegmentInternalBinaryLabelmap(node, "A")[60:65, 60:65, 60:65] = 1
        native.GetPointData().GetScalars().Modified()
        native.Modified()
        preview.note_reference_region("A", [60, 65, 60, 65, 60, 65], volume, revision_before_edit)
        changed_hash = digest_off_thread(mask)
        pump_until(lambda: preview.revisions.get("A") == preview.revision(segment) and preview.job is None)
        assert preview.pooled["A"]["incremental"]
        assert preview.pooled["A"]["scanned_voxels"] < 200000
        report["incremental_update_s"] = preview.pooled["A"]["ready_at"]-update_started
        report["incremental_scanned_voxels"] = preview.pooled["A"]["scanned_voxels"]
        assert digest_off_thread(slicer.util.arrayFromSegmentInternalBinaryLabelmap(node, "A")) == changed_hash
        assert not node.GetSegmentation().ContainsRepresentation("Closed surface")
        if shape == (size,)*3:
            # Clear the scalar data while retaining the source representation,
            # as native empty segments do. Removing every source representation
            # from a bound editor is not a valid clear operation.
            native = segment.GetRepresentation("Binary labelmap")
            native.Initialize()
            segment.Modified()
            preview.tick()
            assert "A" not in preview.models and model.GetScene() is None
            report["cleared_material_removes_stale_preview"] = True
        report["maximum_gui_gap_s"] = max([b-a for a, b in zip(beats, beats[1:])] or [0])
        assert report["maximum_gui_gap_s"] < .5
        other = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Non-room material")
        editor.setSegmentationNode(other)
        preview.scan_editors()
        assert not slicer.util.findChild(editor, "Show3DButton").isHidden()
        editor.setSegmentationNode(node)
        preview.scan_editors()
        assert slicer.util.findChild(editor, "Show3DButton").isHidden()
        report["non_room_editor_unaffected"] = True
        preview.bind(None)
        assert model.GetScene() is None
        assert not slicer.util.findChild(editor, "Show3DButton").isHidden()
        report.update(ok=True, label_data_unchanged=True, color_visibility_transform=True,
                      models_excluded_from_save=True, native_show3d_restored=True, cleanup=True)
    except Exception:
        report["error"] = traceback.format_exc()
    finally:
        beat.stop()
        if widget is not None:
            widget.cleanup()
        slicer.mrmlScene.Clear(0)
        Path(os.environ["LIVE_PREVIEW_PROBE_OUTPUT"]).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report), flush=True)
        slicer.app.exit(0 if report["ok"] else 1)


run()
