from __future__ import annotations

import json
import os
import shutil
import time
import traceback
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

import numpy as np
import qt
import slicer


def pump_events(seconds):
    deadline = time.time() + float(seconds)
    while time.time() < deadline:
        slicer.app.processEvents()
        time.sleep(0.03)


def wait_for_connection(controller, timeout=12.0):
    deadline = time.time() + float(timeout)
    while time.time() < deadline:
        pump_events(0.08)
        controller.on_timer()
        if controller.connected or not getattr(controller, "_joining", False):
            break
    if not controller.connected:
        raise RuntimeError(controller._last_error or "Live room did not connect")


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
        startup_reset_probe = (
            os.environ.get("LIVE_SEGMENTATION_SMOKE_STARTUP_RESET", "0") == "1"
        )
        if startup_reset_probe:
            settings.setValue(
                "LiveSegmentation/collaboration/sharedFolder",
                r"\\offline.invalid\stale-share",
            )
            settings.setValue("LiveSegmentation/collaboration/room", "stale-room")
            settings.setValue("LiveSegmentation/collaboration/transport", "shared-folder")
            settings.sync()

        volume = slicer.mrmlScene.AddNewNodeByClass(
            "vtkMRMLScalarVolumeNode", "Live Segmentation smoke volume"
        )
        shape_text = os.environ.get("LIVE_SEGMENTATION_SMOKE_VOLUME_SHAPE", "12,11,10")
        image_shape = tuple(int(value) for value in shape_text.split(","))
        if len(image_shape) != 3 or min(image_shape) < 10:
            raise RuntimeError("LIVE_SEGMENTATION_SMOKE_VOLUME_SHAPE must contain three sizes >= 10")
        image = np.zeros(image_shape, dtype=np.int16)
        image[-1, -1, -1] = 1
        slicer.util.updateVolumeFromArray(volume, image)

        module = slicer.app.moduleManager().module("LiveSegmentation")
        if module is None:
            raise RuntimeError("LiveSegmentation module is not registered")
        representation = module.widgetRepresentation()
        widget = representation.self()
        controller = widget.live_collaboration
        if controller is None or not hasattr(controller, "group"):
            raise RuntimeError("Live collaboration UI was not created")
        if startup_reset_probe:
            if controller._text(controller.shared_folder_edit) or controller._text(
                controller.room_edit
            ):
                raise RuntimeError("Startup restored stale shared-folder session fields")
            for key in (
                "LiveSegmentation/collaboration/sharedFolder",
                "LiveSegmentation/collaboration/room",
                "LiveSegmentation/collaboration/transport",
            ):
                if settings.contains(key):
                    raise RuntimeError(f"Startup kept stale connection setting: {key}")
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
        async_timeout_probe = None
        if (
            transport == "shared-folder"
            and os.environ.get("LIVE_SEGMENTATION_SMOKE_ASYNC_TIMEOUT", "0") == "1"
        ):
            import LiveSegmentationLib.collaboration as collaboration_module

            original_join = collaboration_module.SharedFolderRoomClient.join
            original_timeout = collaboration_module.SHARED_FOLDER_JOIN_TIMEOUT_SECONDS
            blocked_seconds = float(
                os.environ.get("LIVE_SEGMENTATION_SMOKE_BLOCK_SECONDS", "2")
            )

            def blocked_join(_client, _room_name, _signature):
                time.sleep(blocked_seconds)
                raise RuntimeError("simulated blocked shared folder")

            collaboration_module.SharedFolderRoomClient.join = blocked_join
            collaboration_module.SHARED_FOLDER_JOIN_TIMEOUT_SECONDS = 0.35
            try:
                started = time.monotonic()
                controller.join()
                returned_after = time.monotonic() - started
                if returned_after >= 0.25:
                    raise RuntimeError(
                        f"Shared-folder join blocked the Slicer UI for {returned_after:.3f}s"
                    )
                deadline = time.time() + 1.2
                while time.time() < deadline and controller._joining:
                    pump_events(0.06)
                    controller.on_timer()
                if controller._joining or controller.connected:
                    raise RuntimeError("Blocked shared-folder join was not cancelled locally")
                if "cancelled locally" not in controller.status_label.text:
                    raise RuntimeError("Join timeout did not produce a clear connection error")
                async_timeout_probe = {
                    "join_return_seconds": round(returned_after, 4),
                    "cancelled_locally": True,
                    "ui_responsive": True,
                }
            finally:
                collaboration_module.SharedFolderRoomClient.join = original_join
                collaboration_module.SHARED_FOLDER_JOIN_TIMEOUT_SECONDS = original_timeout
            controller.shared_folder_edit.setText(shared_folder)
            controller.user_edit.setText(user_name)
            controller.room_edit.setText(room_name)

        delayed_join_probe = None
        delayed_join_seconds = float(
            os.environ.get("LIVE_SEGMENTATION_SMOKE_DELAYED_JOIN_SECONDS", "0")
        )
        if transport == "shared-folder" and delayed_join_seconds > 0:
            import LiveSegmentationLib.collaboration as collaboration_module

            original_join = collaboration_module.SharedFolderRoomClient.join
            delayed_room_name = f"{room_name}-delayed-join"
            controller.room_edit.setText(delayed_room_name)

            def delayed_join(client, delayed_room_name, delayed_signature):
                time.sleep(delayed_join_seconds)
                return original_join(client, delayed_room_name, delayed_signature)

            collaboration_module.SharedFolderRoomClient.join = delayed_join
            try:
                started = time.monotonic()
                controller.join()
                wait_for_connection(controller, timeout=delayed_join_seconds + 8.0)
                elapsed = time.monotonic() - started
                if elapsed < delayed_join_seconds:
                    raise RuntimeError("Delayed join completed before its simulated delay")
                if elapsed >= collaboration_module.SHARED_FOLDER_JOIN_TIMEOUT_SECONDS:
                    raise RuntimeError("Delayed join exceeded the configured join watchdog")
                delayed_join_probe = {
                    "simulated_delay_seconds": delayed_join_seconds,
                    "connected_after_seconds": round(elapsed, 3),
                    "accepted_after_old_four_second_limit": delayed_join_seconds > 4.0,
                    "ui_responsive": True,
                }
            finally:
                collaboration_module.SharedFolderRoomClient.join = original_join
            controller.leave()
            controller.shared_folder_edit.setText(shared_folder)
            controller.user_edit.setText(user_name)
            controller.room_edit.setText(room_name)
        controller.join()
        wait_for_connection(controller)
        segmentation = controller._segmentation_node()
        if segmentation is None:
            raise RuntimeError("The room did not provide a shared segmentation node")
        if widget.segmentation_selector.currentNode() != segmentation:
            raise RuntimeError("The room segmentation was not selected automatically")

        slow_watchdog_probe = None
        if (
            transport == "shared-folder"
            and os.environ.get("LIVE_SEGMENTATION_SMOKE_TEST_SLOW_WATCHDOG", "0")
            == "1"
        ):
            import LiveSegmentationLib.collaboration as collaboration_module

            controller.timer.stop()
            worker_deadline = time.time() + 3.0
            while time.time() < worker_deadline and any(
                worker is not None and worker.is_alive()
                for worker in controller_workers(controller)
            ):
                pump_events(0.05)
            controller._drain_worker_results()
            simulated_silence = (
                collaboration_module.SHARED_FOLDER_SLOW_RESPONSE_SECONDS + 1.0
            )
            controller._last_transport_result_at = (
                time.monotonic() - simulated_silence
            )
            controller.on_timer()
            if not controller.connected:
                raise RuntimeError("Slow-response warning disconnected the live room")
            if "responding slowly" not in controller.status_label.text:
                raise RuntimeError("Slow-response warning was not shown")
            slow_watchdog_probe = {
                "simulated_silence_seconds": simulated_silence,
                "room_preserved": True,
                "warning_visible": True,
            }
            controller._last_transport_result_at = time.monotonic()
            controller._transport_stall_status_second = -1
            controller.connection_healthy = True
            controller.status_label.setText(controller._live_status_text())
            controller.timer.start()

        widget.open_segment_editor()
        editor = widget._standard_segment_editor_widget()
        if editor is None or editor.segmentationNode() != segmentation:
            raise RuntimeError("Standard Segment Editor did not receive the room node")

        operations = []
        edit_latency_seconds = None
        receive_latency_seconds = None
        rapid_component_voxels = None
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
            edit_started = time.monotonic()
            if os.environ.get("LIVE_SEGMENTATION_SMOKE_INCREMENTAL_LOCAL_EDIT", "0") == "1":
                if not widget.update_segment_binary_labelmap_crop(
                    np.zeros((3, 3, 3), dtype=np.uint8),
                    np.ones((3, 3, 3), dtype=np.uint8),
                    [2, 5, 3, 6, 4, 7],
                    segmentation,
                    segment_id,
                    volume,
                ):
                    raise RuntimeError("Incremental local test edit failed")
            else:
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
                    edit_latency_seconds = time.monotonic() - edit_started
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
                if (
                    os.environ.get("LIVE_SEGMENTATION_SMOKE_DIRECT_APPLY", "0") == "1"
                    and controller.last_sequence == 0
                    and operations
                ):
                    controller._apply_operations(operations)
                segment = segmentation.GetSegmentation().GetSegment(segment_id)
                if segment is not None:
                    received_crop = widget.segment_mask_region_in_reference_geometry(
                        segmentation, segment_id, volume, [2, 8, 3, 8, 4, 9]
                    )
                    if received_crop is None:
                        received = widget.segment_mask_in_reference_geometry(
                            segmentation, segment_id, volume, image.shape
                        )
                        voxel_count = int(np.asarray(received, dtype=np.uint8).sum())
                    else:
                        received = None
                        voxel_count = int(np.asarray(received_crop, dtype=np.uint8).sum())
                if controller.initial_sync_complete and voxel_count == 27:
                    try:
                        created = datetime.fromisoformat(
                            str(operations[-1]["created_at"]).replace("Z", "+00:00")
                        ).timestamp()
                        receive_latency_seconds = max(0.0, time.time() - created)
                    except Exception:
                        receive_latency_seconds = None
                    break
            if not operations:
                raise RuntimeError("No remote live operation was received")
            if voxel_count != 27:
                raise RuntimeError(
                    f"Expected 27 synchronized voxels, received {voxel_count}; "
                    f"controller error={controller._last_error!r}; "
                    f"sequence={controller.last_sequence}"
                )
            if return_edit:
                if received is None:
                    if not widget.update_segment_binary_labelmap_crop(
                        np.zeros((2, 2, 2), dtype=np.uint8),
                        np.ones((2, 2, 2), dtype=np.uint8),
                        [6, 8, 6, 8, 6, 8],
                        segmentation,
                        segment_id,
                        volume,
                    ):
                        raise RuntimeError("Incremental return edit failed")
                else:
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

        if os.environ.get("LIVE_SEGMENTATION_SMOKE_RAPID_COMPONENTS", "0") == "1":
            rapid_segment_id = segment_id
            rapid_base_voxels = 35 if (
                (
                    mode == "produce"
                    and os.environ.get(
                        "LIVE_SEGMENTATION_SMOKE_WAIT_FOR_RETURN_EDIT", "0"
                    )
                    == "1"
                )
                or (
                    mode == "consume"
                    and os.environ.get(
                        "LIVE_SEGMENTATION_SMOKE_RETURN_EDIT", "0"
                    )
                    == "1"
                )
            ) else 27
            rapid_bounds = (
                [0, 2, 0, 2, 0, 2],
                [6, 8, 0, 2, 7, 9],
                [9, 11, 8, 10, 0, 2],
            )
            if mode == "produce":
                editor.setCurrentSegmentID(rapid_segment_id)
                slicer.app.processEvents()
                for index, bounds in enumerate(rapid_bounds):
                    component = np.ones((2, 2, 2), dtype=np.uint8)
                    if index == 2:
                        # Reproduce Slicer's occasional early paint event: the
                        # notification precedes the final voxel write and there
                        # is deliberately no second explicit notification.
                        controller._on_segmentation_modified(
                            segmentation.GetSegmentation(), None, rapid_segment_id
                        )
                    if not widget.update_segment_binary_labelmap_crop(
                        np.zeros_like(component),
                        component,
                        bounds,
                        segmentation,
                        rapid_segment_id,
                        volume,
                    ):
                        raise RuntimeError("Rapid component edit failed")
                    if index < 2:
                        controller._on_segmentation_modified(
                            segmentation.GetSegmentation(), None, rapid_segment_id
                        )
                    pump_events(0.12)

                deadline = time.time() + 10
                while time.time() < deadline:
                    pump_events(0.12)
                    operations = controller.client.operations(controller.room_id, 0)
                    rapid_mask = np.zeros(image.shape, dtype=np.uint8)
                    from LiveSegmentationLib.collaboration import apply_mask_delta

                    for item in operations:
                        if item["segment_id"] == rapid_segment_id:
                            rapid_mask = apply_mask_delta(rapid_mask, item)
                    rapid_component_voxels = int(rapid_mask.sum()) - rapid_base_voxels
                    if rapid_component_voxels == 24:
                        break
            else:
                deadline = time.time() + 12
                while time.time() < deadline:
                    pump_events(0.12)
                    rapid_segment = segmentation.GetSegmentation().GetSegment(
                        rapid_segment_id
                    )
                    if rapid_segment is None:
                        continue
                    rapid_mask = widget.segment_mask_in_reference_geometry(
                        segmentation, rapid_segment_id, volume, image.shape
                    )
                    rapid_component_voxels = int(
                        np.asarray(rapid_mask, dtype=np.uint8).sum()
                    ) - rapid_base_voxels
                    if rapid_component_voxels == 24:
                        break
            if rapid_component_voxels != 24:
                raise RuntimeError(
                    "Rapid same-label synchronization lost a component: "
                    f"received {rapid_component_voxels} of 24 voxels"
                )

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
            deadline = time.time() + 20
            manifests = []
            snapshot_archives = []
            while time.time() < deadline:
                pump_events(0.15)
                manifests = controller.client.snapshot_manifests(controller.room_id)
                snapshot_archives = list(
                    controller.client._room_path.joinpath(
                        "operation-archives"
                    ).glob("*.zip")
                )
                if manifests and snapshot_archives:
                    break
            if not manifests:
                raise RuntimeError("Room snapshot was not published")
            if manifests[-1].get("label") != "Smoke milestone":
                raise RuntimeError("Milestone name was not preserved in version history")
            if not snapshot_archives:
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
            transient_message = (
                "Could not read shared file transient-probe.json: "
                "temporary SMB sharing violation"
            )
            controller._worker_results.put(
                {
                    "lane": "edit-pull",
                    "session_token": controller._session_token,
                    "error": transient_message,
                    "duration": 0.01,
                }
            )
            controller._drain_worker_results()
            if (
                not controller.connection_healthy
                or "Connection problem" in controller.status_label.text
                or not controller._connection_validation_pending
            ):
                raise RuntimeError("A single transient SMB read flashed the room offline")
            controller._worker_results.put(
                {
                    "lane": "maintenance",
                    "session_token": controller._session_token,
                    "health_checked": True,
                    "health_ok": True,
                    "duration": 0.01,
                }
            )
            controller._drain_worker_results()
            if controller._connection_validation_pending or not controller.connection_healthy:
                raise RuntimeError("A successful health probe did not confirm the live room")
            room_path = Path(controller.client._room_path)
            shared_root = str(controller.client.shared_folder)
            reconnect_room = str(controller.room_name)
            reconnect_user = str(controller.user_name)
            unavailable_path = room_path.with_name(room_path.name + ".offline-test")
            # QFileSystemWatcher keeps a Windows directory handle open. The
            # interruption probe relies on renaming that directory, so use the
            # controller's 100-ms polling fallback until the path is restored.
            controller._stop_shared_folder_watcher()
            os.replace(room_path, unavailable_path)
            controller.timer.start()
            try:
                controller.refresh_now()
                deadline = time.time() + 8
                while time.time() < deadline:
                    pump_events(0.15)
                    if not controller.connected:
                        break
                if controller.connected or "Connection problem" not in controller.status_label.text:
                    raise RuntimeError("Interrupted share was not reset locally")
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
                # The watcher handle may follow the renamed directory on
                # Windows. Copying the probe room back avoids making that
                # platform detail part of the connection-recovery assertion.
                shutil.copytree(unavailable_path, room_path, dirs_exist_ok=True)
            controller.transport_combo.setCurrentIndex(0)
            controller.shared_folder_edit.setText(shared_root)
            controller.user_edit.setText(reconnect_user)
            controller.room_edit.setText(reconnect_room)
            controller.join()
            wait_for_connection(controller)
            segmentation = controller._segmentation_node()
            if segmentation is None:
                raise RuntimeError("Manual rejoin did not recreate the shared segmentation")
            connection_recovery = {
                "transient_read_tolerated": True,
                "offline_detected": True,
                "session_reset_locally": True,
                "manual_rejoin": True,
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

        deletion_sync = None
        deleted_probe_ids = ("LocalDeletionProbe", "RemoteDeletionProbe")
        if os.environ.get("LIVE_SEGMENTATION_SMOKE_TEST_DELETION", "0") == "1":
            if not hasattr(controller.client, "_room_path"):
                raise RuntimeError("Label deletion smoke test requires shared-folder mode")
            from LiveSegmentationLib.collaboration import (
                SharedFolderRoomClient,
                encode_mask_crop_snapshot,
            )

            local_delete_id, remote_delete_id = deleted_probe_ids
            segmentation.GetSegmentation().AddEmptySegment(
                local_delete_id, "Local deletion probe"
            )
            deadline = time.time() + 8
            local_create_sequence = 0
            while time.time() < deadline:
                pump_events(0.12)
                for item in controller.client.operations(controller.room_id, 0):
                    if (
                        item.get("segment_id") == local_delete_id
                        and not item.get("segment_deleted")
                    ):
                        local_create_sequence = int(item["sequence"])
                if local_create_sequence:
                    break
            if not local_create_sequence:
                raise RuntimeError("Locally created deletion probe did not synchronize")
            segmentation.GetSegmentation().RemoveSegment(local_delete_id)
            deadline = time.time() + 8
            local_delete_sequence = 0
            while time.time() < deadline:
                pump_events(0.12)
                for item in controller.client.operations(
                    controller.room_id, local_create_sequence
                ):
                    if item.get("segment_id") == local_delete_id and item.get(
                        "segment_deleted"
                    ):
                        local_delete_sequence = int(item["sequence"])
                if local_delete_sequence:
                    break
            if not local_delete_sequence:
                raise RuntimeError("Local SegmentRemoved event did not publish a tombstone")

            peer = SharedFolderRoomClient(
                os.environ["LIVE_SEGMENTATION_SMOKE_SHARED_FOLDER"],
                "deletion-peer-smoke",
            )
            room_metadata = json.loads(
                controller.client._require_room(controller.room_id)
                .joinpath("room.json")
                .read_text(encoding="utf-8")
            )
            peer.join(room_name, room_metadata["volume_signature"])
            remote_crop = np.ones((2, 2, 2), dtype=np.uint8)
            remote_create = {
                "client_operation_id": f"remote-create-{uuid.uuid4()}",
                "segment_id": remote_delete_id,
                "segment_name": "Remote deletion probe",
                "color_hex": "#FFD23F",
                "base_sequence": int(controller.last_sequence),
                **encode_mask_crop_snapshot(
                    remote_crop, [1, 3, 1, 3, 1, 3], image.shape
                ),
            }
            remote_create_sequence = int(
                peer.push_operation(controller.room_id, remote_create)["sequence"]
            )
            deadline = time.time() + 8
            while time.time() < deadline:
                pump_events(0.12)
                if segmentation.GetSegmentation().GetSegment(remote_delete_id) is not None:
                    break
            if segmentation.GetSegmentation().GetSegment(remote_delete_id) is None:
                raise RuntimeError("Remote label creation did not reach the Slicer scene")
            remote_delete = {
                "client_operation_id": f"remote-delete-{uuid.uuid4()}",
                "segment_id": remote_delete_id,
                "segment_name": "Remote deletion probe",
                "color_hex": "#FFD23F",
                "base_sequence": remote_create_sequence,
                "segment_deleted": True,
                **encode_mask_crop_snapshot(None, None, image.shape),
            }
            remote_delete_sequence = int(
                peer.push_operation(controller.room_id, remote_delete)["sequence"]
            )
            deadline = time.time() + 8
            while time.time() < deadline:
                pump_events(0.12)
                if segmentation.GetSegmentation().GetSegment(remote_delete_id) is None:
                    break
            if segmentation.GetSegmentation().GetSegment(remote_delete_id) is not None:
                raise RuntimeError("Remote label deletion remained visible in Slicer")
            if any(
                item.get("segment_id") in deleted_probe_ids
                for item in peer.state_at_sequence(
                    controller.room_id, remote_delete_sequence
                )
            ):
                raise RuntimeError("Deleted labels remained in reconstructed room state")
            deletion_sync = {
                "local_remove_published": True,
                "remote_remove_applied": True,
                "historical_state_excludes_deleted_labels": True,
                "local_delete_sequence": local_delete_sequence,
                "remote_delete_sequence": remote_delete_sequence,
            }

        standard_segment_editor_node = editor.segmentationNode().GetID()
        lifecycle = None
        if os.environ.get("LIVE_SEGMENTATION_SMOKE_TEST_LIFECYCLE", "0") == "1":
            from LiveSegmentationLib.collaboration import apply_mask_delta

            expected_rejoin_mask = np.zeros(image.shape, dtype=np.uint8)
            for operation in controller.client.operations(controller.room_id, 0):
                if operation["segment_id"] == segment_id:
                    expected_rejoin_mask = apply_mask_delta(
                        expected_rejoin_mask, operation
                    )
            expected_rejoin_voxels = int(expected_rejoin_mask.sum())
            if expected_rejoin_voxels <= 0:
                raise RuntimeError(
                    "Lifecycle probe could not reconstruct the shared label before leaving"
                )
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
            wait_for_connection(controller)
            deadline = time.time() + 20
            rejoined_voxels = 0
            while time.time() < deadline:
                # The smoke probe itself runs synchronously before Slicer's
                # regular application event loop takes over. Tick the
                # controller explicitly so a completed worker result cannot
                # remain queued merely because this test is pumping Qt events.
                pump_events(0.15)
                controller.on_timer()
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
                    if rejoined_voxels == expected_rejoin_voxels:
                        break
            if rejoined_voxels != expected_rejoin_voxels:
                worker_state = {
                    name: bool(
                        getattr(controller, name, None) is not None
                        and getattr(controller, name).is_alive()
                    )
                    for name in (
                        "_edit_push_worker",
                        "_edit_pull_worker",
                        "_maintenance_worker",
                    )
                }
                try:
                    direct_operation_count = len(
                        controller.client.operations(controller.room_id, 0)
                    )
                except Exception as exc:
                    direct_operation_count = f"error: {exc}"
                timer_active = controller.timer.isActive
                timer_active = (
                    timer_active() if callable(timer_active) else bool(timer_active)
                )
            if deletion_sync and any(
                rejoined.GetSegmentation().GetSegment(probe_id) is not None
                for probe_id in deleted_probe_ids
            ):
                raise RuntimeError("A deleted label reappeared after leaving and rejoining")
                queued_results = []
                while not controller._worker_results.empty():
                    queued_results.append(controller._worker_results.get_nowait())
                for queued_result in queued_results:
                    controller._worker_results.put(queued_result)
                raise RuntimeError(
                    "Rejoining did not restore the complete shared label: "
                    f"received {rejoined_voxels} of {expected_rejoin_voxels} voxels "
                    f"(initial sync complete: {controller.initial_sync_complete}, "
                    f"last sequence: {controller.last_sequence}, "
                    f"target sequence: {controller.initial_sequence}, "
                    f"connected: {controller.connected}, "
                    f"workers: {worker_state}, "
                    f"direct operations: {direct_operation_count}, "
                    f"timer active: {timer_active}, "
                    "queued worker results: "
                    f"{[(item.get('lane'), item.get('session_token'), bool(item.get('error')), len(item.get('operations') or [])) for item in queued_results]}, "
                    f"last error: {controller._last_error})"
                )
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
                "expected_rejoin_voxels": expected_rejoin_voxels,
                "second_node_removed": slicer.mrmlScene.GetNodeByID(second_node_id) is None,
                "editor_cleared": editor.segmentationNode() is None,
            }

        patch_probe_node = slicer.mrmlScene.AddNewNodeByClass(
            "vtkMRMLSegmentationNode", "Incremental patch probe"
        )
        patch_probe_node.SetReferenceImageGeometryParameterFromVolumeNode(volume)
        patch_probe_node.GetSegmentation().AddEmptySegment("PatchProbe", "Patch probe")
        patch_bounds = [1, 5, 1, 5, 1, 5]
        patch_ones = np.ones((4, 4, 4), dtype=np.uint8)
        if not widget.update_segment_binary_labelmap_crop(
            np.zeros_like(patch_ones),
            patch_ones,
            patch_bounds,
            patch_probe_node,
            "PatchProbe",
            volume,
        ):
            raise RuntimeError("Incremental addition probe failed")
        patch_target = patch_ones.copy()
        patch_target[1:3, 1:3, 1:3] = 0
        if not widget.update_segment_binary_labelmap_crop(
            patch_ones,
            patch_target,
            patch_bounds,
            patch_probe_node,
            "PatchProbe",
            volume,
        ):
            raise RuntimeError("Incremental removal probe failed")
        patch_result = widget.segment_mask_region_in_reference_geometry(
            patch_probe_node, "PatchProbe", volume, patch_bounds
        )
        patch_voxels = int(np.asarray(patch_result, dtype=np.uint8).sum())
        slicer.mrmlScene.RemoveNode(patch_probe_node)
        if patch_voxels != 56:
            raise RuntimeError(
                f"Incremental removal kept {patch_voxels} voxels instead of 56"
            )

        async_leave_probe = None
        if (
            controller.connected
            and os.environ.get("LIVE_SEGMENTATION_SMOKE_ASYNC_LEAVE", "0") == "1"
        ):
            leave_client = controller.client

            def blocked_leave(_room_id):
                time.sleep(
                    float(os.environ.get("LIVE_SEGMENTATION_SMOKE_BLOCK_SECONDS", "2"))
                )
                return {"left": True}

            leave_client.leave = blocked_leave
            started = time.monotonic()
            controller.leave()
            leave_returned_after = time.monotonic() - started
            if leave_returned_after >= 0.25:
                raise RuntimeError(
                    "Leaving an unreachable shared folder blocked Slicer for "
                    f"{leave_returned_after:.3f}s"
                )
            if controller.connected or controller._segmentation_node() is not None:
                raise RuntimeError("Local leave did not immediately reset the live session")
            async_leave_probe = {
                "leave_return_seconds": round(leave_returned_after, 4),
                "session_reset_locally": True,
                "ui_responsive": True,
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
            "edit_latency_seconds": edit_latency_seconds,
            "receive_latency_seconds": receive_latency_seconds,
            "rapid_component_voxels": rapid_component_voxels,
            "incremental_patch_probe_voxels": patch_voxels,
            "lifecycle": lifecycle,
            "backup": backup,
            "collaboration_extras": collaboration_extras,
            "advanced_features": advanced_features,
            "connection_recovery": connection_recovery,
            "deletion_sync": deletion_sync,
            "startup_reset": bool(startup_reset_probe),
            "async_timeout": async_timeout_probe,
            "delayed_join": delayed_join_probe,
            "slow_watchdog": slow_watchdog_probe,
            "async_leave": async_leave_probe,
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
