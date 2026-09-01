from __future__ import annotations

import json
import os
import time
import traceback
import uuid
import zipfile
from pathlib import Path

import numpy as np
import qt
import slicer


def pump_events(seconds):
    deadline = time.time() + float(seconds)
    while time.time() < deadline:
        slicer.app.processEvents()
        time.sleep(0.03)


def controller_workers(controller):
    return tuple(
        getattr(controller, name, None)
        for name in (
            "_edit_push_worker",
            "_edit_pull_worker",
            "_chat_send_worker",
            "_chat_pull_worker",
            "_presence_worker",
            "_lock_set_worker",
            "_lock_pull_worker",
            "_maintenance_worker",
        )
    )


def run_probe():
    widget = None
    controller = None
    settings = qt.QSettings()
    settings_snapshot = {}
    try:
        keys = (
            "LiveSegmentation/collaboration/user",
            "LiveSegmentation/collaboration/room",
            "LiveSegmentation/collaboration/transport",
            "LiveSegmentation/collaboration/sharedFolder",
            "LiveSegmentation/collaboration/server",
        )
        for key in keys:
            settings_snapshot[key] = (
                bool(settings.contains(key)),
                settings.value(key),
            )

        volume = slicer.mrmlScene.AddNewNodeByClass(
            "vtkMRMLScalarVolumeNode", "Live Segmentation smoke volume"
        )
        image = np.arange(12 * 11 * 10, dtype=np.int16).reshape((12, 11, 10))
        slicer.util.updateVolumeFromArray(volume, image)

        module = slicer.app.moduleManager().module("LiveSegmentation")
        if module is None:
            raise RuntimeError("LiveSegmentation module is not registered")
        representation = module.widgetRepresentation()
        widget = representation.self()
        controller = widget.live_collaboration
        if controller is None or not hasattr(controller, "group"):
            raise RuntimeError("Live collaboration UI was not created")
        controller.backup_enabled_checkbox.checked = False

        widget.source_volume_selector.setCurrentNode(volume)
        mode = os.environ.get("LIVE_SEGMENTATION_SMOKE_MODE", "produce").strip().lower()
        transport = os.environ.get(
            "LIVE_SEGMENTATION_SMOKE_TRANSPORT", "shared-folder"
        ).strip().lower()
        room_name = os.environ.get("LIVE_SEGMENTATION_SMOKE_ROOM") or (
            f"slicer-smoke-{uuid.uuid4()}"
        )
        user_name = os.environ.get("LIVE_SEGMENTATION_SMOKE_USER") or "slicer-smoke"
        if transport == "shared-folder":
            shared_folder = os.environ.get("LIVE_SEGMENTATION_SMOKE_SHARED_FOLDER")
            if not shared_folder:
                raise RuntimeError("LIVE_SEGMENTATION_SMOKE_SHARED_FOLDER is required")
            controller.transport_combo.setCurrentIndex(0)
            controller.shared_folder_edit.setText(shared_folder)
        elif transport == "server":
            controller.transport_combo.setCurrentIndex(1)
            controller.server_edit.setText("http://127.0.0.1:8000")
        else:
            raise RuntimeError(f"Unsupported smoke-test transport: {transport}")
        if mode == "produce":
            segmentation = widget.create_segmentation()
            segment_id = "TestSegment"
            segmentation.GetSegmentation().AddEmptySegment(segment_id, "Test segment")
            widget.segmentation_selector.setCurrentNode(segmentation)
        else:
            segmentation = None
            segment_id = "TestSegment"
            widget.segmentation_selector.setCurrentNode(None)
        controller.user_edit.setText(user_name)
        controller.room_edit.setText(room_name)
        controller.join()
        if not controller.connected:
            raise RuntimeError(controller._last_error or "Live room did not connect")
        segmentation = controller._segmentation_node()
        if segmentation is None:
            raise RuntimeError("The room did not provide a shared segmentation node")
        if widget.segmentation_selector.currentNode() != segmentation:
            raise RuntimeError("The room segmentation was not selected automatically")

        widget.open_segment_editor()
        editor = widget._standard_segment_editor_widget()
        if editor is None or editor.segmentationNode() != segmentation:
            raise RuntimeError("Standard Segment Editor did not receive the room node")

        operations = []
        if mode == "produce":
            wait_for_return_edit = (
                os.environ.get("LIVE_SEGMENTATION_SMOKE_WAIT_FOR_RETURN_EDIT", "0")
                == "1"
            )
            edit_delay = float(
                os.environ.get("LIVE_SEGMENTATION_SMOKE_EDIT_DELAY", "0") or 0
            )
            if edit_delay > 0:
                pump_events(edit_delay)
            mask = np.zeros(image.shape, dtype=np.uint8)
            mask[2:5, 3:6, 4:7] = 1
            widget.update_segment_binary_labelmap_from_array(
                mask, segmentation, segment_id, volume
            )

            deadline = time.time() + 8
            while time.time() < deadline:
                pump_events(0.15)
                operations = controller.client.operations(controller.room_id, 0)
                shared_mask = np.zeros_like(mask)
                from LiveSegmentationLib.collaboration import apply_mask_delta

                for item in operations:
                    if item["segment_id"] == segment_id:
                        shared_mask = apply_mask_delta(shared_mask, item)
                if int(shared_mask.sum()) == 27:
                    break
            if not operations or int(shared_mask.sum()) != 27:
                raise RuntimeError("Shared room did not receive the 27-voxel edit")
            voxel_count = int(mask.sum())
            if wait_for_return_edit:
                deadline = time.time() + 12
                while time.time() < deadline:
                    pump_events(0.15)
                    received = widget.segment_mask_in_reference_geometry(
                        segmentation, segment_id, volume, image.shape
                    )
                    voxel_count = int(np.asarray(received, dtype=np.uint8).sum())
                    operations = controller.client.operations(controller.room_id, 0)
                    if voxel_count == 35:
                        break
                if voxel_count != 35:
                    raise RuntimeError("Producer did not receive the peer's live edit")
        elif mode == "consume":
            return_edit = (
                os.environ.get("LIVE_SEGMENTATION_SMOKE_RETURN_EDIT", "0") == "1"
            )
            deadline = time.time() + 10
            voxel_count = 0
            received = None
            while time.time() < deadline:
                pump_events(0.15)
                operations = controller.client.operations(controller.room_id, 0)
                segment = segmentation.GetSegmentation().GetSegment(segment_id)
                if segment is not None:
                    received = widget.segment_mask_in_reference_geometry(
                        segmentation, segment_id, volume, image.shape
                    )
                    voxel_count = int(np.asarray(received, dtype=np.uint8).sum())
                if controller.initial_sync_complete and voxel_count == 27:
                    break
            if not operations:
                raise RuntimeError("No remote live operation was received")
            if voxel_count != 27:
                raise RuntimeError(
                    f"Expected 27 synchronized voxels, received {voxel_count}"
                )
            if return_edit:
                returned = np.asarray(received, dtype=np.uint8).copy()
                returned[6:8, 6:8, 6:8] = 1
                widget.update_segment_binary_labelmap_from_array(
                    returned, segmentation, segment_id, volume
                )
                deadline = time.time() + 8
                while time.time() < deadline:
                    pump_events(0.15)
                    operations = controller.client.operations(controller.room_id, 0)
                    shared_mask = np.zeros(image.shape, dtype=np.uint8)
                    from LiveSegmentationLib.collaboration import apply_mask_delta

                    for item in operations:
                        if item["segment_id"] == segment_id:
                            shared_mask = apply_mask_delta(shared_mask, item)
                    voxel_count = int(shared_mask.sum())
                    if voxel_count == 35:
                        break
                if voxel_count != 35:
                    raise RuntimeError("Room did not receive the peer's return edit")
        else:
            raise RuntimeError(f"Unsupported smoke-test mode: {mode}")

        collaboration_extras = None
        if os.environ.get("LIVE_SEGMENTATION_SMOKE_TEST_EXTRAS", "0") == "1":
            if str(controller._combo_current_data(controller.label_combo)) != segment_id:
                raise RuntimeError("Explicit label-management selector was not populated")
            if not controller.backup_enabled_checkbox.enabled:
                raise RuntimeError("Backup settings are disabled while the shared room is active")
            controller.backup_enabled_checkbox.checked = True
            if not controller.backup_interval_spin.enabled:
                raise RuntimeError("Backup interval did not become editable")
            controller.backup_enabled_checkbox.checked = False

            controller.chat_input.setText("Persistent controller chat test")
            controller.send_chat_message()
            if "Persistent controller chat test" not in controller.chat_history.toPlainText():
                raise RuntimeError("Own chat message was not displayed optimistically")
            if controller.chat_dock is None or controller.chat_dock_history is None:
                raise RuntimeError("Persistent chat dock was not created")
            if "Persistent controller chat test" not in controller.chat_dock_history.toPlainText():
                raise RuntimeError("Persistent chat dock did not mirror the room message")
            deadline = time.time() + 8
            chat_text = ""
            while time.time() < deadline:
                pump_events(0.15)
                chat_text = controller.chat_history.toPlainText()
                if "Persistent controller chat test" in chat_text:
                    break
            if "Persistent controller chat test" not in chat_text:
                raise RuntimeError("Controller chat message did not appear")

            controller.toggle_selected_segment_lock()
            deadline = time.time() + 8
            while time.time() < deadline:
                pump_events(0.15)
                if (controller.segment_locks_state.get(segment_id) or {}).get("locked"):
                    break
            if not (controller.segment_locks_state.get(segment_id) or {}).get("locked"):
                raise RuntimeError("Controller did not lock the selected label")
            controller.toggle_selected_segment_lock()
            deadline = time.time() + 8
            while time.time() < deadline:
                pump_events(0.15)
                state = controller.segment_locks_state.get(segment_id) or {}
                if not state.get("locked") and segment_id not in controller.pending_lock_changes:
                    break
            if (controller.segment_locks_state.get(segment_id) or {}).get("locked"):
                raise RuntimeError("Controller did not unlock the selected label")

            controller.refresh_now()
            deadline = time.time() + 8
            while time.time() < deadline:
                pump_events(0.15)
                workers = controller_workers(controller)
                if controller.connection_healthy and not any(
                    worker is not None and worker.is_alive() for worker in workers
                ):
                    break
            if not controller.connection_healthy:
                raise RuntimeError("Manual refresh did not verify the room connection")
            collaboration_extras = {
                "chat_visible": True,
                "chat_optimistic": True,
                "lock_round_trip": True,
                "explicit_label_selector": True,
                "backup_settings_editable": True,
                "manual_refresh": True,
            }

        advanced_features = None
        if os.environ.get("LIVE_SEGMENTATION_SMOKE_TEST_ADVANCED", "0") == "1":
            if not hasattr(controller.client, "_room_path"):
                raise RuntimeError("Advanced smoke test requires shared-folder mode")
            from LiveSegmentationLib.collaboration import (
                SharedFolderRoomClient,
                encode_mask_delta,
            )

            peer = SharedFolderRoomClient(
                os.environ["LIVE_SEGMENTATION_SMOKE_SHARED_FOLDER"], "thomas-smoke"
            )
            room_metadata = json.loads(
                controller.client._require_room(controller.room_id)
                .joinpath("room.json")
                .read_text(encoding="utf-8")
            )
            peer.join(room_name, room_metadata["volume_signature"])
            peer.presence(
                controller.room_id,
                {
                    "active_segment_id": segment_id,
                    "active_segment_name": "Test segment",
                    "crosshair_ras": [1.0, 2.0, 3.0],
                    "slice_offsets": {"Red": 4.0, "Yellow": 5.0, "Green": 6.0},
                    "active_effect": "Paint",
                },
            )
            controller.refresh_now()
            deadline = time.time() + 8
            while time.time() < deadline and "thomas-smoke" not in controller.presence_by_user:
                pump_events(0.15)
            if "thomas-smoke" not in controller.presence_by_user:
                raise RuntimeError("Rich collaborator presence did not appear")
            controller.collaborator_combo.setCurrentText("thomas-smoke")
            controller.jump_to_selected_user()

            controller.chat_input.setText("Anchored spatial message")
            controller.send_chat_message()
            deadline = time.time() + 8
            anchored_messages = []
            while time.time() < deadline:
                pump_events(0.15)
                anchored_messages = controller.client.chat_messages(controller.room_id, 0)
                if any(item.get("anchor") for item in anchored_messages):
                    break
            if not any(item.get("anchor") for item in anchored_messages):
                raise RuntimeError("Spatial chat anchor was not persisted")

            room_template = {
                "name": "Smoke labels",
                "segments": [
                    {"id": segment_id, "name": "Test segment", "color": "#37E8B8"},
                    {"id": "SecondLabel", "name": "Second label", "color": "#E31A1C"},
                ],
            }
            controller.client.set_material_template(controller.room_id, room_template)
            controller.refresh_advanced_state()
            deadline = time.time() + 8
            while time.time() < deadline and not controller.material_template_state:
                pump_events(0.15)
            controller.apply_material_template()
            if segmentation.GetSegmentation().GetSegment("SecondLabel") is None:
                raise RuntimeError("Room material template did not create its missing label")

            peer.request_segment_access(controller.room_id, segment_id, "Smoke request")
            controller.client.set_room_role(controller.room_id, "thomas-smoke", "reviewer")
            peer.set_review_state(controller.room_id, segment_id, "approved", "Smoke review")
            controller.refresh_advanced_state()
            deadline = time.time() + 8
            while time.time() < deadline:
                pump_events(0.15)
                if (
                    controller.review_states_state.get(segment_id, {}).get("state")
                    == "approved"
                ):
                    break
            if controller.review_states_state.get(segment_id, {}).get("state") != "approved":
                raise RuntimeError("Review workflow did not synchronize")
            controller.client.set_segment_lock(controller.room_id, segment_id, False)

            controller.snapshot_label_edit.setText("Smoke milestone")
            controller.request_room_snapshot()
            deadline = time.time() + 12
            manifests = []
            while time.time() < deadline:
                pump_events(0.15)
                manifests = controller.client.snapshot_manifests(controller.room_id)
                if manifests:
                    break
            if not manifests:
                raise RuntimeError("Room snapshot was not published")
            if manifests[-1].get("label") != "Smoke milestone":
                raise RuntimeError("Milestone name was not preserved in version history")
            if not list(controller.client._room_path.joinpath("operation-archives").glob("*.zip")):
                raise RuntimeError("Snapshot did not compact earlier loose operations")

            base_sequence = controller.last_sequence
            baseline = widget.segment_mask_in_reference_geometry(
                segmentation, segment_id, volume, image.shape
            ).astype(np.uint8)
            concurrent = baseline.copy()
            concurrent[0, 0, 0] = 1 - concurrent[0, 0, 0]
            alice_operation = {
                "client_operation_id": f"advanced-alice-{uuid.uuid4()}",
                "segment_id": segment_id,
                "segment_name": "Test segment",
                "color_hex": "#37E8B8",
                "base_sequence": base_sequence,
                **encode_mask_delta(baseline, concurrent),
            }
            controller.client.push_operation(controller.room_id, alice_operation)
            peer_operation = {
                **alice_operation,
                "client_operation_id": f"advanced-peer-{uuid.uuid4()}",
            }
            conflict_result = peer.push_operation(controller.room_id, peer_operation)
            if not conflict_result.get("conflicts"):
                raise RuntimeError("Concurrent voxel overlap was not detected")
            controller.refresh_advanced_state()
            deadline = time.time() + 8
            while time.time() < deadline and not controller.conflicts_state:
                pump_events(0.15)
            if not controller.conflicts_state:
                raise RuntimeError("Conflict panel did not receive conflict records")

            controller.run_room_diagnostics()
            deadline = time.time() + 8
            while time.time() < deadline and not getattr(controller, "last_diagnostics", None):
                pump_events(0.15)
            if controller.last_diagnostics.get("status") != "ok":
                raise RuntimeError("Room diagnostics did not pass")
            if not controller.history_records:
                raise RuntimeError("Version timeline remained empty")
            advanced_features = {
                "rich_presence": True,
                "spatial_chat": True,
                "material_template": True,
                "review_and_access_request": True,
                "snapshot_and_compaction": True,
                "conflict_detection": True,
                "diagnostics": True,
                "history_entries": len(controller.history_records),
            }

        connection_recovery = None
        if os.environ.get("LIVE_SEGMENTATION_SMOKE_TEST_CONNECTION", "0") == "1":
            if not hasattr(controller.client, "_room_path"):
                raise RuntimeError("Connection interruption test requires shared-folder mode")
            controller.timer.stop()
            deadline = time.time() + 5
            while any(
                worker is not None and worker.is_alive()
                for worker in controller_workers(controller)
            ):
                if time.time() >= deadline:
                    raise RuntimeError("Synchronization worker did not become idle")
                pump_events(0.1)
            controller._drain_worker_results()
            room_path = Path(controller.client._room_path)
            unavailable_path = room_path.with_name(room_path.name + ".offline-test")
            os.replace(room_path, unavailable_path)
            controller.timer.start()
            try:
                controller.refresh_now()
                deadline = time.time() + 8
                while time.time() < deadline:
                    pump_events(0.15)
                    if not controller.connection_healthy:
                        break
                if controller.connection_healthy or "Connection problem" not in controller.status_label.text:
                    raise RuntimeError("Interrupted share was still shown as online")
            finally:
                controller.timer.stop()
                deadline = time.time() + 5
                while any(
                    worker is not None and worker.is_alive()
                    for worker in controller_workers(controller)
                ):
                    if time.time() >= deadline:
                        raise RuntimeError("Offline workers did not become idle")
                    pump_events(0.1)
                os.replace(unavailable_path, room_path)
                controller.timer.start()
            controller.refresh_now()
            deadline = time.time() + 8
            while time.time() < deadline:
                pump_events(0.15)
                if controller.connection_healthy and "Connection problem" not in controller.status_label.text:
                    break
            if not controller.connection_healthy:
                raise RuntimeError("Connection did not recover after manual refresh")
            connection_recovery = {
                "offline_detected": True,
                "manual_recovery": True,
            }

        backup = None
        if os.environ.get("LIVE_SEGMENTATION_SMOKE_TEST_BACKUP", "0") == "1":
            reservation = controller.client.reserve_project_backup(
                controller.room_id, 60
            )
            if reservation is None:
                raise RuntimeError("The shared room did not reserve a project backup")
            if not controller._create_project_backup(reservation):
                raise RuntimeError(controller._last_error or "Project backup failed")
            backup_path = Path(reservation["path"])
            if not backup_path.is_file() or not zipfile.is_zipfile(backup_path):
                raise RuntimeError("The project backup is missing or is not a valid MRB bundle")
            backup = {
                "file": backup_path.name,
                "size_bytes": backup_path.stat().st_size,
                "valid_mrb": True,
            }

        standard_segment_editor_node = editor.segmentationNode().GetID()
        lifecycle = None
        if os.environ.get("LIVE_SEGMENTATION_SMOKE_TEST_LIFECYCLE", "0") == "1":
            first_node_id = segmentation.GetID()
            controller.leave()
            pump_events(0.2)
            remaining_after_leave = [
                node
                for node in slicer.util.getNodesByClass("vtkMRMLSegmentationNode")
                if node.GetAttribute("LiveSegmentation.SharedReplica") == "1"
            ]
            if slicer.mrmlScene.GetNodeByID(first_node_id) is not None:
                raise RuntimeError("Leaving kept the old room segmentation in the scene")
            if widget.segmentation_selector.currentNode() is not None:
                raise RuntimeError("Leaving did not clear the shared segmentation field")
            if remaining_after_leave:
                raise RuntimeError("Leaving kept stale room replicas")
            if editor.segmentationNode() is not None:
                raise RuntimeError("Leaving did not clear Segment Editor label fields")

            controller.join()
            deadline = time.time() + 12
            rejoined_voxels = 0
            while time.time() < deadline:
                pump_events(0.15)
                rejoined = controller._segmentation_node()
                if (
                    controller.initial_sync_complete
                    and rejoined is not None
                    and rejoined.GetSegmentation().GetSegment(segment_id) is not None
                ):
                    rejoined_mask = widget.segment_mask_in_reference_geometry(
                        rejoined, segment_id, volume, image.shape
                    )
                    rejoined_voxels = int(np.asarray(rejoined_mask, dtype=np.uint8).sum())
                    if rejoined_voxels >= 27:
                        break
            replicas = [
                node
                for node in slicer.util.getNodesByClass("vtkMRMLSegmentationNode")
                if node.GetAttribute("LiveSegmentation.SharedReplica") == "1"
            ]
            if len(replicas) != 1:
                raise RuntimeError(
                    f"Rejoining created {len(replicas)} room replicas instead of one"
                )
            widget.open_segment_editor()
            editor = widget._standard_segment_editor_widget()
            if editor is None or editor.segmentationNode() != replicas[0]:
                raise RuntimeError("Rejoining did not attach the clean room replica")
            second_node_id = replicas[0].GetID()
            controller.leave()
            pump_events(0.2)
            if editor.segmentationNode() is not None:
                raise RuntimeError("Second leave did not clear Segment Editor")
            if any(
                node.GetAttribute("LiveSegmentation.SharedReplica") == "1"
                for node in slicer.util.getNodesByClass("vtkMRMLSegmentationNode")
            ):
                raise RuntimeError("Second leave kept a room replica")
            lifecycle = {
                "old_node_removed": True,
                "rejoin_replica_count": len(replicas),
                "rejoined_voxels": rejoined_voxels,
                "second_node_removed": slicer.mrmlScene.GetNodeByID(second_node_id) is None,
                "editor_cleared": editor.segmentationNode() is None,
            }

        return {
            "ok": True,
            "module_path": slicer.util.modulePath("LiveSegmentation"),
            "module_title": module.title,
            "live_ui_title": controller.group.title,
            "standard_segment_editor_node": standard_segment_editor_node,
            "connected": controller.connected,
            "status": controller.status_label.text,
            "room_id": controller.room_id,
            "operation_count": len(operations),
            "last_sequence": max(int(item["sequence"]) for item in operations),
            "segment_id": segment_id,
            "mode": mode,
            "transport": transport,
            "voxel_count": voxel_count,
            "lifecycle": lifecycle,
            "backup": backup,
            "collaboration_extras": collaboration_extras,
            "advanced_features": advanced_features,
            "connection_recovery": connection_recovery,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "traceback": traceback.format_exc()}
    finally:
        if controller is not None:
            controller.cleanup()
        if widget is not None:
            widget.cleanup()
        slicer.mrmlScene.Clear(0)
        for key, (existed, value) in settings_snapshot.items():
            if existed:
                settings.setValue(key, value)
            else:
                settings.remove(key)
        settings.sync()


output_path = os.environ.get("LIVE_SEGMENTATION_SLICER_PROBE_OUTPUT")
if not output_path:
    raise RuntimeError("LIVE_SEGMENTATION_SLICER_PROBE_OUTPUT is required")

result = run_probe()
Path(output_path).write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
)
slicer.app.exit(0 if result["ok"] else 1)
