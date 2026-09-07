"""Exercise Linux-folder UI and actual encrypted transport inside Slicer."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import traceback
from pathlib import Path

import numpy as np
import qt
import slicer


def wait_until(predicate, timeout=25):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        slicer.app.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    raise TimeoutError("SSH Slicer test did not converge")


def probe():
    from LiveSegmentationLib.collaboration import encode_mask_crop_snapshot
    from LiveSegmentationLib.ssh_transport import SshRoomClient

    root = Path(__file__).resolve().parents[1]
    folder = Path(tempfile.mkdtemp(prefix="live-slicer-ssh-"))
    fixture_python = os.environ.get("LIVE_SSH_TEST_PYTHON") or str(root / ".venv" / "Scripts" / "python.exe")
    fixture = subprocess.Popen([fixture_python, str(root / "scripts" / "run_ssh_test_server.py"), str(folder)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
    settings = qt.QSettings()
    saved = {str(key): settings.value(key) for key in settings.allKeys() if str(key).startswith("LiveSegmentation/")}
    widget = slicer.modules.livesegmentation.widgetRepresentation().self()
    controller = widget.live_collaboration
    peer = None
    try:
        connection = json.loads(fixture.stdout.readline())
        assert not controller._text(controller.shared_folder_edit), "Startup restored a connection target"
        controller.shared_folder_edit.setEditText("linux-host/dev/shm/team")
        assert controller._transport_mode() == "ssh-folder"
        assert not controller.ssh_settings.isHidden()
        assert "Temporary RAM" in controller.ssh_storage_warning.text
        controller.transport_combo.setCurrentIndex(2)
        controller.server_edit.setText("linux-host.example.org/dev/shm/team")
        controller._detect_ssh_server_address()
        assert controller._transport_mode() == "ssh-folder"
        assert controller._text(controller.shared_folder_edit) == "linux-host.example.org/dev/shm/team"
        controller.shared_folder_edit.setEditText(connection["location"])
        controller.ssh_user_edit.setText(connection["username"])
        controller.ssh_password_edit.setText(connection["password"])
        controller._ssh_known_hosts_path = lambda: Path(connection["known_hosts"])
        controller.user_edit.setText("slicer-ssh-alice")
        controller.room_edit.setText("encrypted-slicer-room")
        volume = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "SSH synthetic volume")
        slicer.util.updateVolumeFromArray(volume, np.zeros((64, 64, 64), dtype=np.uint8))
        widget.source_volume_selector.setCurrentNode(volume)
        start = time.monotonic()
        controller.join()
        join_return = time.monotonic() - start
        assert join_return < 0.25
        wait_until(lambda: controller.connected and controller.initial_sync_complete)
        assert not controller._text(controller.ssh_password_edit)
        assert "encrypted SSH" in controller._live_status_text()
        assert controller.backup_group.isHidden()
        peer = SshRoomClient(connection["location"], "ssh-bob", connection["username"], connection["password"], known_hosts=connection["known_hosts"])
        room = peer.join("encrypted-slicer-room", controller.source_volume_signature)
        operation = {"client_operation_id": "ssh-smoke-paint", "segment_id": "LiveSeg-Mandibles", "segment_name": "Mandibles", "color_hex": "#FF0000", "base_sequence": 0, **encode_mask_crop_snapshot(np.ones((2, 2, 2), dtype=np.uint8), [4, 6, 5, 7, 6, 8], [64, 64, 64])}
        start = time.monotonic()
        result = peer.push_operation(room["id"], operation)
        wait_until(lambda: controller.last_sequence >= result["sequence"])
        latency = time.monotonic() - start
        node = controller._segmentation_node()
        mask = widget.segment_mask_region_in_reference_geometry(node, "LiveSeg-Mandibles", volume, [4, 6, 5, 7, 6, 8])
        assert int(mask.sum()) == 8
        peer.send_chat(room["id"], "hello from SSH", "ssh-smoke-chat")
        wait_until(lambda: controller.last_chat_sequence >= 1)
        before = widget.segment_mask_region_in_reference_geometry(node, "LiveSeg-Mandibles", volume, [4, 6, 5, 7, 6, 8])
        after = before.copy()
        after[0, 0, 0] = 0
        old_sequence = controller.last_sequence
        assert widget.update_segment_binary_labelmap_crop(before, after, [4, 6, 5, 7, 6, 8], node, "LiveSeg-Mandibles", volume)
        wait_until(lambda: controller.last_sequence > old_sequence and not controller.outgoing and not controller.awaiting_echo)
        assert peer.operations(room["id"], result["sequence"])
        old_node_id = node.GetID()
        start = time.monotonic()
        controller.leave()
        leave_return = time.monotonic() - start
        assert leave_return < 0.25
        assert slicer.mrmlScene.GetNodeByID(old_node_id) is None
        controller.ssh_password_edit.setText(connection["password"])
        controller.join()
        wait_until(lambda: controller.connected and controller.initial_sync_complete)
        mask = widget.segment_mask_region_in_reference_geometry(controller._segmentation_node(), "LiveSeg-Mandibles", volume, [4, 6, 5, 7, 6, 8])
        assert int(mask.sum()) == 7
        return {"ok": True, "module_path": slicer.util.modulePath("LiveSegmentation"), "join_return_seconds": join_return, "remote_paint_seconds": latency, "leave_return_seconds": leave_return, "rejoined_voxels": 7, "auto_detect_shared_and_server_fields": True, "chat": True, "password_cleared": True}
    finally:
        controller.cleanup()
        if peer:
            peer.close()
        slicer.mrmlScene.Clear(0)
        fixture.stdin.write(b"stop\n")
        fixture.stdin.flush()
        try:
            fixture.wait(timeout=10)
        except subprocess.TimeoutExpired:
            fixture.terminate()
        for key in list(settings.allKeys()):
            if str(key).startswith("LiveSegmentation/"):
                settings.remove(key)
        for key, value in saved.items():
            settings.setValue(key, value)
        settings.sync()


def main():
    try:
        result = probe()
    except Exception:
        result = {"ok": False, "traceback": traceback.format_exc()}
    Path(os.environ["LIVE_SSH_SMOKE_OUTPUT"]).write_text(json.dumps(result, indent=2), encoding="utf-8")
    slicer.app.exit(0 if result["ok"] else 1)


qt.QTimer.singleShot(1000, main)
