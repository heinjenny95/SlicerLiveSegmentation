"""Real-Slicer smoke test for the non-mutating two-computer preflight UI."""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import traceback
from pathlib import Path

import numpy as np
import qt
import slicer

RESULT_PATH = Path(__file__).with_suffix(".result.json")


def finish(result, exit_code):
    RESULT_PATH.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("LIVESEG_PREFLIGHT_RESULT=" + json.dumps(result), flush=True)
    slicer.app.exit(int(exit_code))


def run():
    temporary_root = None
    try:
        slicer.util.selectModule("LiveSegmentation")
        slicer.app.processEvents()
        representation = slicer.modules.livesegmentation.widgetRepresentation()
        widget = representation.self()
        controller = widget.live_collaboration

        volume = slicer.mrmlScene.AddNewNodeByClass(
            "vtkMRMLScalarVolumeNode", "Preflight source"
        )
        slicer.util.updateVolumeFromArray(
            volume, np.arange(12 * 13 * 14, dtype=np.uint16).reshape((12, 13, 14))
        )
        widget.source_volume_selector.setCurrentNode(volume)
        temporary_root = Path(tempfile.mkdtemp(prefix="liveseg-preflight-"))
        controller.user_edit.setText("Alice")
        controller.room_edit.setText("two-computer-smoke")
        controller.transport_combo.setCurrentIndex(0)
        controller.shared_folder_edit.setEditText(str(temporary_root))
        signature = controller._current_volume_signature()

        from LiveSegmentationLib.collaboration import SharedFolderRoomClient

        bob = SharedFolderRoomClient(temporary_root, "Bob")
        bob.preflight("two-computer-smoke", signature)
        controller.run_connection_preflight()
        deadline = time.monotonic() + 12.0

        def poll():
            if controller._preflight_running and time.monotonic() < deadline:
                qt.QTimer.singleShot(50, poll)
                return
            text = controller.preflight_text.toPlainText
            text = text() if callable(text) else str(text)
            room_path = (
                temporary_root
                / "LiveSegmentation"
                / "rooms"
                / SharedFolderRoomClient._room_key("two-computer-smoke")
            )
            result = {
                "ok": (
                    not controller._preflight_running
                    and "READY — shared-folder" in text
                    and "[PASS] Second computer" in text
                    and not room_path.exists()
                    and not slicer.util.getNodesByClass("vtkMRMLSegmentationNode")
                ),
                "report": text,
                "room_created": room_path.exists(),
                "segmentation_count": len(
                    slicer.util.getNodesByClass("vtkMRMLSegmentationNode")
                ),
            }
            shutil.rmtree(temporary_root, ignore_errors=True)
            finish(result, 0 if result["ok"] else 2)

        poll()
    except Exception as exc:
        if temporary_root is not None:
            shutil.rmtree(temporary_root, ignore_errors=True)
        finish(
            {"ok": False, "error": str(exc), "traceback": traceback.format_exc()},
            1,
        )


qt.QTimer.singleShot(1200, run)

