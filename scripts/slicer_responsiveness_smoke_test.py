"""Exercise remote bursts and stalled storage while measuring real Qt heartbeats."""

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


def pump_until(predicate, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        slicer.app.processEvents()
        if predicate():
            return
        time.sleep(0.002)
    raise TimeoutError("Responsiveness probe did not converge")


def run_probe():
    from LiveSegmentationLib.collaboration import (
        SharedFolderRoomClient,
        encode_mask_crop_snapshot,
        encode_mask_delta,
    )

    widget = slicer.modules.livesegmentation.widgetRepresentation().self()
    controller = widget.live_collaboration
    settings = qt.QSettings()
    prefix = controller.SETTINGS_PREFIX
    saved = {str(key): settings.value(key) for key in settings.allKeys() if str(key).startswith(prefix)}
    result = {}
    heartbeats = []
    heartbeat = qt.QTimer()
    heartbeat.setInterval(10)
    heartbeat.connect("timeout()", lambda: heartbeats.append(time.monotonic()))
    try:
        size = int(os.environ.get("LIVE_RESPONSIVENESS_SIZE", "384"))
        shape = (size, size, size)
        volume = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "Responsiveness synthetic volume")
        slicer.util.updateVolumeFromArray(volume, np.zeros(shape, dtype=np.uint8))
        widget.source_volume_selector.setCurrentNode(volume)
        room_name = "responsiveness-" + uuid.uuid4().hex
        folder = tempfile.mkdtemp(prefix="live-responsiveness-")
        controller.transport_combo.setCurrentIndex(0)
        controller.shared_folder_edit.setEditText(folder)
        controller.room_edit.setText(room_name)
        controller.user_edit.setText("responsive-observer")
        controller.join()
        pump_until(lambda: controller.connected and controller.initial_sync_complete)
        peer = SharedFolderRoomClient(folder, "remote-painter")
        room = peer.join(room_name, controller.source_volume_signature)
        sequence = 0

        def send(segment_id, encoded):
            nonlocal sequence
            operation = peer.push_operation(room["id"], {
                "client_operation_id": str(uuid.uuid4()),
                "segment_id": segment_id,
                "segment_name": segment_id,
                "color_hex": "#E53B32" if segment_id == "Red" else "#2878DA",
                "base_sequence": sequence,
                **encoded,
            })
            sequence = int(operation["sequence"])

        # Two distant points force a microscopy-sized internal labelmap while
        # each following brush edit is small. This catches full-extent merges.
        seed = np.zeros(shape, dtype=np.uint8)
        seed[1, 1, 1] = 1
        seed[-2, -2, -2] = 1
        send("Red", encode_mask_crop_snapshot(seed, [0, size, 0, size, 0, size], shape))
        pump_until(lambda: controller.last_sequence >= sequence, timeout=60.0)
        del seed
        widget.open_segment_editor()
        slicer.app.layoutManager().setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutOneUpRedSliceView)
        slicer.util.setSliceViewerLayers(background=volume)
        slicer.util.resetSliceViews()
        pump_until(lambda: not controller._incoming_operations)
        heartbeat.start()
        start = time.monotonic()
        errors = []

        def burst():
            try:
                for index in range(24):
                    patch = np.ones((1, 8, 8), dtype=np.uint8)
                    encoded = encode_mask_delta(np.zeros_like(patch), patch)
                    x = 20 + index * 8
                    encoded["volume_shape"] = list(shape)
                    encoded["voxel_bbox"] = [size // 2, size // 2 + 1, 40, 48, x, x + 8]
                    send("Red", encoded)
                    time.sleep(0.01)
            except Exception:
                errors.append(traceback.format_exc())

        publisher = threading.Thread(target=burst, daemon=True)
        publisher.start()
        pump_until(lambda: not publisher.is_alive() and controller.last_sequence >= sequence, timeout=60.0)
        if errors:
            raise RuntimeError(errors[0])
        duration = time.monotonic() - start
        gaps = np.diff([start, *heartbeats, time.monotonic()])
        result["brush_burst"] = {
            "operations": 24,
            "seconds": round(duration, 4),
            "heartbeat_count": len(heartbeats),
            "maximum_gui_gap_seconds": round(float(gaps.max()), 4),
            "p95_gui_gap_seconds": round(float(np.percentile(gaps, 95)), 4),
        }
        expected = widget.segment_mask_region_in_reference_geometry(
            controller._segmentation_node(), "Red", volume,
            [size // 2, size // 2 + 1, 40, 48, 20, 20 + 24 * 8],
        )
        assert np.count_nonzero(expected) == 24 * 64, "A brush component was lost"

        # An inference-sized remote mask must be split across Qt turns too.
        # Its foreground takes ownership from the existing red label.
        heartbeats.clear()
        start = time.monotonic()
        dense = np.ones((96, 96, 96), dtype=np.uint8)
        dense_bounds = [size // 2 - 32, size // 2 + 64, 32, 128, 32, 128]
        dense_encoded = encode_mask_crop_snapshot(dense, dense_bounds, shape)
        publisher = threading.Thread(target=lambda: send("Blue", dense_encoded), daemon=True)
        publisher.start()
        pump_until(lambda: not publisher.is_alive() and controller.last_sequence >= sequence, timeout=60.0)
        gaps = np.diff([start, *heartbeats, time.monotonic()])
        result["remote_dense_mask"] = {
            "voxels": int(dense.size),
            "seconds": round(time.monotonic() - start, 4),
            "maximum_gui_gap_seconds": round(float(gaps.max()), 4),
        }
        node = controller._segmentation_node()
        blue = widget.segment_mask_region_in_reference_geometry(node, "Blue", volume, dense_bounds)
        red = widget.segment_mask_region_in_reference_geometry(node, "Red", volume, dense_bounds)
        assert np.all(blue == 1) and not np.any(red), "Exclusive ownership failed during tiled inference"

        # Native per-label representation invalidation must not discard an
        # unrelated label's cached mesh during a small red brush update.
        surface_name = "Closed surface"
        blue_surface = vtk.vtkPolyData()
        node.GetSegmentation().GetSegment("Blue").AddRepresentation(surface_name, blue_surface)
        node.GetSegmentation().GetSegment("Red").AddRepresentation(surface_name, vtk.vtkPolyData())

        # Growing/capturing a large existing layer must not block the GUI when
        # the local person paints while the peer is sending work.
        heartbeats.clear()
        start = time.monotonic()
        sequence_before_local = controller.last_sequence
        region_bounds = [size // 2 + 1, size // 2 + 2, 200, 208, 200, 208]
        before = widget.segment_mask_region_in_reference_geometry(node, "Red", volume, region_bounds)
        assert widget.update_segment_binary_labelmap_crop(before, np.ones_like(before), region_bounds, node, "Red", volume)
        assert node.GetSegmentation().GetSegment("Blue").GetRepresentation(surface_name) == blue_surface
        assert node.GetSegmentation().GetSegment("Red").GetRepresentation(surface_name) is None
        pump_until(lambda: controller.last_sequence > sequence_before_local and not controller.outgoing and not controller.awaiting_echo and not controller._local_encode_pending, timeout=30.0)
        gaps = np.diff([start, *heartbeats, time.monotonic()])
        result["local_large_layer_edit"] = {
            "seconds": round(time.monotonic() - start, 4),
            "maximum_gui_gap_seconds": round(float(gaps.max()), 4),
            "voxels": 64,
        }
        # Rename and edit must respect a user's manually hidden segment.
        node.GetDisplayNode().SetSegmentVisibility("Red", False)
        encoded = encode_mask_delta(np.zeros((1, 2, 2), dtype=np.uint8), np.ones((1, 2, 2), dtype=np.uint8))
        encoded["voxel_bbox"] = [2, 3, 2, 4, 2, 4]
        encoded["volume_shape"] = list(shape)
        send("Red", encoded)
        pump_until(lambda: controller.last_sequence >= sequence)
        assert not node.GetDisplayNode().GetSegmentVisibility("Red"), "Remote edit unhid a hidden label"

        # Deliberately stall reads on the local receiver's transport, as happens
        # while an SMB host is saturated by another user's inference job.
        original_operations = controller.client.operations

        def slow_operations(*args, **kwargs):
            time.sleep(2.0)
            return original_operations(*args, **kwargs)

        controller.client.operations = slow_operations
        heartbeats.clear()
        start = time.monotonic()
        controller._force_sync_refresh = True
        pump_until(lambda: time.monotonic() - start >= 2.5, timeout=5.0)
        gaps = np.diff([start, *heartbeats, time.monotonic()])
        result["stalled_remote_storage"] = {
            "simulated_request_delay_seconds": 2.0,
            "heartbeat_count": len(heartbeats),
            "maximum_gui_gap_seconds": round(float(gaps.max()), 4),
        }
        controller.client.operations = original_operations
        result["ok"] = True
        result["module_path"] = slicer.util.modulePath("LiveSegmentation")
        result["source_shape"] = list(shape)
        result["metrics"] = controller.session_metrics.summary()
        assert result["stalled_remote_storage"]["maximum_gui_gap_seconds"] < 0.25
        assert result["brush_burst"]["maximum_gui_gap_seconds"] < 0.25
        assert result["remote_dense_mask"]["maximum_gui_gap_seconds"] < 0.25
        assert result["local_large_layer_edit"]["maximum_gui_gap_seconds"] < 0.25
        return result
    finally:
        heartbeat.stop()
        controller.cleanup()
        slicer.mrmlScene.Clear(0)
        for key in list(settings.allKeys()):
            if str(key).startswith(prefix):
                settings.remove(key)
        for key, value in saved.items():
            settings.setValue(key, value)
        settings.sync()


def main():
    try:
        result = run_probe()
    except Exception:
        result = {"ok": False, "traceback": traceback.format_exc()}
    Path(os.environ["LIVE_RESPONSIVENESS_OUTPUT"]).write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    slicer.app.exit(0 if result["ok"] else 1)


qt.QTimer.singleShot(1000, main)
