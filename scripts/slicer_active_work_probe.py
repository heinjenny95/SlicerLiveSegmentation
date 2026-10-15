"""Native controls/input/IR integration for local timing, without model inference."""

from __future__ import annotations

import json
import os
import time
import traceback
from pathlib import Path

import numpy as np
import qt
import slicer

_WIDGETS = []


def wait_for(predicate, timeout=40):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        slicer.app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise RuntimeError("Active-work probe condition did not complete")


def pump(seconds):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        slicer.app.processEvents()
        time.sleep(0.01)


def run():
    result = {"ok": False}
    try:
        from LiveSegmentationLib import collaboration as core
        from LiveSegmentationLib.active_work import nninteractive_busy
        from LiveSegmentationLib.work_log import read_work_report

        if not slicer.app.testingEnabled():
            raise RuntimeError("Use isolated --testing settings")
        root = Path(os.environ["ACTIVE_WORK_PROBE_FOLDER"])
        original_history = core.location_history_settings
        core.location_history_settings = lambda: original_history(str(root / "settings"))
        volume = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "Synthetic timing volume")
        slicer.util.updateVolumeFromArray(volume, np.zeros((16, 16, 16), np.uint8))
        slicer.util.selectModule("LiveSegmentation")
        widget = slicer.modules.livesegmentation.widgetRepresentation().self()
        _WIDGETS.append(widget)
        live = widget.live_collaboration
        widget.source_volume_selector.setCurrentNode(volume)
        widget.segmentation_selector.setCurrentNode(None)
        live.user_edit.setText("Local researcher")
        live.room_edit.setText("active-study-probe")
        live.shared_folder_edit.setEditText(str(root))
        live.join()
        wait_for(lambda: live.connected and live.initial_sync_complete)
        ui = live.work_time
        real_context = ui._context

        def focused_context():
            context = real_context()
            context["foreground"] = True  # Isolated hidden test: don't steal user focus.
            return context

        ui._context = focused_context
        clock = ui.clock
        ui.idle_spin.setValue(15)
        clock.last_input = time.monotonic() - 16
        ui.tick()
        assert clock.category == "idle"
        assert ui.button.text.startswith("Auto-paused")
        assert ui.pause_action.text == "Resume"
        view = slicer.app.layoutManager().sliceWidget("Red").sliceView()
        event = qt.QMouseEvent(qt.QEvent.MouseButtonPress, qt.QPointF(4, 4),
                              qt.Qt.LeftButton, qt.Qt.LeftButton, qt.Qt.NoModifier)
        qt.QApplication.sendEvent(view, event)
        qt.QApplication.sendEvent(view, qt.QMouseEvent(qt.QEvent.MouseButtonRelease, qt.QPointF(4, 4),
                                                     qt.Qt.LeftButton, qt.Qt.NoButton, qt.Qt.NoModifier))
        ui.tick()
        assert clock.category == "work", "Native local view input did not resume timing"

        room_path = live.client._room_path
        metadata = json.loads(room_path.joinpath("room.json").read_text())
        peer = core.SharedFolderRoomClient(root, "Remote researcher")
        peer.join("active-study-probe", metadata["volume_signature"])
        clock.last_input = time.monotonic() - 16
        ui.tick()
        peer.send_chat(live.room_id, "remote input must not resume local timer", "active-probe-chat")
        wait_for(lambda: "remote input must not resume" in live.chat_history.toPlainText())
        assert clock.category == "idle"
        empty = np.zeros((16, 16, 16), np.uint8)
        painted = empty.copy()
        painted[3, 3, 3] = 1
        operation = {"client_operation_id": "peer-paint-active-clock", "segment_id": "2.25.123456789987654321",
                     "segment_name": "Peer material", "color_hex": "#0000FF", "base_sequence": 0,
                     "metadata_update": True, **core.encode_mask_delta(empty, painted)}
        peer.push_operation(live.room_id, operation)
        wait_for(lambda: live._segmentation_node().GetSegmentation().GetSegment(operation["segment_id"]) is not None)
        assert clock.category == "idle", "Remote painting incorrectly resumed local timing"

        ui.review_action.trigger()
        clock.last_input = time.monotonic() - 120
        before = clock.totals["review"]
        pump(0.8)
        assert clock.totals["review"] > before + 0.5
        ui.pause_action.trigger()
        active = clock.active_seconds
        pump(0.6)
        assert abs(clock.active_seconds - active) < 0.01
        assert ui.pause_action.text == "Resume"
        ui.pause_action.trigger()

        slicer.util.selectModule("SlicerNNInteractiveIR")
        ir = slicer.modules.slicernninteractiveir.widgetRepresentation().self()
        _WIDGETS.append(ir)
        ir.ui.targetSegmentationSelector.setCurrentNode(live._segmentation_node())
        assert vars(slicer.modules).get("SlicerNNInteractiveIRWidget") is ir
        assert not nninteractive_busy(slicer.modules)
        ui.tick()
        before_wait = clock.totals["technical_wait"]
        ir._run_session_call(lambda: time.sleep(1.2), message="", timeout_seconds=5)
        ui.tick()
        assert clock.totals["technical_wait"] > before_wait + 0.5
        ui.review_action.trigger()
        before_review, before_wait = clock.totals["review"], clock.totals["technical_wait"]
        ir._run_session_call(lambda: time.sleep(1.2), message="", timeout_seconds=5)
        ui.tick()
        assert clock.totals["review"] > before_review + 0.5
        assert clock.totals["technical_wait"] - before_wait < 0.3
        image_path = Path(os.environ["ACTIVE_WORK_PROBE_OUTPUT"]).with_suffix(".png")
        widget.navigation_toolbar.grab().save(str(image_path))
        peer.leave(live.room_id)
        local_path = ui.local_log.room_path
        live.leave()

        def final_report():
            path = room_path / "work-log.txt"
            if not path.is_file():
                return False
            text = read_work_report(path)
            return ('ACTIVE PERSON "Local researcher"' in text and '"expected_samples"' in text
                    and "Missing local timing samples" not in text
                    and "Local timing end unconfirmed for 'Local researcher'" not in text)

        wait_for(final_report)
        assert local_path.joinpath("work-log.txt").is_file()
        assert not ui._observing and not ui.timer.isActive()
        result.update(ok=True, module_path=slicer.modules.livesegmentation.path,
                      local_input_resumes=True, remote_chat_does_not_resume=True,
                      remote_paint_does_not_resume=True,
                      review_survives_idle_threshold=True, manual_pause_stops_timer=True,
                      actual_ir_worker_wait_detected=True, simultaneous_review_not_subtracted=True,
                      local_backup=str(local_path), room_log=str(room_path / "work-log.txt"),
                      final_sample_coverage_confirmed=True, toolbar_image=str(image_path))
        ir.cleanup()
        widget.cleanup()
    except Exception as exc:
        result.update(error=str(exc), traceback=traceback.format_exc())
    Path(os.environ["ACTIVE_WORK_PROBE_OUTPUT"]).write_text(json.dumps(result, indent=2), encoding="utf-8")
    qt.QTimer.singleShot(200, lambda: slicer.app.exit(0 if result["ok"] else 1))


run()
