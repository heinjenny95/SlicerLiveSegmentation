"""Native editor-opening regression, optionally on a real BigTIFF volume."""
from __future__ import annotations

import json
import os
import time
import traceback
from pathlib import Path

import numpy as np
import qt
import slicer
import vtk
from vtk.util.numpy_support import numpy_to_vtk, vtk_to_numpy

KEEP_ALIVE = []


def check_native_range_equivalence(guard):
    if guard.backend is None:
        return {"skipped": guard.reason}
    checked = []
    for dtype in (np.uint8, np.int8, np.uint16, np.int16, np.uint32, np.int32, np.uint64, np.int64):
        limits = np.iinfo(dtype)
        values = np.array([limits.min, 0, 1, limits.max] * 16, dtype=dtype)
        image = vtk.vtkImageData()
        image.SetExtent(5, 8, -2, 1, 11, 14)
        image.GetPointData().SetScalars(numpy_to_vtk(values, deep=True))
        scalars = image.GetPointData().GetScalars()
        reference = vtk.vtkImageData()
        reference.DeepCopy(image)
        expected, revision = reference.GetScalarRange(), image.GetMTime()
        assert guard.backend.prime(image)
        assert image.GetScalarRange() == expected, (dtype, image.GetScalarRange(), expected)
        assert image.GetMTime() == revision and image.GetPointData().GetScalars() == scalars
        assert image.GetExtent() == (5, 8, -2, 1, 11, 14)
        assert np.array_equal(vtk_to_numpy(scalars), values)
        scalars.SetValue(3, 0)
        scalars.Modified()
        reference.DeepCopy(image)
        assert guard.backend.prime(image) and image.GetScalarRange() == reference.GetScalarRange()
        checked.append(np.dtype(dtype).name)
    # Real VTK unsupported inputs must not enter the protected native method.
    for kind in ("float_nan", "vector", "point_ghost", "cell_ghost", "cell_scalars"):
        image = vtk.vtkImageData()
        image.SetDimensions(4, 4, 4)
        image.AllocateScalars(vtk.VTK_FLOAT if kind == "float_nan" else vtk.VTK_UNSIGNED_CHAR,
                              2 if kind == "vector" else 1)
        image.GetPointData().GetScalars().FillComponent(0, float("nan") if kind == "float_nan" else 7)
        if kind.endswith("ghost"):
            ghost = vtk.vtkUnsignedCharArray()
            ghost.SetName("vtkGhostType")
            attributes = image.GetPointData() if kind == "point_ghost" else image.GetCellData()
            ghost.SetNumberOfTuples(image.GetNumberOfPoints() if kind == "point_ghost" else image.GetNumberOfCells())
            ghost.Fill(0)
            attributes.AddArray(ghost)
        if kind == "cell_scalars":
            cells = vtk.vtkUnsignedCharArray()
            cells.SetNumberOfTuples(image.GetNumberOfCells())
            cells.Fill(5)
            image.GetCellData().SetScalars(cells)
        before, revision = guard.backend.calls, image.GetMTime()
        assert not guard.backend.prime(image), kind
        assert guard.backend.calls == before and image.GetMTime() == revision
    return {"integer_types": checked, "nonzero_extent_and_revision": True,
            "unsupported_native_inputs_declined": True}


def run():
    widget = slicer.modules.livesegmentation.widgetRepresentation().self()
    KEEP_ALIVE.append(widget)
    started = time.monotonic()
    dataset = os.environ.get("LIVE_EDITOR_DATASET")
    if dataset:
        volume = slicer.util.loadNodeFromFile(dataset, "BigTiffVolumeFile")
    else:
        volume = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "Editor source")
        size = int(os.environ.get("LIVE_EDITOR_SIZE", "1024"))
        data = np.zeros((size, size, size), np.uint8)
        data[0, 0, 0], data[-1, -1, -1] = 3, 211
        slicer.util.updateVolumeFromArray(volume, data)
        del data
    load_seconds = time.monotonic()-started
    volume.CreateDefaultDisplayNodes()
    volume.GetDisplayNode().SetAutoWindowLevel(False)
    volume.GetDisplayNode().SetWindowLevel(255, 127.5)
    segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Editor labels")
    segmentation.CreateDefaultDisplayNodes()
    segmentation.SetReferenceImageGeometryParameterFromVolumeNode(volume)
    sid = segmentation.GetSegmentation().AddEmptySegment("Probe", "Probe", [1, 0, 0])
    editor = widget._standard_segment_editor_widget()
    threshold = editor.effectByName("Threshold").self()
    original = threshold.scriptedEffect
    calls = []

    class SourceRequests:
        def __getattr__(self, name):
            return getattr(original, name)

        def sourceVolumeImageData(self):
            t0 = time.monotonic()
            image = original.sourceVolumeImageData()
            calls.append({"active": original.active(), "returned_image": image is not None,
                          "seconds": time.monotonic()-t0})
            return image

    threshold.scriptedEffect = SourceRequests()
    source_image = volume.GetImageData()
    source_scalars = source_image.GetPointData().GetScalars()
    source_time = source_image.GetMTime()
    beats = []
    timer = qt.QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: beats.append(time.monotonic()))
    timer.start()
    start = time.monotonic()
    widget._configure_standard_segment_editor(editor, segmentation, volume)
    configured = time.monotonic()
    slicer.util.selectModule("SegmentEditor")
    editor.setCurrentSegmentID(sid)
    editor.setActiveEffectByName("Paint")
    for _ in range(20):
        slicer.app.processEvents()
        time.sleep(0.01)
    result = {"ok": True, "module_path": slicer.util.modulePath("LiveSegmentation"),
              "shape": list(reversed(source_image.GetDimensions())),
              "source_bytes": int(slicer.util.arrayFromVolume(volume).nbytes),
              "load_seconds": load_seconds, "configure_seconds": configured-start,
              "open_and_paint_seconds": time.monotonic()-start,
              "maximum_gui_gap_seconds": float(np.diff([start, *beats, time.monotonic()]).max()),
              "threshold_requests": list(calls)}
    range_guard = getattr(widget, "_source_range_preprocessing", None)
    if range_guard is not None:
        result["source_range_backend"] = range_guard.reason
        result["source_range_calls"] = range_guard.backend.calls if range_guard.backend else 0
        result["source_range_seconds"] = range_guard.backend.seconds if range_guard.backend else None
    timer.stop()
    if os.environ.get("LIVE_EDITOR_ASSERT_FAST", "0") == "1":
        assert result["maximum_gui_gap_seconds"] < 1.0, result
    assert calls and all(not c["active"] and not c["returned_image"] for c in calls), calls
    assert editor.sourceVolumeNode() == volume and editor.segmentationNode() == segmentation
    assert volume.GetImageData() == source_image and source_image.GetMTime() == source_time
    assert source_image.GetPointData().GetScalars() == source_scalars
    for name in ("LiveSegmentation", "SegmentEditor"):
        slicer.util.selectModule(name)
    t0 = time.monotonic()
    widget._configure_standard_segment_editor(editor, segmentation, volume)
    result["rebind_seconds"] = time.monotonic()-t0
    assert result["rebind_seconds"] < .1
    small = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "Threshold QA")
    values = np.arange(16**3, dtype=np.int16).reshape(16, 16, 16)-2000
    slicer.util.updateVolumeFromArray(small, values)
    small_seg = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Threshold QA labels")
    small_seg.CreateDefaultDisplayNodes()
    small_seg.SetReferenceImageGeometryParameterFromVolumeNode(small)
    small_sid = small_seg.GetSegmentation().AddEmptySegment("ThresholdQA", "ThresholdQA", [0, 0, 1])
    widget._configure_standard_segment_editor(editor, small_seg, small)
    editor.setCurrentSegmentID(small_sid)
    editor.setActiveEffectByName("Threshold")
    effect = editor.activeEffect()
    effect.setParameter("MinimumThreshold", -100)
    effect.setParameter("MaximumThreshold", 100)
    effect.self().onApply()
    actual = slicer.util.arrayFromSegmentBinaryLabelmap(small_seg, small_sid, small)
    assert np.array_equal(actual != 0, (values >= -100) & (values <= 100))
    result["active_threshold_exact"] = True
    assert any(c["active"] and c["returned_image"] for c in calls)
    editor.setActiveEffectByName("Paint")
    assert editor.activeEffect().name == "Paint"
    editor.setActiveEffectByName("Erase")
    assert editor.activeEffect().name == "Erase"
    result["paint_erase_activation"] = True
    paint_sid = small_seg.GetSegmentation().AddEmptySegment("PaintQA", "PaintQA", [0, 1, 0])
    editor.setCurrentSegmentID(paint_sid)
    slicer.util.setSliceViewerLayers(background=small)
    slicer.util.resetSliceViews()
    red = slicer.app.layoutManager().sliceWidget("Red")
    red.sliceLogic().SetSliceOffset(8)
    slicer.app.processEvents()
    view = red.sliceView()
    center = (int(view.width/2), int(view.height/2))
    editor.setActiveEffectByName("Paint")
    editor.activeEffect().setCommonParameter("BrushDiameterIsRelative", 0)
    editor.activeEffect().setCommonParameter("BrushAbsoluteDiameter", 3)
    slicer.util.clickAndDrag(view, start=center, end=center, steps=1)
    painted = slicer.util.arrayFromSegmentBinaryLabelmap(small_seg, paint_sid, small)
    assert np.count_nonzero(painted) > 0, "Native brush did not paint"
    result["paint_voxels"] = int(np.count_nonzero(painted))
    editor.setActiveEffectByName("Erase")
    slicer.util.clickAndDrag(view, start=center, end=center, steps=1)
    erased = slicer.util.arrayFromSegmentBinaryLabelmap(small_seg, paint_sid, small)
    assert np.count_nonzero(erased) == 0, "Native eraser did not remove its brush stroke"
    result["native_paint_erase"] = True
    # Bypass Live's explicit configure helper: native source selection and later
    # source image updates must also be primed by high-priority observers.
    manual = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "Manual source QA")
    manual_values = np.zeros((256, 256, 256), np.uint16)
    manual_values[0, 0, 0], manual_values[-1, -1, -1] = 7, 60000
    slicer.util.updateVolumeFromArray(manual, manual_values)
    before = time.monotonic()
    editor.setSourceVolumeNode(manual)
    result["manual_source_seconds"] = time.monotonic()-before
    assert manual.GetImageData().GetScalarRange() == (0., 60000.)
    view_array = slicer.util.arrayFromVolume(manual)
    view_array[0, 0, 0] = 65535
    before = time.monotonic()
    slicer.util.arrayFromVolumeModified(manual)
    result["source_revision_seconds"] = time.monotonic()-before
    assert manual.GetImageData().GetScalarRange() == (0., 65535.)
    assert result["manual_source_seconds"] < 1. and result["source_revision_seconds"] < 1.
    result["manual_source_and_revision_correct"] = True
    backend_calls = range_guard.backend.calls if range_guard.backend else None
    editor.setSourceVolumeNode(None)
    view_array[0, 0, 0] = 65000
    slicer.util.arrayFromVolumeModified(manual)
    assert range_guard.backend is None or range_guard.backend.calls == backend_calls, "Detached source still observed"
    result["old_source_detached"] = True
    result["native_range_equivalence"] = check_native_range_equivalence(range_guard)
    old_parameter = editor.mrmlSegmentEditorNode()
    editor.setMRMLSegmentEditorNode(None)
    slicer.mrmlScene.RemoveNode(old_parameter)
    new_parameter = slicer.vtkMRMLSegmentEditorNode()
    new_parameter.SetSingletonTag("SegmentEditor")
    new_parameter = slicer.mrmlScene.AddNode(new_parameter)
    editor.setMRMLSegmentEditorNode(new_parameter)
    editor.setSegmentationNode(small_seg)
    editor.setSourceVolumeNode(manual)
    assert range_guard.parameter == new_parameter and range_guard.source == manual, {
        "guard_parameter": range_guard.parameter.GetID() if range_guard.parameter else None,
        "new_parameter": new_parameter.GetID(), "singleton": new_parameter.GetSingletonTag(),
        "guard_source": range_guard.source.GetID() if range_guard.source else None,
        "editor_source": editor.sourceVolumeNode().GetID() if editor.sourceVolumeNode() else None,
        "parameter_source": new_parameter.GetSourceVolumeNode().GetID() if new_parameter.GetSourceVolumeNode() else None}
    assert manual.GetImageData().GetScalarRange() == (0., 65000.)
    result["native_parameter_replacement"] = True
    result["threshold_requests_all"] = calls
    threshold.scriptedEffect = original
    widget._editor_preprocessing.cleanup()
    assert threshold.scriptedEffect is original.original
    result["adapter_restored"] = True
    range_guard.cleanup()
    range_guard.cleanup()
    assert not range_guard.prepare(manual)
    result["source_range_cleanup"] = True
    KEEP_ALIVE.extend([volume, segmentation, editor, small, small_seg])
    return result


def main():
    try:
        result = run()
    except Exception:
        result = {"ok": False, "traceback": traceback.format_exc()}
    Path(os.environ["LIVE_EDITOR_OUTPUT"]).write_text(json.dumps(result, indent=2), encoding="utf-8")
    slicer.app.exit(0 if result["ok"] else 1)


main()
