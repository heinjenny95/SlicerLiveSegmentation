"""Private native Show 3D lifecycle and regional-write regression.

The optional source is read-only. Only compact synthetic labels are created;
no reference-sized mask allocation, production room, or model is used.
"""
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

import numpy as np
import qt
import slicer
from vtk.util.numpy_support import vtk_to_numpy
from vtkSegmentationCorePython import vtkSegmentation

ROOT = Path(os.environ.get("LIVE_NATIVE_3D_PROBE_ROOT", tempfile.mkdtemp(prefix="native-3d-")))
REPORT = {"ok": False, "slicer": slicer.app.applicationVersion}
KEEP = []


def watch_progress():
    while not REPORT.get("finished"):
        time.sleep(5)
        frame = sys._current_frames().get(threading.main_thread().ident)
        if frame is not None:
            (ROOT / "progress.json").write_text(json.dumps({"report": REPORT,
                "stack": traceback.format_stack(frame)[-12:]}, indent=2), encoding="utf-8")


def wait(predicate, timeout=90):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("Native 3D probe condition")
        slicer.app.processEvents()
        time.sleep(.01)


def pause(seconds=.7):
    deadline = time.monotonic() + seconds
    wait(lambda: time.monotonic() > deadline)


def run():
    from LiveSegmentationLib import collaboration as core

    window = slicer.util.mainWindow()
    window.setAttribute(qt.Qt.WA_DontShowOnScreen, True)
    window.hide()
    source = os.environ.get("LIVE_NATIVE_3D_SOURCE")
    volume = slicer.util.loadVolume(source) if source else slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "Native 3D private source")
    if not source:
        slicer.util.updateVolumeFromArray(volume, np.zeros((96, 96, 96), np.uint8))
    REPORT["source_shape_kji"] = shape = list(reversed(volume.GetImageData().GetDimensions()))
    widget = slicer.modules.livesegmentation.widgetRepresentation().self()
    REPORT["live_module"] = str(slicer.modules.livesegmentation.path)
    slicer.util.selectModule("SlicerNNInteractiveIR")
    ir = slicer.modules.slicernninteractiveir.widgetRepresentation().self()
    KEEP.extend((widget, ir))
    node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Native 3D private labels")
    node.CreateDefaultDisplayNodes()
    node.SetReferenceImageGeometryParameterFromVolumeNode(volume)
    segmentation = node.GetSegmentation()
    offset = [max(0, v // 2 - 32) for v in shape]
    logic = slicer.modules.segmentations.logic()
    for index, sid in enumerate(("A", "B")):
        segmentation.AddEmptySegment(sid, "Private " + sid, (1, .3 + index * .5, .2))
        mask = np.zeros((24, 24, 24), np.uint8)
        mask[4:16, 4:16, 4:16] = 1
        origin = [v + index * 28 for v in offset]
        bounds = [value for v in origin for value in (v, v + 24)]
        image = widget._oriented_image_from_reference_crop(mask, bounds, volume, node)
        assert logic.SetBinaryLabelmapToSegment(image, node, sid, logic.MODE_REPLACE)
    segmentation.SetConversionParameter("Smoothing factor", "0.25")
    editor = widget._standard_segment_editor_widget()
    editor.setSegmentationNode(node)
    editor.setSourceVolumeNode(volume)
    ir.ui.editor_widget.setSegmentationNode(node)
    ir.ui.editor_widget.setSourceVolumeNode(volume)
    editor.setCurrentSegmentID("A")
    ir.ui.editor_widget.setCurrentSegmentID("B")
    button = slicer.util.findChild(editor, "Show3DButton")
    ir_button = slicer.util.findChild(ir.ui.editor_widget, "Show3DButton")
    button.setChecked(True)
    wait(lambda: segmentation.ContainsRepresentation("Closed surface"))
    display = node.GetDisplayNode()
    display.SetSegmentVisibility("B", False)
    region = [value for start in offset for value in (start, start + 64)]
    def digest(target, sid):
        return hashlib.sha256(widget.segment_mask_region_in_reference_geometry(target, sid, volume, region).tobytes()).hexdigest()
    before = {sid: digest(node, sid)
              for sid in ("A", "B")}
    history = core.location_history_settings
    core.location_history_settings = lambda: history(str(ROOT / "settings"))
    widget.source_volume_selector.setCurrentNode(volume)
    widget.segmentation_selector.setCurrentNode(node)
    live = widget.live_collaboration
    live.user_edit.setText("Private native 3D")
    live.room_edit.setText("Private native 3D")
    live.transport_combo.setCurrentIndex(0)
    live.shared_folder_edit.setEditText(str(ROOT / "share"))
    live.backup_enabled_checkbox.checked = False
    live.join()
    wait(lambda: live.connected and live.initial_sync_complete and not live.dirty_segments
         and not live.outgoing and not live.awaiting_echo and not live._local_encode_pending)
    original_node = node
    node = live._segmentation_node()
    REPORT["room_uses_new_shared_node"] = node != original_node
    segmentation = node.GetSegmentation()
    display = node.GetDisplayNode()
    editor.setSegmentationNode(node)
    ir.ui.editor_widget.setSegmentationNode(node)
    editor.setSourceVolumeNode(volume)
    ir.ui.editor_widget.setSourceVolumeNode(volume)
    pause()
    assert not button.isHidden(), "Native standard Show 3D button was hidden"
    assert not ir_button.isHidden(), "Native IR Show 3D button was hidden"
    assert slicer.util.findChild(editor, "Show3DButton") == button
    assert slicer.util.findChild(ir.ui.editor_widget, "Show3DButton") == ir_button
    REPORT["native_button_python_type"] = type(button).__name__
    assert not any("3D preview" in str(action.text) for action in widget.navigation_toolbar.actions())
    assert not getattr(widget, "surface_preview", None)
    assert not any(node.GetName() == "Live 3D PREVIEW" for node in slicer.util.getNodesByClass("vtkMRMLModelNode"))
    assert before == {sid: digest(node, sid) for sid in ("A", "B")}
    if not segmentation.ContainsRepresentation("Closed surface"):
        button.setChecked(True)
        wait(lambda: segmentation.ContainsRepresentation("Closed surface"))
    segmentation.SetConversionParameter("Smoothing factor", "0.25")
    parameter_names = ("Smoothing factor", "Decimation factor", "Joint smoothing", "Conversion method")
    parameters = {key: segmentation.GetConversionParameter(key) for key in parameter_names}
    display.SetSegmentVisibility("B", False)
    before_visibility = [bool(display.GetVisibility3D()), bool(display.GetSegmentVisibility("B"))]
    REPORT["native_buttons_and_surfaces_available_in_room"] = True
    # The native button still owns removal/recreation of actual full-resolution
    # closed surfaces, with the user's conversion parameters unchanged.
    button.setChecked(False)
    wait(lambda: not segmentation.ContainsRepresentation("Closed surface"))
    button.setChecked(True)
    wait(lambda: segmentation.ContainsRepresentation("Closed surface"))
    REPORT["native_toggle_roundtrip"] = True
    sid = "A"
    image = segmentation.GetSegment(sid).GetRepresentation("Binary labelmap")
    extent = list(image.GetExtent())
    bbox = [extent[4], extent[5] + 3, extent[2], extent[3] + 3, extent[0], extent[1] + 3]
    for _ in widget.prepare_segment_labelmap_extent(node, sid, volume, bbox):
        slicer.app.processEvents()
    assert segmentation.ContainsRepresentation("Closed surface"), "Extent preparation removed native 3D"
    REPORT["native_surface_after_extent_preparation"] = True
    image = segmentation.GetSegment(sid).GetRepresentation("Binary labelmap")
    array = slicer.util.arrayFromSegmentInternalBinaryLabelmap(node, sid)
    empty = np.argwhere(array == 0)[0]
    extent = image.GetExtent()
    z, y, x = (int(empty[0]) + extent[4], int(empty[1]) + extent[2], int(empty[2]) + extent[0])
    assert widget.update_segment_binary_labelmap_crop(np.zeros((1, 1, 1), np.uint8), np.ones((1, 1, 1), np.uint8),
               [z, z+1, y, y+1, x, x+1], node, sid, volume)
    assert segmentation.ContainsRepresentation("Closed surface"), "Regional update removed native 3D"
    assert segmentation.GetSegment(sid).GetRepresentation("Closed surface").GetNumberOfPoints() > 0
    REPORT["native_surface_after_regional_write"] = True
    image = segmentation.GetSegment(sid).GetRepresentation("Binary labelmap")
    staged_image = image.NewInstance()
    staged_image.DeepCopy(image)
    values = vtk_to_numpy(staged_image.GetPointData().GetScalars())
    values[:] = 0
    values[len(values)//2] = 1
    staged_image.GetPointData().GetScalars().Modified()
    stage = dict(segment_id=sid, segment=segmentation.GetSegment(sid), old_image=image,
                 old_revision=(image.GetMTime(), image.GetPointData().GetScalars().GetMTime(), int(segmentation.GetSegment(sid).GetLabelValue())),
                 volume=volume, matrix=widget._reference_ijk_to_segmentation_ras_matrix(volume, node), image=staged_image)
    assert widget.commit_checkpoint_segments(node, [stage])
    assert segmentation.ContainsRepresentation("Closed surface"), "Checkpoint removed native 3D"
    assert int(np.count_nonzero(slicer.util.arrayFromSegmentInternalBinaryLabelmap(node, sid))) == 1
    assert segmentation.GetSegment(sid).GetRepresentation("Closed surface").GetNumberOfPoints() > 0
    REPORT["native_surface_after_checkpoint"] = True
    ir.get_volume_node = lambda: volume
    ir_bounds = [value for start in offset for value in (start + 8, start + 10)]
    reference = ir.get_or_create_crop_reference_volume_node(tuple((ir_bounds[i], ir_bounds[i+1]) for i in (0, 2, 4)))
    ir.update_segment_binary_labelmap_from_array(np.ones((2, 2, 2), np.uint8), node, sid, reference)
    assert segmentation.ContainsRepresentation("Closed surface"), "IR regional writer removed native 3D"
    assert segmentation.GetSegment(sid).GetRepresentation("Closed surface").GetNumberOfPoints() > 0
    REPORT["native_surface_after_ir_regional_write"] = True
    assert before["B"] == digest(node, "B")
    assert [bool(display.GetVisibility3D()), bool(display.GetSegmentVisibility("B"))] == before_visibility
    assert {key: segmentation.GetConversionParameter(key) for key in parameter_names} == parameters
    expected = vtkSegmentation()
    expected.DeepCopy(segmentation)
    expected.RemoveRepresentation("Closed surface")
    assert expected.CreateRepresentation("Closed surface")
    actual_mesh = segmentation.GetSegment(sid).GetRepresentation("Closed surface")
    expected_mesh = expected.GetSegment(sid).GetRepresentation("Closed surface")
    actual_points = vtk_to_numpy(actual_mesh.GetPoints().GetData())
    expected_points = vtk_to_numpy(expected_mesh.GetPoints().GetData())
    assert actual_mesh.GetNumberOfPolys() == expected_mesh.GetNumberOfPolys()
    assert actual_points.shape == expected_points.shape
    def sorted_points(points):
        return points[np.lexsort((points[:, 2], points[:, 1], points[:, 0]))]
    assert np.allclose(sorted_points(actual_points), sorted_points(expected_points), atol=1e-5)
    REPORT["native_mesh_matches_fresh_native_conversion"] = True
    REPORT["native_surface_points"] = len(actual_points)
    REPORT["appearance_settings"] = parameters
    pause()
    wait(lambda: not live.dirty_segments and not live.outgoing and not live.awaiting_echo
         and not live._local_encode_pending)
    live.leave()
    wait(lambda: not live.connected)
    assert not button.isHidden() and not ir_button.isHidden()
    assert segmentation.ContainsRepresentation("Closed surface")
    REPORT.update(ok=True, conversion_settings_preserved=True, other_label_unchanged=True,
                  native_controls_preserved_after_leave=True, model_initialized=False)


def start():
    ROOT.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=watch_progress, daemon=True).start()
    try:
        run()
    except Exception:
        REPORT["error"] = traceback.format_exc()
    REPORT["finished"] = True
    (ROOT / "result.json").write_text(json.dumps(REPORT, indent=2), encoding="utf-8")
    slicer.app.exit(0 if REPORT["ok"] else 1)


qt.QTimer.singleShot(300, start)
