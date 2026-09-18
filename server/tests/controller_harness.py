"""Drive the real LiveCollaborationController without 3D Slicer.

Only Slicer/MRML itself is replaced (segments are numpy arrays). The
controller methods under test, such as ``_prepare_outgoing``,
``_drain_worker_results``, ``_drain_incoming_operations`` and
``_apply_operations``, are the production code. This shows how the
synchronization state machine behaves, not how Slicer behaves.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np

LIVE_CORE = Path(__file__).resolve().parents[2] / "LiveSegmentation" / "LiveSegmentationLib"
sys.path.insert(0, str(LIVE_CORE))

import collaboration  # noqa: E402

SHAPE = (8, 8, 8)


class FakeSegment:
    def __init__(self):
        self.name = ""
        self.color = (0.5, 0.5, 0.5)
        self.mask = np.zeros(SHAPE, np.uint8)
        self.tags = {}

    def SetName(self, name):
        self.name = name

    def GetName(self):
        return self.name

    def SetColor(self, *color):
        self.color = tuple(color)

    def GetColor(self):
        return self.color

    def SetTag(self, key, value):
        self.tags[key] = value

    def GetRepresentation(self, name):
        return None


class FakeSegmentation:
    def __init__(self):
        self.segments = {}

    def GetSegmentIDs(self):
        return list(self.segments)

    def GetSegment(self, segment_id):
        return self.segments.get(str(segment_id))

    def AddSegment(self, segment, segment_id):
        self.segments[str(segment_id)] = segment
        return True

    def RemoveSegment(self, segment_id):
        self.segments.pop(str(segment_id), None)


class FakeNode:
    def __init__(self, node_id):
        self.node_id = node_id
        self.segmentation = FakeSegmentation()

    def GetID(self):
        return self.node_id

    def GetSegmentation(self):
        return self.segmentation

    def Modified(self):
        pass


class FakeScene:
    def __init__(self):
        self.nodes = {}

    def GetNodeByID(self, node_id):
        return self.nodes.get(node_id)


class Owner:
    """The part of the Slicer module the controller calls for voxel access."""

    def get_volume_node(self):
        return object()

    def segment_mask_crop_in_reference_geometry(self, node, segment_id, volume):
        mask = node.GetSegmentation().GetSegment(segment_id).mask
        bounds = collaboration._delta_bounds(mask != 0)
        if bounds is None:
            return None, None
        z0, z1, y0, y1, x0, x1 = bounds
        return mask[z0:z1, y0:y1, x0:x1].copy(), bounds

    def segment_mask_region_in_reference_geometry(self, node, segment_id, volume, bounds):
        z0, z1, y0, y1, x0, x1 = bounds
        return node.GetSegmentation().GetSegment(segment_id).mask[z0:z1, y0:y1, x0:x1].copy()

    def update_segment_binary_labelmap_crop(
        self, current, target, bounds, node, segment_id, volume
    ):
        z0, z1, y0, y1, x0, x1 = bounds
        node.GetSegmentation().GetSegment(segment_id).mask[z0:z1, y0:y1, x0:x1] = target
        return True

    def refresh_segmentation_display(self, *args):
        pass

    def show_remote_change_highlight_crop(self, *args):
        pass

    def select_segment_in_editor(self, *args):
        pass

    def get_selected_segmentation_node_and_segment_id(self):
        return None, None


def install_fake_slicer(monkeypatch):
    """Put stand-ins for ``slicer`` and ``qt`` into sys.modules for one test."""
    scene = FakeScene()
    status_messages = []
    monkeypatch.setitem(
        sys.modules,
        "slicer",
        types.SimpleNamespace(
            util=types.SimpleNamespace(
                showStatusMessage=lambda text, *args: status_messages.append(text)
            ),
            mrmlScene=scene,
            vtkSegment=FakeSegment,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "qt",
        types.SimpleNamespace(
            QTimer=types.SimpleNamespace(singleShot=lambda *args, **kwargs: None)
        ),
    )
    return scene, status_messages


class Peer:
    """One participant: a fake segmentation node plus a real controller."""

    def __init__(self, name, scene):
        self.name = name
        self.node = FakeNode(f"node-{name}")
        scene.nodes[self.node.GetID()] = self.node
        controller = collaboration.LiveCollaborationController(Owner())
        controller.connected = True
        controller.user_name = name
        controller.volume_shape = SHAPE
        controller.segmentation_node_id = self.node.GetID()
        controller.initial_sync_complete = True
        self.activity = []
        self.errors = []
        controller._append_activity = self.activity.append
        controller._sync_operation_journal = lambda *args, **kwargs: None
        controller._show_error = lambda message, popup=False: self.errors.append(message)
        controller._refresh_label_combo = lambda *args, **kwargs: None
        controller._update_lock_controls = lambda *args, **kwargs: None
        controller._update_performance_label = lambda *args, **kwargs: None
        controller._append_history_operations = lambda *args, **kwargs: None
        controller._live_status_text = lambda: "live"
        for widget_name in ("status_label", "recovery_status_label"):
            setattr(controller, widget_name, MagicMock())
        controller.client = MagicMock()
        controller.room_id = "room"
        self.controller = controller

    def mask(self, segment_id):
        segment = self.node.GetSegmentation().GetSegment(segment_id)
        return None if segment is None else segment.mask

    def paint(self, segment_id, index, value=1):
        self.mask(segment_id)[index] = value
        self.controller.dirty_segments.add((self.node.GetID(), segment_id))

    def delete_label(self, segment_id):
        """What the controller sees after the user removed a segment in Slicer."""
        metadata = dict(
            self.controller._segment_metadata.get(segment_id) or {"segment_id": segment_id}
        )
        self.node.GetSegmentation().RemoveSegment(segment_id)
        self.controller.pending_segment_deletions[segment_id] = metadata

    def prepare(self):
        """Run the real outgoing preparation until it is quiescent."""
        controller = self.controller
        for _ in range(20):
            if not (controller.dirty_segments or controller.pending_segment_deletions):
                break
            controller._prepare_outgoing()
            worker = controller._local_encode_worker
            if worker is not None:
                worker.join()
            controller._drain_worker_results()

    def finish_push(self, accepted=(), rejected=(), errors=()):
        """Deliver the result the real edit-push lane reports."""
        accepted = list(accepted)
        rejected = list(rejected)
        self.controller._worker_results.put(
            {
                "lane": "edit-push",
                "session_token": self.controller._session_token,
                "outgoing_ids": [item["client_operation_id"] for item in accepted + rejected],
                "rejected_ids": [item["client_operation_id"] for item in rejected],
                "rejected_segments": [item["segment_id"] for item in rejected],
                "conflicts_detected": [],
                "command_errors": list(errors),
                "snapshot": None,
                "duration": 0.01,
            }
        )
        self.controller._drain_worker_results()

    def apply(self, operation):
        """The real receive path: tile iterator plus _apply_operations."""
        operation = dict(operation)
        if not operation.get("segment_deleted"):
            operation["_packed"] = collaboration.PackedOperationMask(operation)
        self.controller._incoming_operations.append(operation)
        for _ in range(1000):
            if not self.controller._incoming_operations:
                break
            self.controller._incoming_retry_at = 0
            self.controller._drain_incoming_operations()
        assert not self.controller._incoming_operations, "incoming operation is stuck"


class RoomLog:
    """The globally ordered operation sequence of a room."""

    def __init__(self):
        self.operations = []

    def commit(self, operation, author):
        stored = {key: value for key, value in operation.items() if not key.startswith("_")}
        stored["sequence"] = len(self.operations) + 1
        stored["author"] = author
        self.operations.append(stored)
        return stored
