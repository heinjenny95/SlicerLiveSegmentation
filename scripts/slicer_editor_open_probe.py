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

KEEP_ALIVE = []


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
    timer.stop()
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
    result["threshold_requests_all"] = calls
    threshold.scriptedEffect = original
    widget._editor_preprocessing.cleanup()
    assert threshold.scriptedEffect is original.original
    result["adapter_restored"] = True
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
