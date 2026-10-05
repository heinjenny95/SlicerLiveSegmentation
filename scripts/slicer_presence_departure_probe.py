"""Native Qt roster regression using a Slicer controller and a real room peer.

Run in an isolated Slicer with LIVE_PRESENCE_PROBE_FOLDER and
LIVE_PRESENCE_PROBE_OUTPUT pointing at fresh local test locations.
"""

from __future__ import annotations

import inspect
import json
import os
import time
import traceback
import uuid
from pathlib import Path

import numpy as np
import slicer


def wait_for(predicate, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        slicer.app.processEvents()
        if predicate():
            return
        time.sleep(0.02)
    raise RuntimeError("Native presence condition did not become true")


def run_probe():
    from LiveSegmentationLib.collaboration import SharedFolderRoomClient, _write_presence_json

    controller = None
    peer = None
    try:
        if not slicer.app.testingEnabled():
            raise RuntimeError("This probe requires isolated --testing settings")
        factory = slicer.app.moduleManager().factoryManager()
        if not factory.isLoaded("LiveSegmentation"):
            if not factory.loadModules(["LiveSegmentation"]):
                raise RuntimeError("Could not load the LiveSegmentation test module")
        slicer.util.selectModule("LiveSegmentation")
        module = slicer.app.moduleManager().module("LiveSegmentation")
        if module is None:
            raise RuntimeError("LiveSegmentation module is not registered")
        widget = module.widgetRepresentation().self()
        controller = widget.live_collaboration
        volume = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "Presence test volume")
        slicer.util.updateVolumeFromArray(volume, np.zeros((16, 16, 16), np.int16))
        widget.source_volume_selector.setCurrentNode(volume)
        widget.segmentation_selector.setCurrentNode(None)
        folder = os.environ["LIVE_PRESENCE_PROBE_FOLDER"]
        room_name = "presence-native-" + uuid.uuid4().hex
        controller.transport_combo.setCurrentIndex(0)
        controller.shared_folder_edit.setEditText(folder)
        controller.user_edit.setText("JennyA")
        controller.room_edit.setText(room_name)
        controller.backup_enabled_checkbox.checked = False
        controller.join()
        wait_for(lambda: controller.connected and controller.initial_sync_complete)
        metadata = json.loads(controller.client._room_path.joinpath("room.json").read_text())
        peer = SharedFolderRoomClient(folder, "JennyB")
        peer.join(room_name, metadata["volume_signature"])
        peer.presence(controller.room_id, {})
        wait_for(lambda: "JennyB" in controller.presence_by_user)
        old_session = peer.presence_session_id
        peer_path = peer._presence_path(peer._room_path)
        stale_heartbeat = json.loads(peer_path.read_text())

        started = time.monotonic()
        peer.leave(controller.room_id)
        wait_for(lambda: "JennyB" not in controller.presence_by_user, timeout=5.0)
        departure_seconds = time.monotonic() - started
        if controller.users_label.text != "Nobody else is connected":
            raise RuntimeError("The native roster label still displays the departed peer")

        _write_presence_json(peer_path, stale_heartbeat)
        until = time.monotonic() + 2.0
        while time.monotonic() < until:
            slicer.app.processEvents()
            if "JennyB" in controller.presence_by_user:
                raise RuntimeError("Late heartbeat resurrected a closed session in the UI")
            time.sleep(0.02)

        peer.join(room_name, metadata["volume_signature"])
        if peer.presence_session_id == old_session:
            raise RuntimeError("Rejoin reused a closed presence session")
        peer.presence(controller.room_id, {})
        wait_for(lambda: controller.presence_by_user.get("JennyB", {}).get("presence_session_id") == peer.presence_session_id)
        started = time.monotonic()
        peer.leave(controller.room_id)
        wait_for(lambda: "JennyB" not in controller.presence_by_user, timeout=5.0)
        return {
            "ok": True,
            "module_path": str(Path(inspect.getfile(type(widget))).resolve()),
            "departure_detection_seconds": round(departure_seconds, 3),
            "second_departure_seconds": round(time.monotonic() - started, 3),
            "late_heartbeat_did_not_resurrect": True,
            "fresh_session_rejoined": True,
            "automatic_qt_polling_only": True,
            "roster_text": controller.users_label.text,
        }
    finally:
        if controller is not None:
            controller.cleanup()


def execute():
    try:
        result = run_probe()
    except Exception as exc:
        result = {"ok": False, "error": str(exc), "traceback": traceback.format_exc()}
    Path(os.environ["LIVE_PRESENCE_PROBE_OUTPUT"]).write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    slicer.app.exit(0 if result["ok"] else 1)


execute()
