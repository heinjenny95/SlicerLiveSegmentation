"""Native regression for ROI-origin offsets, affine sampling and large data."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import traceback
import uuid
from pathlib import Path

import numpy as np
import qt
import slicer
import vtk

KEEP_ALIVE = []


def pump_until(predicate, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        slicer.app.processEvents()
        if predicate():
            return
        time.sleep(0.002)
    raise TimeoutError("ROI collaboration probe did not converge")


def large_probe(widget):
    from LiveSegmentationLib.collaboration import SharedFolderRoomClient, encode_mask_delta

    controller = widget.live_collaboration
    size = int(os.environ.get("LIVE_ROI_SIZE", "1024"))
    volume = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "Large ROI source")
    loaded = time.monotonic()
    dataset = os.environ.get("LIVE_ROI_DATASET")
    if dataset:
        slicer.mrmlScene.RemoveNode(volume)
        volume = slicer.util.loadNodeFromFile(dataset, "BigTiffVolumeFile")
        assert volume is not None, "Real large TIFF failed to load"
    else:
        slicer.util.updateVolumeFromArray(volume, np.zeros((size, size, size), np.uint8))
    shape = tuple(reversed(volume.GetImageData().GetDimensions()))
    result = {"source_shape": shape, "source_bytes": int(slicer.util.arrayFromVolume(volume).nbytes),
              "real_dataset": bool(dataset), "source_load_seconds": time.monotonic()-loaded}
    widget.source_volume_selector.setCurrentNode(volume)
    folder = os.environ.get("LIVE_ROI_SHARED_FOLDER") or tempfile.mkdtemp(prefix="live-roi-geometry-")
    controller.transport_combo.setCurrentIndex(0)
    controller.shared_folder_edit.setEditText(folder)
    controller.room_edit.setText("roi-probe-"+uuid.uuid4().hex)
    controller.user_edit.setText("roi-observer")
    controller.join()
    pump_until(lambda: controller.connected and controller.initial_sync_complete)
    peer = SharedFolderRoomClient(folder, "roi-peer")
    joined = peer.join(controller.room_edit.text, controller.source_volume_signature)
    KEEP_ALIVE.extend([volume, controller, peer])
    node = controller._segmentation_node()
    roi = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "nnInteractiveIR ROI Reference (hidden)")
    roi.SetHideFromEditors(True)
    matrix = vtk.vtkMatrix4x4()
    volume.GetIJKToRASMatrix(matrix)
    roi.SetIJKToRASMatrix(matrix)
    origin = matrix.MultiplyPoint([100, 90, 80, 1])
    roi.SetOrigin(*origin[:3])
    data = np.zeros((272, 238, 295), np.uint8)
    data[8:200, 8:180, 8:40] = 1
    slicer.util.updateVolumeFromArray(roi, data)
    result["roi_shape"] = list(data.shape)
    sid = node.GetSegmentation().AddEmptySegment("", "Shifted ROI", [1, 0, 0])
    slicer.util.updateSegmentBinaryLabelmapFromArray(data, node, sid, roi)
    pump_until(lambda: controller.last_sequence > 0 and not controller.dirty_segments
               and not controller.outgoing and not controller.awaiting_echo and not controller._local_encode_pending)
    sid = next(str(item) for item in node.GetSegmentation().GetSegmentIDs()
               if node.GetSegmentation().GetSegment(item).GetName() == "Shifted ROI")
    # Force the exact non-canonical ROI geometry observed in user sessions after
    # initial synchronization: native ROI result written again at a local origin.
    slicer.util.updateSegmentBinaryLabelmapFromArray(data, node, sid, roi)
    heartbeat = qt.QTimer()
    heartbeat.setInterval(10)
    beats = []
    heartbeat.timeout.connect(lambda: beats.append(time.monotonic()))
    heartbeat.start()
    start = time.monotonic()
    errors = []
    target = [controller.last_sequence]
    original = slicer.util.arrayFromSegmentBinaryLabelmap

    def forbidden(*args, **kwargs):
        raise AssertionError("Full reference export requested during large ROI synchronization")

    slicer.util.arrayFromSegmentBinaryLabelmap = forbidden
    try:
        def publish():
            try:
                for index in range(12):
                    patch = np.ones((1, 4, 4), np.uint8)
                    encoded = encode_mask_delta(np.zeros_like(patch), patch)
                    encoded["volume_shape"] = list(shape)
                    encoded["voxel_bbox"] = [90+index, 91+index, 102, 106, 150, 154]
                    sent = peer.push_operation(joined["id"], {"client_operation_id": str(uuid.uuid4()),
                        "segment_id": sid, "segment_name": "Shifted ROI", "color_hex": "#FF0000",
                        "base_sequence": target[0], **encoded})
                    target[0] = int(sent["sequence"])
                    time.sleep(0.02)
            except Exception:
                errors.append(traceback.format_exc())
        worker = threading.Thread(target=publish, daemon=True)
        worker.start()
        pump_until(lambda: not worker.is_alive() and controller.last_sequence >= target[0]
                   and not controller._incoming_operations)
        if errors:
            raise RuntimeError(errors[0])
        region = widget.segment_mask_region_in_reference_geometry(node, sid, volume, [90, 102, 102, 106, 150, 154])
        assert np.count_nonzero(region) == 192, "Remote ROI stroke components were lost"
        original_region = widget.segment_mask_region_in_reference_geometry(node, sid, volume, [88, 120, 98, 130, 108, 140])
        assert np.all(original_region), "Existing ROI foreground was changed"
        gaps = np.diff([start, *beats, time.monotonic()])
        result.update({"remote_strokes": 12, "remote_voxels": 192,
                       "sync_seconds": time.monotonic()-start, "heartbeat_count": len(beats),
                       "maximum_gui_gap_seconds": float(gaps.max()),
                       "full_reference_export_calls": 0, "original_roi_preserved": True})
        assert result["maximum_gui_gap_seconds"] < 0.25, result
    finally:
        slicer.util.arrayFromSegmentBinaryLabelmap = original
        heartbeat.stop()
        peer.leave(joined["id"])
        controller.leave()
    return result


def run():
    widget = slicer.modules.livesegmentation.widgetRepresentation().self()
    KEEP_ALIVE.append(widget)
    volume = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "ROI geometry reference")
    slicer.util.updateVolumeFromArray(volume, np.zeros((40, 48, 56), np.uint8))
    node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "ROI probe")
    node.CreateDefaultDisplayNodes()
    node.SetReferenceImageGeometryParameterFromVolumeNode(volume)
    roi = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "ROI cropped reference")
    reference_matrix = vtk.vtkMatrix4x4()
    volume.GetIJKToRASMatrix(reference_matrix)
    roi.SetIJKToRASMatrix(reference_matrix)
    origin = reference_matrix.MultiplyPoint([13, 11, 7, 1])
    roi.SetOrigin(*origin[:3])
    data = np.zeros((9, 10, 11), np.uint8)
    data[1:5, 2:6, 3:7] = 1
    slicer.util.updateVolumeFromArray(roi, data)
    sid = node.GetSegmentation().AddEmptySegment("ROI", "ROI", [1, 0, 0])
    slicer.util.updateSegmentBinaryLabelmapFromArray(data, node, sid, roi)
    expected = slicer.util.arrayFromSegmentBinaryLabelmap(node, sid, volume)
    assert expected.sum() == data.sum()
    result = {"module_path": slicer.util.modulePath("LiveSegmentation")}
    original = slicer.util.arrayFromSegmentBinaryLabelmap

    def forbidden(*args, **kwargs):
        raise AssertionError("Full reference-volume mask requested in live ROI path")

    slicer.util.arrayFromSegmentBinaryLabelmap = forbidden
    try:
        bounds = [0, 40, 0, 48, 0, 56]
        actual = widget.segment_mask_region_in_reference_geometry(node, sid, volume, bounds)
        assert actual is not None, "ROI offset selects the full-volume fallback"
        assert np.array_equal(actual, expected)
        crop, box = widget.segment_mask_crop_in_reference_geometry(node, sid, volume)
        rebuilt = np.zeros(expected.shape, np.uint8)
        z0, z1, y0, y1, x0, x1 = box
        rebuilt[z0:z1, y0:y1, x0:x1] = crop
        assert np.array_equal(rebuilt, expected)
        capture = widget.capture_segment_mask_steps(node, sid, volume)
        yields = 0
        while True:
            try:
                next(capture)
                yields += 1
            except StopIteration as stopped:
                captured, captured_box = stopped.value
                break
        assert captured_box == box and np.array_equal(captured, crop)
        result["roi_offset_read_and_capture"] = True
        result["capture_yields"] = yields
    finally:
        slicer.util.arrayFromSegmentBinaryLabelmap = original
    affine_results = []
    for angle, scales, parent, shift in [(0.31, (1., 1., 1.), False, (13, 11, 7)),
                                       (0., (1.3, 0.8, 1.1), False, (13, 11, 7)),
                                       (0., (1., 1., 1.), True, (13, 11, 7)),
                                       (0., (1., 1., 1.), False, (13.5, 11.5, 7.5)),
                                       (0., (-1., 1., 1.), False, (30, 11, 7)),
                                       (0., (0.5, 0.5, 0.5), False, (13, 11, 7))]:
        other = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Affine ROI")
        if parent:
            transform = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLinearTransformNode")
            transform_matrix = vtk.vtkMatrix4x4()
            transform_matrix.SetElement(0, 3, 2)
            transform_matrix.SetElement(1, 3, -3)
            transform.SetMatrixTransformToParent(transform_matrix)
            other.SetAndObserveTransformNodeID(transform.GetID())
        affine = np.eye(4)
        c, s = np.cos(angle), np.sin(angle)
        affine[:3, :3] = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]]) @ np.diag(scales)
        affine[:3, 3] = shift
        vtk_affine = vtk.vtkMatrix4x4()
        for r in range(4):
            for col in range(4):
                vtk_affine.SetElement(r, col, float(affine[r, col]))
        native = vtk.vtkMatrix4x4()
        vtk.vtkMatrix4x4.Multiply4x4(reference_matrix, vtk_affine, native)
        roi.SetIJKToRASMatrix(native)
        other_sid = other.GetSegmentation().AddEmptySegment("Affine", "Affine")
        slicer.util.updateSegmentBinaryLabelmapFromArray(data, other, other_sid, roi)
        expected = original(other, other_sid, volume)
        slicer.util.arrayFromSegmentBinaryLabelmap = forbidden
        try:
            actual = widget.segment_mask_region_in_reference_geometry(other, other_sid, volume, bounds)
            assert np.array_equal(actual, expected), f"Affine differs at {np.count_nonzero(actual != expected)} voxels"
            crop, box = widget.segment_mask_crop_in_reference_geometry(other, other_sid, volume)
            rebuilt = np.zeros_like(expected)
            z0, z1, y0, y1, x0, x1 = box
            rebuilt[z0:z1, y0:y1, x0:x1] = crop
            assert np.array_equal(rebuilt, expected)
        finally:
            slicer.util.arrayFromSegmentBinaryLabelmap = original
        affine_results.append({"angle": angle, "scales": scales, "parent_transform": parent, "exact_match": True})
    result["affine_cases"] = affine_results
    # A logically huge reference with no scalar allocation catches accidental
    # source-sized reads without allocating hundreds of GiB in the test itself.
    virtual = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "Virtual 8192 reference")
    virtual.SetHideFromEditors(True)
    virtual_image = vtk.vtkImageData()
    virtual_image.SetExtent(0, 8191, 0, 8191, 0, 8191)
    virtual.SetAndObserveImageData(virtual_image)
    virtual_roi = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "Distant ROI")
    virtual_roi.SetOrigin(4000, 3000, 2000)
    slicer.util.updateVolumeFromArray(virtual_roi, data)
    virtual_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode")
    virtual_sid = virtual_node.GetSegmentation().AddEmptySegment("Distant", "Distant")
    slicer.util.updateSegmentBinaryLabelmapFromArray(data, virtual_node, virtual_sid, virtual_roi)
    slicer.util.arrayFromSegmentBinaryLabelmap = forbidden
    try:
        crop, box = widget.segment_mask_crop_in_reference_geometry(virtual_node, virtual_sid, virtual)
        assert box == [2001, 2005, 3002, 3006, 4003, 4007] and np.all(crop)
        result["virtual_reference"] = {"shape": [8192]*3, "allocated_source_scalars": False,
                                       "captured_bytes": int(crop.nbytes), "bounds": box}
    finally:
        slicer.util.arrayFromSegmentBinaryLabelmap = original
    # Packed labels may share one scalar buffer. Reading one label must neither
    # include nor modify a different label value in that same representation.
    from vtk.util import numpy_support
    binary = "Binary labelmap"
    image = virtual_node.GetSegmentation().GetSegment(virtual_sid).GetRepresentation(binary)
    scalars = numpy_support.vtk_to_numpy(image.GetPointData().GetScalars())
    scalars[0] = 7
    image.GetPointData().GetScalars().Modified()
    sibling = virtual_node.GetSegmentation().AddEmptySegment("Sibling", "Sibling")
    sibling_segment = virtual_node.GetSegmentation().GetSegment(sibling)
    sibling_segment.SetLabelValue(7)
    sibling_segment.AddRepresentation(binary, image)
    snapshot = scalars.copy()
    first = widget.segment_mask_region_in_reference_geometry(virtual_node, virtual_sid, virtual, [2000, 2009, 3000, 3010, 4000, 4011])
    second = widget.segment_mask_region_in_reference_geometry(virtual_node, sibling, virtual, [2000, 2009, 3000, 3010, 4000, 4011])
    assert not np.any(first & second) and first.sum() == 64 and second.sum() == 1
    assert np.array_equal(snapshot, scalars)
    result["packed_labels_preserved"] = True
    capture = widget.capture_segment_mask_steps(virtual_node, virtual_sid, virtual)
    next(capture)
    image.GetPointData().GetScalars().Modified()
    try:
        list(capture)
    except RuntimeError as exc:
        assert "changed during mask capture" in str(exc)
    else:
        raise AssertionError("Capture accepted a concurrently modified ROI")
    result["capture_revision_guard"] = True
    grid_image = vtk.vtkImageData()
    grid_image.SetDimensions(3, 3, 3)
    grid_image.AllocateScalars(vtk.VTK_DOUBLE, 3)
    numpy_support.vtk_to_numpy(grid_image.GetPointData().GetScalars())[:] = 0
    grid = vtk.vtkGridTransform()
    grid.SetDisplacementGridData(grid_image)
    non_linear = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLTransformNode")
    non_linear.SetAndObserveTransformToParent(grid)
    virtual_node.SetAndObserveTransformNodeID(non_linear.GetID())
    try:
        widget.segment_mask_region_in_reference_geometry(virtual_node, virtual_sid, virtual, [2000, 2009, 3000, 3010, 4000, 4011])
    except RuntimeError as exc:
        assert "non-linear" in str(exc)
    else:
        raise AssertionError("Non-linear relative geometry was silently accepted")
    result["nonlinear_rejected_without_export"] = True
    if os.environ.get("LIVE_ROI_LARGE", "1") == "1":
        result["large_data"] = large_probe(widget)
    result["ok"] = True
    return result


def main():
    start = time.monotonic()
    try:
        result = run()
    except Exception:
        result = {"ok": False, "traceback": traceback.format_exc()}
    result["seconds"] = time.monotonic() - start
    Path(os.environ["LIVE_ROI_OUTPUT"]).write_text(json.dumps(result, indent=2), encoding="utf-8")
    slicer.app.exit(0 if result["ok"] else 1)


main()
