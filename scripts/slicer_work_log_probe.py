"""Native room/edit/leave regression for the persistent study log. No real data."""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from pathlib import Path

import numpy as np
import qt
import slicer

_WIDGETS = []


def wait_for(predicate, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        slicer.app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise RuntimeError("Study-log probe condition did not complete")


def run():
    result = {"ok": False}
    live = peer = None
    release = threading.Event()
    try:
        from LiveSegmentationLib import collaboration as core

        if not slicer.app.testingEnabled():
            raise RuntimeError("This probe needs isolated --testing settings")
        root = Path(os.environ["LIVE_WORK_LOG_PROBE_FOLDER"])
        root.mkdir(parents=True, exist_ok=True)
        original_history = core.location_history_settings
        core.location_history_settings = lambda: original_history(str(root / "settings"))
        slicer.util.selectModule("LiveSegmentation")
        widget = slicer.modules.livesegmentation.widgetRepresentation().self()
        _WIDGETS.append(widget)
        live = widget.live_collaboration
        volume = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "Synthetic study")
        slicer.util.updateVolumeFromArray(volume, np.zeros((16, 16, 16), np.uint8))
        widget.source_volume_selector.setCurrentNode(volume)
        widget.segmentation_selector.setCurrentNode(None)
        live.user_edit.setText("Person A")
        live.room_edit.setText("study-log-probe")
        live.shared_folder_edit.setEditText(str(root))
        live.backup_enabled_checkbox.checked = False
        live.join()
        wait_for(lambda: live.connected and live.initial_sync_complete)
        room_id = live.room_id
        room_path = live.client._room_path
        metadata = json.loads(room_path.joinpath("room.json").read_text())
        peer = core.SharedFolderRoomClient(root, "Person B")
        peer.join("study-log-probe", metadata["volume_signature"])
        peer.presence(room_id, {})
        wait_for(lambda: "Person B" in live.presence_by_user)
        node = live._segmentation_node()
        segment_id = node.GetSegmentation().AddEmptySegment("", "Mandible", (1, 0, 0))
        mask = np.zeros((16, 16, 16), np.uint8)
        mask[5, 5, 5] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(mask, node, segment_id, volume)
        wait_for(lambda: live.last_sequence > 0 and not live.outgoing and not live.awaiting_echo)
        wait_for(lambda: any(item.get("segment_name") == "Mandible"
                            for item in peer.operations(room_id, 0, limit=100)))

        # Hold only the logging worker. Qt, room chat and roster must progress.
        log = live.client.work_log
        original_persist = log._persist
        entered = threading.Event()

        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(8):
                raise RuntimeError("Test failed to release delayed study-log I/O")
            return original_persist(*args, **kwargs)

        log._persist = delayed
        log.submit("room.join", "probe-worker", "qt-delay-test")
        wait_for(entered.is_set)
        ticks = []
        timer = qt.QTimer()
        timer.setInterval(20)
        timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        timer.start()
        peer.send_chat(room_id, "probe message", "study-probe-message")
        wait_for(lambda: "probe message" in live.chat_history.toPlainText() and len(ticks) >= 6)
        timer.stop()
        release.set()
        log._persist = original_persist
        peer.leave(room_id)
        wait_for(lambda: "Person B" not in live.presence_by_user)
        live.leave()
        wait_for(lambda: not live.connected)
        # Poll from Qt; do not call the blocking flush barrier on the GUI thread.
        def both_closed():
            text = room_path.joinpath("work-log.txt").read_text(encoding="utf-8")
            return all(any(line.startswith(f'SESSION "{name}"') and "| closed" in line
                           for line in text.splitlines()) for name in ("Person A", "Person B"))

        wait_for(both_closed)
        text = room_path.joinpath("work-log.txt").read_text(encoding="utf-8")
        assert "Mandible" in text and "segmentation.operation" in text and "chat.message" in text
        result.update(ok=True, module_path=slicer.modules.livesegmentation.path,
                      txt_path=str(room_path / "work-log.txt"), qt_ticks_during_blocked_log=len(ticks),
                      real_mrml_edit_logged=True, peer_chat_while_log_blocked=True,
                      leave_and_presence_logged=True)
        widget.cleanup()
    except Exception as exc:
        result.update(error=str(exc), traceback=traceback.format_exc())
    finally:
        release.set()
    Path(os.environ["LIVE_WORK_LOG_PROBE_OUTPUT"]).write_text(json.dumps(result, indent=2), encoding="utf-8")
    qt.QTimer.singleShot(200, lambda: slicer.app.exit(0 if result["ok"] else 1))


run()
