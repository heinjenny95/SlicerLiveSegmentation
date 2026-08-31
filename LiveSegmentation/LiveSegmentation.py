from __future__ import annotations

import numpy as np
import qt
import slicer
import vtkSegmentationCorePython as vtkSegmentationCore
from LiveSegmentationLib.collaboration import LiveCollaborationController
from LiveSegmentationLib.features import stable_user_color, validate_material_template
from slicer.ScriptedLoadableModule import (
    ScriptedLoadableModule,
    ScriptedLoadableModuleTest,
    ScriptedLoadableModuleWidget,
)

PLUGIN_VERSION = "0.9.0"


class LiveSegmentation(ScriptedLoadableModule):
    """Thin live-synchronization layer for Slicer's standard segmentation nodes."""

    def __init__(self, parent):
        super().__init__(parent)
        self.parent.title = "Live Segmentation"
        self.parent.categories = ["Segmentation"]
        self.parent.dependencies = ["Segmentations", "SegmentEditor"]
        self.parent.contributors = ["Live Segmentation contributors"]
        self.parent.helpText = (
            "Synchronize a standard 3D Slicer Segmentation node through a shared "
            "network folder or an optional collaboration server. Segmentation tools "
            "remain separate and can edit the selected node through Slicer's MRML scene."
        )
        self.parent.acknowledgementText = (
            "Built as a transport-independent collaboration layer for 3D Slicer."
        )


class LiveSegmentationWidget(ScriptedLoadableModuleWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.live_collaboration = None

    def setup(self):
        super().setup()
        import ctk

        introduction = qt.QLabel(
            "Choose the source volume and join a room. The room automatically creates "
            "or selects one shared Segmentation node; nobody has to create a separate "
            "segmentation first. Drawing is done with Slicer's standard Segment Editor "
            "or another separately installed tool."
        )
        introduction.setWordWrap(True)
        self.layout.addWidget(introduction)

        data_group = ctk.ctkCollapsibleButton()
        data_group.text = "Slicer data"
        data_group.collapsed = False
        data_form = qt.QFormLayout(data_group)

        self.source_volume_selector = slicer.qMRMLNodeComboBox()
        self.source_volume_selector.nodeTypes = ["vtkMRMLScalarVolumeNode"]
        self.source_volume_selector.noneEnabled = True
        self.source_volume_selector.addEnabled = False
        self.source_volume_selector.removeEnabled = False
        self.source_volume_selector.setMRMLScene(slicer.mrmlScene)

        self.segmentation_selector = slicer.qMRMLNodeComboBox()
        self.segmentation_selector.nodeTypes = ["vtkMRMLSegmentationNode"]
        self.segmentation_selector.noneEnabled = True
        self.segmentation_selector.addEnabled = False
        self.segmentation_selector.removeEnabled = False
        self.segmentation_selector.renameEnabled = True
        self.segmentation_selector.setMRMLScene(slicer.mrmlScene)

        data_form.addRow("Source volume", self.source_volume_selector)
        data_form.addRow("Shared segmentation", self.segmentation_selector)
        segmentation_hint = qt.QLabel(
            "Leave this empty for a new shared segmentation. To start a new room "
            "from existing work, select that segmentation before joining. Existing "
            "rooms always load their own shared state."
        )
        segmentation_hint.setWordWrap(True)
        data_form.addRow(segmentation_hint)

        actions = qt.QHBoxLayout()
        self.open_segment_editor_button = qt.QPushButton("Open Segment Editor")
        self.open_segment_editor_button.enabled = False
        actions.addWidget(self.open_segment_editor_button)
        data_form.addRow(actions)
        self.layout.addWidget(data_group)

        self.source_volume_selector.connect(
            "currentNodeChanged(vtkMRMLNode*)", self.on_source_volume_changed
        )
        self.segmentation_selector.connect(
            "currentNodeChanged(vtkMRMLNode*)", self.on_segmentation_changed
        )
        self.open_segment_editor_button.connect(
            "clicked()", self.open_segment_editor
        )

        self.live_collaboration = LiveCollaborationController(self)
        self.live_collaboration.setup()
        self.layout.addStretch(1)

        self._select_scene_defaults()

    def cleanup(self):
        if self.live_collaboration is not None:
            self.live_collaboration.cleanup()

    def _select_scene_defaults(self):
        if self.source_volume_selector.currentNode() is None:
            volumes = slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")
            if volumes:
                self.source_volume_selector.setCurrentNode(volumes[-1])

    def set_live_inputs_enabled(self, enabled):
        enabled = bool(enabled)
        self.source_volume_selector.enabled = enabled
        self.segmentation_selector.enabled = enabled

    def set_live_session_active(self, active):
        self.open_segment_editor_button.enabled = bool(active)

    def get_volume_node(self):
        return self.source_volume_selector.currentNode()

    def get_segmentation_node(self):
        return self.segmentation_selector.currentNode()

    def _set_reference_geometry_if_empty(self, segmentation_node=None):
        segmentation_node = segmentation_node or self.get_segmentation_node()
        volume_node = self.get_volume_node()
        if segmentation_node is None or volume_node is None:
            return
        if len(segmentation_node.GetSegmentation().GetSegmentIDs()) == 0:
            segmentation_node.SetReferenceImageGeometryParameterFromVolumeNode(
                volume_node
            )

    def on_source_volume_changed(self, node=None):
        del node
        self._set_reference_geometry_if_empty()

    def on_segmentation_changed(self, node=None):
        self._set_reference_geometry_if_empty(node)

    def _create_segmentation_node(self, name="Live Segmentation"):
        volume_node = self.get_volume_node()
        if volume_node is None:
            slicer.util.errorDisplay("Select a source volume first.")
            return None
        segmentation_node = slicer.mrmlScene.AddNewNodeByClass(
            "vtkMRMLSegmentationNode", str(name)
        )
        segmentation_node.CreateDefaultDisplayNodes()
        segmentation_node.SetReferenceImageGeometryParameterFromVolumeNode(volume_node)
        self.segmentation_selector.setCurrentNode(segmentation_node)
        return segmentation_node

    def create_segmentation(self):
        return self._create_segmentation_node()

    def prepare_shared_segmentation(self, room):
        """Select the authoritative local replica for a joined live room.

        A newly created room may be seeded from the currently selected segmentation.
        Every join uses a clean, removable room replica so leave/rejoin cycles cannot
        stack duplicate labels or accidentally turn a user's source node into live state.
        """
        room_name = str(room.get("name") or "Live room")
        seed_node = self.get_segmentation_node() if bool(room.get("created")) else None
        if seed_node is not None and seed_node.GetAttribute("LiveSegmentation.SharedReplica") == "1":
            seed_node = None

        self.clear_live_segmentation()
        segmentation_node = self._create_segmentation_node(
            f"{room_name} – Shared segmentation"
        )
        if segmentation_node is None:
            return None

        if seed_node is not None:
            source_segmentation = seed_node.GetSegmentation()
            target_segmentation = segmentation_node.GetSegmentation()
            for segment_id in source_segmentation.GetSegmentIDs():
                copied = target_segmentation.CopySegmentFromSegmentation(
                    source_segmentation, segment_id
                )
                if copied is False:
                    raise RuntimeError(f"Could not copy seed label: {segment_id}")

        segmentation_node.SetAttribute("LiveSegmentation.RoomId", str(room["id"]))
        segmentation_node.SetAttribute("LiveSegmentation.SharedReplica", "1")
        self.segmentation_selector.setCurrentNode(segmentation_node)
        self._set_reference_geometry_if_empty(segmentation_node)

        editor = self._standard_segment_editor_widget()
        if editor is not None:
            editor.setSegmentationNode(segmentation_node)
            volume_node = self.get_volume_node()
            if hasattr(editor, "setSourceVolumeNode"):
                editor.setSourceVolumeNode(volume_node)
            elif hasattr(editor, "setMasterVolumeNode"):
                editor.setMasterVolumeNode(volume_node)
        return segmentation_node

    def clear_live_segmentation(self, segmentation_node_id=None):
        """Detach and remove all room-managed segmentation replicas from the scene."""
        editor = self._standard_segment_editor_widget()
        if editor is not None:
            try:
                editor_node = editor.segmentationNode()
                if (
                    editor_node is not None
                    and (
                        not segmentation_node_id
                        or editor_node.GetID() == segmentation_node_id
                        or editor_node.GetAttribute("LiveSegmentation.SharedReplica") == "1"
                    )
                ):
                    editor.setSegmentationNode(None)
            except Exception:
                pass

        selected = self.get_segmentation_node()
        if selected is not None and (
            not segmentation_node_id
            or selected.GetID() == segmentation_node_id
            or selected.GetAttribute("LiveSegmentation.SharedReplica") == "1"
        ):
            self.segmentation_selector.setCurrentNode(None)

        removable = []
        for node in slicer.util.getNodesByClass("vtkMRMLSegmentationNode"):
            if node.GetAttribute("LiveSegmentation.SharedReplica") == "1" or (
                segmentation_node_id and node.GetID() == segmentation_node_id
            ):
                removable.append(node)
        for node in removable:
            slicer.mrmlScene.RemoveNode(node)
        self.clear_remote_highlights()

    def apply_material_template(self, payload):
        """Create/update standard room labels without replacing existing voxel data."""
        template = validate_material_template(payload)
        segmentation_node = self.get_segmentation_node()
        if segmentation_node is None:
            raise RuntimeError("Join a live room before applying its material template")
        segmentation = segmentation_node.GetSegmentation()
        for item in template["segments"]:
            segment = segmentation.GetSegment(item["id"])
            if segment is None:
                segment = slicer.vtkSegment()
                segmentation.AddSegment(segment, item["id"])
            segment.SetName(item["name"])
            color = item["color"].lstrip("#")
            segment.SetColor(
                int(color[0:2], 16) / 255.0,
                int(color[2:4], 16) / 255.0,
                int(color[4:6], 16) / 255.0,
            )
            if item.get("terminology"):
                segment.SetTag("TerminologyEntry", item["terminology"])
        segmentation_node.Modified()

    def show_remote_change_highlight(self, mask, author):
        """Show a short-lived spatial overlay for voxels changed by a peer."""
        reference = self.get_volume_node()
        if reference is None:
            return
        node = slicer.mrmlScene.GetFirstNodeByName("Live remote changes")
        if node is None or node.GetClassName() != "vtkMRMLSegmentationNode":
            node = slicer.mrmlScene.AddNewNodeByClass(
                "vtkMRMLSegmentationNode", "Live remote changes"
            )
            node.SetAttribute("LiveSegmentation.RemoteHighlights", "1")
            node.CreateDefaultDisplayNodes()
            node.SetReferenceImageGeometryParameterFromVolumeNode(reference)
        segment_id = f"Remote-{qt.QDateTime.currentMSecsSinceEpoch()}"
        segment = slicer.vtkSegment()
        segment.SetName(f"Recent change by {author}")
        segment.SetColor(*stable_user_color(author))
        node.GetSegmentation().AddSegment(segment, segment_id)
        self.update_segment_binary_labelmap_from_array(mask, node, segment_id, reference)
        display = node.GetDisplayNode()
        if display is not None:
            display.SetSegmentOpacity2DFill(segment_id, 0.15)
            display.SetSegmentOpacity2DOutline(segment_id, 1.0)
            display.SetSegmentOpacity3D(segment_id, 0.55)
        qt.QTimer.singleShot(
            2500,
            lambda current_node=node, current_id=segment_id: self._remove_highlight_segment(
                current_node, current_id
            ),
        )

    @staticmethod
    def _remove_highlight_segment(node, segment_id):
        try:
            if node is None or node.GetScene() is None:
                return
            node.GetSegmentation().RemoveSegment(segment_id)
            if len(node.GetSegmentation().GetSegmentIDs()) == 0:
                slicer.mrmlScene.RemoveNode(node)
        except Exception:
            pass

    @staticmethod
    def clear_remote_highlights():
        for node in list(slicer.util.getNodesByClass("vtkMRMLSegmentationNode")):
            if node.GetAttribute("LiveSegmentation.RemoteHighlights") == "1":
                slicer.mrmlScene.RemoveNode(node)

    @staticmethod
    def _standard_segment_editor_widget():
        try:
            representation = slicer.modules.segmenteditor.widgetRepresentation()
            scripted_widget = representation.self()
            return getattr(scripted_widget, "editor", None)
        except Exception:
            return None

    def open_segment_editor(self):
        if self.live_collaboration is None or not self.live_collaboration.connected:
            slicer.util.errorDisplay("Join a live room before opening Segment Editor.")
            return
        volume_node = self.get_volume_node()
        if volume_node is None:
            slicer.util.errorDisplay("Select a source volume first.")
            return
        segmentation_node = self.get_segmentation_node()
        if segmentation_node is None:
            segmentation_node = self.create_segmentation()
        if segmentation_node is None:
            return

        slicer.util.setSliceViewerLayers(background=volume_node)
        slicer.util.selectModule("SegmentEditor")
        slicer.app.processEvents()
        editor = self._standard_segment_editor_widget()
        if editor is None:
            slicer.util.errorDisplay("Slicer's Segment Editor is not available.")
            return
        editor.setSegmentationNode(segmentation_node)
        if hasattr(editor, "setSourceVolumeNode"):
            editor.setSourceVolumeNode(volume_node)
        elif hasattr(editor, "setMasterVolumeNode"):
            editor.setMasterVolumeNode(volume_node)

    def get_selected_segmentation_node_and_segment_id(self):
        segmentation_node = self.get_segmentation_node()
        if segmentation_node is None:
            return None, None

        selected_segment_id = None
        editor = self._standard_segment_editor_widget()
        if editor is not None:
            try:
                editor_node = editor.segmentationNode()
                if editor_node == segmentation_node:
                    selected_segment_id = str(editor.selectedSegmentID() or "")
            except Exception:
                selected_segment_id = None

        if not selected_segment_id:
            segment_ids = list(segmentation_node.GetSegmentation().GetSegmentIDs())
            selected_segment_id = segment_ids[0] if segment_ids else None
        return segmentation_node, selected_segment_id

    def select_segment_in_editor(self, segment_id):
        """Make an explicitly managed live label active in Slicer's Segment Editor."""
        segmentation_node = self.get_segmentation_node()
        editor = self._standard_segment_editor_widget()
        if segmentation_node is None or editor is None:
            return False
        try:
            if editor.segmentationNode() != segmentation_node:
                editor.setSegmentationNode(segmentation_node)
            editor.setSelectedSegmentID(str(segment_id))
            return True
        except Exception:
            return False

    @staticmethod
    def segment_mask_in_reference_geometry(
        segmentation_node, segment_id, reference_volume_node, fallback_shape
    ):
        segment = segmentation_node.GetSegmentation().GetSegment(segment_id)
        if segment is None:
            raise RuntimeError(f"Segment no longer exists: {segment_id}")
        try:
            mask = slicer.util.arrayFromSegmentBinaryLabelmap(
                segmentation_node, segment_id, reference_volume_node
            )
            return np.asarray(mask, dtype=bool)
        except Exception as exc:
            binary_name = (
                vtkSegmentationCore.vtkSegmentationConverter.GetSegmentationBinaryLabelmapRepresentationName()
            )
            if segment.GetRepresentation(binary_name) is None:
                return np.zeros(tuple(fallback_shape), dtype=bool)
            raise RuntimeError(
                "Could not read segment in the selected source-volume geometry: "
                f"segment={segment_id}, shape={tuple(fallback_shape)}, error={exc}"
            ) from exc

    @staticmethod
    def update_segment_binary_labelmap_from_array(
        mask, segmentation_node, segment_id, reference_volume_node
    ):
        slicer.util.updateSegmentBinaryLabelmapFromArray(
            np.asarray(mask, dtype=np.uint8),
            segmentation_node,
            segment_id,
            reference_volume_node,
        )

    @staticmethod
    def refresh_segmentation_display(segmentation_node, segment_id):
        display_node = segmentation_node.GetDisplayNode()
        if display_node is None:
            segmentation_node.CreateDefaultDisplayNodes()
            display_node = segmentation_node.GetDisplayNode()
        if display_node is not None:
            display_node.SetVisibility(True)
            display_node.SetSegmentVisibility(segment_id, True)
            display_node.Modified()
        segmentation_node.Modified()


class LiveSegmentationTest(ScriptedLoadableModuleTest):
    def runTest(self):
        self.delayDisplay("Live Segmentation module loaded")
        self.assertEqual(PLUGIN_VERSION, "0.9.0")
