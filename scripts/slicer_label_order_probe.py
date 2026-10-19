"""Native IR drag, two MRML peers, private folder log and checkpoint/rejoin."""
import json
import os
import tempfile
import time
import traceback
import uuid
from pathlib import Path

import numpy as np
import qt
import slicer

KEEP_ALIVE = []


def pump():
    for _ in range(8):
        slicer.app.processEvents()
        time.sleep(.003)


def run():
    result = {"ok": False}
    controllers = []
    live = ir = client = None
    try:
        from LiveSegmentationLib.collaboration import (
            LiveCollaborationController,
            SharedFolderRoomClient,
            encode_mask_crop_snapshot,
        )
        live = slicer.modules.livesegmentation.widgetRepresentation().self()
        live.live_collaboration.timer.stop()
        KEEP_ALIVE.append(live)
        shape = (32,) * 3
        volume = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "Private order source")
        slicer.util.updateVolumeFromArray(volume, np.zeros(shape, np.uint8))
        live.source_volume_selector.setCurrentNode(volume)
        folder = tempfile.mkdtemp(prefix="private-label-order-")
        client = SharedFolderRoomClient(folder, "private-author")
        room = client.join("private-label-order", "a" * 64)
        ids = ["LiveSeg-" + str(uuid.uuid4()) for _ in range(3)]
        masks = {}
        for i, sid in enumerate(ids):
            mask = np.zeros(shape, np.uint8)
            mask[2+i*7:5+i*7, 3:7, 8:12] = 1
            masks[sid] = mask
            client.push_operation(room["id"], {
                "client_operation_id": str(uuid.uuid4()), "segment_id": sid,
                "segment_name": chr(65+i), "color_hex": ["#FF0000", "#0000FF", "#00FF00"][i],
                "metadata_update": True, **encode_mask_crop_snapshot(mask, [0, 32, 0, 32, 0, 32], shape),
            })
        errors = []

        def peer(name):
            node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", name)
            node.CreateDefaultDisplayNodes()
            node.SetReferenceImageGeometryParameterFromVolumeNode(volume)
            controller = LiveCollaborationController(live)
            controllers.append(controller)
            KEEP_ALIVE.append(controller)
            controller.connected = controller.initial_sync_complete = True
            controller.volume_shape = shape
            controller.segmentation_node_id = node.GetID()
            controller.user_name = name
            controller.room_id = room["id"]
            controller.client = client if name == "private-author" else SharedFolderRoomClient(folder, name)
            if controller.client is not client:
                controller.client.join("private-label-order", "a" * 64)
            controller._show_error = lambda message, popup=False: errors.append(str(message))
            controller._refresh_label_combo = lambda: None
            controller._update_lock_controls = lambda: None
            controller._append_history_operations = lambda ops: None
            controller._append_activity = lambda message: None
            controller._sync_operation_journal = lambda: None
            return controller, node

        def pull(controller):
            for operation in client.operations(room["id"], controller.last_sequence, limit=500):
                controller._incoming_operations.append(operation)
            end = time.monotonic()+20
            while controller._incoming_operations or (controller._checkpoint_group and controller._checkpoint_group.complete):
                controller._drain_incoming_operations()
                pump()
                assert not errors, errors
                assert time.monotonic() < end, "Private order replay did not converge"

        first, first_node = peer("private-author")
        second, second_node = peer("private-observer")
        pull(first)
        pull(second)
        for controller, node in ((first, first_node), (second, second_node)):
            controller._observe_segmentation(node)
        live.segmentation_selector.setCurrentNode(first_node)
        standard = live._standard_segment_editor_widget()
        standard.setSegmentationNode(first_node)
        standard.setCurrentSegmentID(ids[0])
        slicer.util.selectModule("SlicerNNInteractiveIR")
        ir = slicer.modules.slicernninteractiveir.widgetRepresentation().self()
        KEEP_ALIVE.append(ir)
        ir.set_explicit_target_segment(second_node, ids[2], lock=False)
        editor = ir.ui.editor_widget
        editor.setSourceVolumeNode(volume)
        table = editor.findChild("QTableView", "SegmentsTable")
        pump()
        assert table is not None

        def position(name):
            model = table.model()
            index = next(model.index(r, c) for r in range(model.rowCount()) for c in range(model.columnCount())
                         if str(model.data(model.index(r, c), qt.Qt.DisplayRole)) == name)
            return qt.QPointF(table.visualRect(index).center())

        source, target = position("C"), position("A")
        for event, pos, buttons in ((qt.QEvent.MouseButtonPress, source, qt.Qt.LeftButton),
                (qt.QEvent.MouseMove, target, qt.Qt.LeftButton),
                (qt.QEvent.MouseButtonRelease, target, qt.Qt.NoButton)):
            qt.QApplication.sendEvent(table.viewport(), qt.QMouseEvent(event, pos,
                qt.Qt.NoButton if event == qt.QEvent.MouseMove else qt.Qt.LeftButton, buttons, qt.Qt.NoModifier))
        pump()
        assert list(second_node.GetSegmentation().GetSegmentIDs()) == [ids[2], ids[0], ids[1]]
        assert len(second.outgoing) == 1 and second.outgoing[0]["segment_order"] == [ids[2], ids[0], ids[1]]
        assert not second.dirty_segments, "Drag queued a voxel edit"
        operation = second.outgoing.pop()
        second.awaiting_echo.append(operation)
        second.client.push_operation(room["id"], operation)
        # Isolate the two simulated participants' editor parameter nodes. Two
        # modules in one Slicer otherwise share its standard editor singleton.
        standard.setMRMLSegmentEditorNode(slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentEditorNode", "Private first editor"))
        standard.setSegmentationNode(first_node)
        standard.setCurrentSegmentID(ids[0])
        result["selection_before_replay"] = {"standard": standard.currentSegmentID(),
                                              "ir": ir.get_current_segment_id()}
        pull(first)
        pull(second)
        result["selection_after_replay"] = {"standard": standard.currentSegmentID(),
                                             "ir": ir.get_current_segment_id()}
        assert list(first_node.GetSegmentation().GetSegmentIDs()) == [ids[2], ids[0], ids[1]]
        assert standard.currentSegmentID() == ids[0]
        assert ir.get_selected_segmentation_node_and_segment_id() == (second_node, ids[2])
        assert not first.outgoing and not second.outgoing and not second.awaiting_echo
        for view in (table, standard.findChild("QTableView", "SegmentsTable")):
            model = view.model()
            column = next(c for c in range(model.columnCount())
                          if str(model.data(model.index(0, c), qt.Qt.DisplayRole)) == "C")
            assert [str(model.data(model.index(r, column), qt.Qt.DisplayRole))
                    for r in range(3)] == ["C", "A", "B"]
        result.update(native_ir_drag_shared=True, standard_target_preserved=True,
                      ir_target_preserved=True, native_tables_same_visible_order=True,
                      no_echo_or_voxel_edit=True)
        manifest = client.publish_room_snapshot(room["id"], first._snapshot_operations(), expected_sequence=first.last_sequence)
        assert manifest and not manifest.get("skipped")
        late, late_node = peer("private-late")
        pull(late)
        assert list(late_node.GetSegmentation().GetSegmentIDs()) == [ids[2], ids[0], ids[1]]
        for node in (first_node, second_node, late_node):
            for i, sid in enumerate(ids):
                np.testing.assert_array_equal(slicer.util.arrayFromSegmentBinaryLabelmap(node, sid, volume), masks[sid])
                assert node.GetSegmentation().GetSegment(sid).GetName() == chr(65+i)
                assert tuple(node.GetSegmentation().GetSegment(sid).GetColor()) == [(1., 0., 0.), (0., 0., 1.), (0., 1., 0.)][i]
        result.update(ok=True, native_checkpoint_rejoin_order=True, masks_and_names_unchanged=True,
                      colors_unchanged=True,
                      room_operations=len(client.operations(room["id"], 0)),
                      note="Private local folder, real native MRML/Qt; no Antscan or inference/model initialized")
    except Exception:
        result["error"] = traceback.format_exc()
    finally:
        for controller in controllers:
            controller._unobserve_segmentation()
            controller.connected = False
            if controller.client is not client:
                controller.client.leave(room["id"])
        if client is not None:
            client.leave(room["id"])
        if ir is not None:
            ir.cleanup()
        if live is not None:
            live.cleanup()
        slicer.mrmlScene.Clear(0)
        Path(os.environ["LIVE_LABEL_ORDER_OUTPUT"]).write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result), flush=True)
        slicer.app.exit(0 if result["ok"] else 1)


run()
