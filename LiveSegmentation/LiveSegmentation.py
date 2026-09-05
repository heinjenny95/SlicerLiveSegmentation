from __future__ import annotations

import numpy as np
import qt
import slicer
import vtk
import vtkSegmentationCorePython as vtkSegmentationCore
from LiveSegmentationLib.collaboration import LiveCollaborationController
from LiveSegmentationLib.features import stable_user_color, validate_material_template
from LiveSegmentationLib.version import PLUGIN_VERSION
from slicer.ScriptedLoadableModule import (
    ScriptedLoadableModule,
    ScriptedLoadableModuleTest,
    ScriptedLoadableModuleWidget,
)


def clear_legacy_connection_settings():
    """Remove network targets before the module widget or any Qt path field exists."""
    settings = qt.QSettings()
    prefix = "LiveSegmentation/collaboration/"
    for key in ("room", "transport", "sharedFolder", "server"):
        settings.remove(prefix + key)
    settings.sync()


# Scripted modules are imported while Slicer discovers them.  Clearing legacy
# connection targets here is intentionally earlier than widget setup so a stale
# UNC value from an older release cannot survive until UI restoration.
clear_legacy_connection_settings()


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
            "network folder, trusted direct LAN relay, or remote HTTPS server. Segmentation tools "
            "remain separate and can edit the selected node through Slicer's MRML scene."
        )
        self.parent.acknowledgementText = (
            "Built as a transport-independent collaboration layer for 3D Slicer."
        )


class LiveSegmentationWidget(ScriptedLoadableModuleWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.live_collaboration = None
        self._live_session_active = False
        self._previous_segment_editor_overwrite_mode = None

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
        active = bool(active)
        self._live_session_active = active
        self.open_segment_editor_button.enabled = active
        editor = self._standard_segment_editor_widget()
        if active:
            self._enable_exclusive_segment_editing(editor)
        else:
            self._restore_segment_editor_overwrite_mode(editor)

    def _enable_exclusive_segment_editing(self, editor):
        """Make the most recent paint stroke own every voxel it touches.

        A live room represents a material partition, not overlapping regions.
        Keeping Slicer's ``OverwriteNone`` mode here allowed two ordinary
        Segment Editor labels to claim the same voxel locally.  The transport
        also resolves remote claims by the shared operation sequence, so the
        local editor must use the same last-writer-wins rule.
        """
        if editor is None:
            return
        try:
            parameter_node = editor.mrmlSegmentEditorNode()
            if parameter_node is None:
                return
            if self._previous_segment_editor_overwrite_mode is None:
                self._previous_segment_editor_overwrite_mode = int(
                    parameter_node.GetOverwriteMode()
                )
            parameter_node.SetOverwriteMode(
                slicer.vtkMRMLSegmentEditorNode.OverwriteAllSegments
            )
        except Exception:
            pass

    def _restore_segment_editor_overwrite_mode(self, editor):
        previous = self._previous_segment_editor_overwrite_mode
        self._previous_segment_editor_overwrite_mode = None
        if editor is None or previous is None:
            return
        try:
            parameter_node = editor.mrmlSegmentEditorNode()
            if parameter_node is not None:
                parameter_node.SetOverwriteMode(int(previous))
        except Exception:
            pass

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
            self._configure_standard_segment_editor(
                editor, segmentation_node, self.get_volume_node()
            )
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
                    try:
                        editor.setActiveEffect(None)
                    except Exception:
                        pass
                    if hasattr(editor, "setSourceVolumeNode"):
                        editor.setSourceVolumeNode(None)
                    elif hasattr(editor, "setMasterVolumeNode"):
                        editor.setMasterVolumeNode(None)
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

    def show_remote_change_highlight_crop(self, changed_crop, voxel_bbox, author):
        """Show a remote-edit overlay without allocating a reference-sized volume."""
        reference = self.get_volume_node()
        if reference is None or not np.any(changed_crop):
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
        empty = np.zeros_like(np.asarray(changed_crop, dtype=np.uint8))
        if not self.update_segment_binary_labelmap_crop(
            empty,
            np.asarray(changed_crop, dtype=np.uint8),
            voxel_bbox,
            node,
            segment_id,
            reference,
        ):
            mask = np.zeros(tuple(reversed(reference.GetImageData().GetDimensions())), dtype=np.uint8)
            z0, z1, y0, y1, x0, x1 = [int(value) for value in voxel_bbox]
            mask[z0:z1, y0:y1, x0:x1] = changed_crop
            self.update_segment_binary_labelmap_from_array(
                mask, node, segment_id, reference
            )
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

    def _configure_standard_segment_editor(self, editor, segmentation_node, volume_node):
        editor.setSegmentationNode(segmentation_node)
        if hasattr(editor, "setSourceVolumeNode"):
            editor.setSourceVolumeNode(volume_node)
        elif hasattr(editor, "setMasterVolumeNode"):
            editor.setMasterVolumeNode(volume_node)
        if self._live_session_active:
            self._enable_exclusive_segment_editing(editor)

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
        # Attach the authoritative room replica before the module becomes
        # visible.  This prevents its qMRMLSegmentsModel from briefly binding to
        # a stale node while queued segment notifications are still settling.
        editor = self._standard_segment_editor_widget()
        if editor is not None:
            self._configure_standard_segment_editor(
                editor, segmentation_node, volume_node
            )
        slicer.util.selectModule("SegmentEditor")
        editor = self._standard_segment_editor_widget()
        if editor is None:
            slicer.util.errorDisplay("Slicer's Segment Editor is not available.")
            return
        self._configure_standard_segment_editor(editor, segmentation_node, volume_node)

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
                    if hasattr(editor, "currentSegmentID"):
                        selected_segment_id = str(editor.currentSegmentID() or "")
                    elif hasattr(editor, "selectedSegmentID"):
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
            if hasattr(editor, "setCurrentSegmentID"):
                editor.setCurrentSegmentID(str(segment_id))
            else:
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
    def _reference_ijk_to_segmentation_ras_matrix(
        reference_volume_node, segmentation_node
    ):
        """Return reference IJK-to-RAS expressed in the segmentation coordinate system.

        Incremental labelmap mutation is safe only for linear parent transforms.
        The caller falls back to Slicer's regular resampling importer otherwise.
        """
        reference_ijk_to_ras = vtk.vtkMatrix4x4()
        reference_volume_node.GetIJKToRASMatrix(reference_ijk_to_ras)
        reference_parent = reference_volume_node.GetParentTransformNode()
        segmentation_parent = segmentation_node.GetParentTransformNode()
        if reference_parent == segmentation_parent:
            return reference_ijk_to_ras
        reference_to_segmentation = vtk.vtkMatrix4x4()
        if not slicer.vtkMRMLTransformNode.GetMatrixTransformBetweenNodes(
            reference_parent, segmentation_parent, reference_to_segmentation
        ):
            return None
        result = vtk.vtkMatrix4x4()
        vtk.vtkMatrix4x4.Multiply4x4(
            reference_to_segmentation, reference_ijk_to_ras, result
        )
        return result

    @staticmethod
    def _matrices_match(first, second, tolerance=1e-6):
        return all(
            abs(float(first.GetElement(row, column)) - float(second.GetElement(row, column)))
            <= tolerance
            for row in range(4)
            for column in range(4)
        )

    @classmethod
    def segment_mask_crop_in_reference_geometry(
        cls, segmentation_node, segment_id, reference_volume_node
    ):
        """Return ``(crop, zyx_bounds)`` directly from Slicer's internal labelmap.

        This avoids the temporary full-size label volume created by
        ``arrayFromSegmentBinaryLabelmap``. ``None`` means that geometry requires
        the slower, general resampling path. An empty segment returns ``(None, None)``.
        """
        segment = segmentation_node.GetSegmentation().GetSegment(segment_id)
        if segment is None:
            raise RuntimeError(f"Segment no longer exists: {segment_id}")
        binary_name = (
            vtkSegmentationCore.vtkSegmentationConverter.GetSegmentationBinaryLabelmapRepresentationName()
        )
        image = segment.GetRepresentation(binary_name)
        if image is None or image.GetPointData().GetScalars() is None:
            return (None, None)
        reference_matrix = cls._reference_ijk_to_segmentation_ras_matrix(
            reference_volume_node, segmentation_node
        )
        if reference_matrix is None:
            return None
        image_matrix = vtk.vtkMatrix4x4()
        image.GetImageToWorldMatrix(image_matrix)
        if not cls._matrices_match(image_matrix, reference_matrix):
            return None
        extent = [int(value) for value in image.GetExtent()]
        if extent[0] > extent[1] or extent[2] > extent[3] or extent[4] > extent[5]:
            return (None, None)
        internal = slicer.util.arrayFromSegmentInternalBinaryLabelmap(
            segmentation_node, segment_id
        )
        label_value = int(segment.GetLabelValue())
        crop = np.asarray(internal == label_value, dtype=np.uint8)
        occupied_z = np.flatnonzero(np.any(crop, axis=(1, 2)))
        if not occupied_z.size:
            return (None, None)
        occupied_y = np.flatnonzero(np.any(crop, axis=(0, 2)))
        occupied_x = np.flatnonzero(np.any(crop, axis=(0, 1)))
        local_bounds = [
            int(occupied_z[0]), int(occupied_z[-1]) + 1,
            int(occupied_y[0]), int(occupied_y[-1]) + 1,
            int(occupied_x[0]), int(occupied_x[-1]) + 1,
        ]
        z0, z1, y0, y1, x0, x1 = local_bounds
        crop = np.ascontiguousarray(crop[z0:z1, y0:y1, x0:x1])
        bounds = [
            extent[4] + z0,
            extent[4] + z1,
            extent[2] + y0,
            extent[2] + y1,
            extent[0] + x0,
            extent[0] + x1,
        ]
        return crop, bounds

    @classmethod
    def capture_segment_mask_steps(cls, segmentation_node, segment_id, reference_volume_node):
        """Copy a stable mask for background diffing, yielding between slabs."""
        segment = segmentation_node.GetSegmentation().GetSegment(segment_id)
        if segment is None:
            raise RuntimeError("Live label changed during mask capture")
        binary_name = vtkSegmentationCore.vtkSegmentationConverter.GetSegmentationBinaryLabelmapRepresentationName()
        image = segment.GetRepresentation(binary_name)
        if image is None or image.GetPointData().GetScalars() is None:
            return None, None
        matrix = cls._reference_ijk_to_segmentation_ras_matrix(reference_volume_node, segmentation_node)
        image_matrix = vtk.vtkMatrix4x4()
        image.GetImageToWorldMatrix(image_matrix)
        if matrix is None or not cls._matrices_match(image_matrix, matrix):
            result = cls.segment_mask_crop_in_reference_geometry(segmentation_node, segment_id, reference_volume_node)
            if result is None:
                # Preserve Slicer's general resampling compatibility. This is
                # the uncommon transformed-geometry fallback, not the bounded
                # path used by standard Segment Editor and source-aligned ROIs.
                shape = tuple(reversed(reference_volume_node.GetImageData().GetDimensions()))
                mask = cls.segment_mask_in_reference_geometry(segmentation_node, segment_id, reference_volume_node, shape)
                return mask, [0, shape[0], 0, shape[1], 0, shape[2]]
            return result
        revision = (image.GetMTime(), image.GetPointData().GetScalars().GetMTime())
        label_value = int(segment.GetLabelValue())
        extent = [int(value) for value in image.GetExtent()]
        internal = slicer.util.arrayFromSegmentInternalBinaryLabelmap(segmentation_node, segment_id)
        copy = np.empty(internal.shape, dtype=np.uint8)
        depth = max(1, (1024 * 1024) // max(1, internal.shape[1] * internal.shape[2] * internal.itemsize))
        bounds = None
        for start in range(0, internal.shape[0], depth):
            end = min(start + depth, internal.shape[0])
            slab = copy[start:end]
            np.equal(internal[start:end], label_value, out=slab)
            z = np.flatnonzero(np.any(slab, axis=(1, 2)))
            if z.size:
                y = np.flatnonzero(np.any(slab, axis=(0, 2)))
                x = np.flatnonzero(np.any(slab, axis=(0, 1)))
                found = [start + int(z[0]), start + int(z[-1]) + 1, int(y[0]), int(y[-1]) + 1, int(x[0]), int(x[-1]) + 1]
                if bounds is None:
                    bounds = found
                else:
                    for axis in range(3):
                        bounds[2 * axis] = min(bounds[2 * axis], found[2 * axis])
                        bounds[2 * axis + 1] = max(bounds[2 * axis + 1], found[2 * axis + 1])
            yield None
        current = segmentation_node.GetSegmentation().GetSegment(segment_id)
        if current is None or current.GetRepresentation(binary_name) != image or int(current.GetLabelValue()) != label_value or (
            image.GetMTime(), image.GetPointData().GetScalars().GetMTime()
        ) != revision:
            raise RuntimeError("Live label changed during mask capture")
        if bounds is None:
            return None, None
        z0, z1, y0, y1, x0, x1 = bounds
        return copy[z0:z1, y0:y1, x0:x1], [extent[4] + z0, extent[4] + z1, extent[2] + y0, extent[2] + y1, extent[0] + x0, extent[0] + x1]

    @classmethod
    def segment_mask_region_in_reference_geometry(
        cls, segmentation_node, segment_id, reference_volume_node, voxel_bbox
    ):
        """Read only one reference-geometry region from an internal labelmap.

        The returned region always has the requested size and is zero-padded
        outside the segment's cropped internal extent. ``None`` requests the
        caller's general resampling fallback.
        """
        segment = segmentation_node.GetSegmentation().GetSegment(segment_id)
        if segment is None:
            raise RuntimeError(f"Segment no longer exists: {segment_id}")
        z0, z1, y0, y1, x0, x1 = [int(value) for value in voxel_bbox]
        result = np.zeros((z1 - z0, y1 - y0, x1 - x0), dtype=np.uint8)
        binary_name = (
            vtkSegmentationCore.vtkSegmentationConverter.GetSegmentationBinaryLabelmapRepresentationName()
        )
        image = segment.GetRepresentation(binary_name)
        if image is None or image.GetPointData().GetScalars() is None:
            return result
        reference_matrix = cls._reference_ijk_to_segmentation_ras_matrix(
            reference_volume_node, segmentation_node
        )
        if reference_matrix is None:
            return None
        image_matrix = vtk.vtkMatrix4x4()
        image.GetImageToWorldMatrix(image_matrix)
        if not cls._matrices_match(image_matrix, reference_matrix):
            return None
        extent = [int(value) for value in image.GetExtent()]
        ix0, ix1, iy0, iy1, iz0, iz1 = extent
        overlap = [
            max(z0, iz0),
            min(z1, iz1 + 1),
            max(y0, iy0),
            min(y1, iy1 + 1),
            max(x0, ix0),
            min(x1, ix1 + 1),
        ]
        oz0, oz1, oy0, oy1, ox0, ox1 = overlap
        if oz0 >= oz1 or oy0 >= oy1 or ox0 >= ox1:
            return result
        internal = slicer.util.arrayFromSegmentInternalBinaryLabelmap(
            segmentation_node, segment_id
        )
        label_value = int(segment.GetLabelValue())
        result[
            oz0 - z0 : oz1 - z0,
            oy0 - y0 : oy1 - y0,
            ox0 - x0 : ox1 - x0,
        ] = (
            internal[
                oz0 - iz0 : oz1 - iz0,
                oy0 - iy0 : oy1 - iy0,
                ox0 - ix0 : ox1 - ix0,
            ]
            == label_value
        )
        return result

    @classmethod
    def _oriented_image_from_reference_crop(
        cls, values, voxel_bbox, reference_volume_node, segmentation_node
    ):
        matrix = cls._reference_ijk_to_segmentation_ras_matrix(
            reference_volume_node, segmentation_node
        )
        if matrix is None:
            return None
        values = np.ascontiguousarray(values, dtype=np.uint8)
        z0, z1, y0, y1, x0, x1 = [int(value) for value in voxel_bbox]
        if values.shape != (z1 - z0, y1 - y0, x1 - x0):
            raise ValueError("Incremental labelmap crop does not match its voxel bounds")
        image = vtkSegmentationCore.vtkOrientedImageData()
        image.SetExtent(x0, x1 - 1, y0, y1 - 1, z0, z1 - 1)
        image.AllocateScalars(vtk.VTK_UNSIGNED_CHAR, 1)
        from vtk.util import numpy_support

        scalars = numpy_support.vtk_to_numpy(
            image.GetPointData().GetScalars()
        ).reshape(values.shape)
        scalars[:] = values
        image.GetPointData().GetScalars().Modified()
        image.SetGeometryFromImageToWorldMatrix(matrix)
        return image

    @classmethod
    def prepare_segment_labelmap_extent(
        cls, segmentation_node, segment_id, reference_volume_node, voxel_bbox
    ):
        """Grow an isolated layer with bounded copies before applying tiles.

        Everything runs on the GUI thread, one yielded slice at a time. A local
        stroke during preparation invalidates the staged copy; it is retried
        against the new image, never committed over the user's newer data.
        """
        from vtk.util import numpy_support

        segmentation = segmentation_node.GetSegmentation()
        segment = segmentation.GetSegment(segment_id)
        binary_name = vtkSegmentationCore.vtkSegmentationConverter.GetSegmentationBinaryLabelmapRepresentationName()
        matrix = cls._reference_ijk_to_segmentation_ras_matrix(reference_volume_node, segmentation_node)
        if segment is None or matrix is None:
            return
        old_image = segment.GetRepresentation(binary_name)
        old_extent = None
        old_array = None
        old_revision = None
        old_label_value = int(segment.GetLabelValue())
        if old_image is not None and old_image.GetPointData().GetScalars() is not None:
            old_matrix = vtk.vtkMatrix4x4()
            old_image.GetImageToWorldMatrix(old_matrix)
            if not cls._matrices_match(old_matrix, matrix):
                return
            old_extent = [int(value) for value in old_image.GetExtent()]
            old_array = slicer.util.arrayFromSegmentInternalBinaryLabelmap(segmentation_node, segment_id)
            old_revision = (old_image.GetMTime(), old_image.GetPointData().GetScalars().GetMTime())
        z0, z1, y0, y1, x0, x1 = [int(value) for value in voxel_bbox]
        if old_extent is not None:
            ox0, ox1, oy0, oy1, oz0, oz1 = old_extent
            if ox0 <= x0 < x1 <= ox1 + 1 and oy0 <= y0 < y1 <= oy1 + 1 and oz0 <= z0 < z1 <= oz1 + 1:
                return
            z0, z1 = min(z0, oz0), max(z1, oz1 + 1)
            y0, y1 = min(y0, oy0), max(y1, oy1 + 1)
            x0, x1 = min(x0, ox0), max(x1, ox1 + 1)
        dims = tuple(reversed(reference_volume_node.GetImageData().GetDimensions()))
        bounds = [z0, z1, y0, y1, x0, x1]
        for axis in range(3):
            bounds[2 * axis] = max(0, (bounds[2 * axis] // 64) * 64)
            bounds[2 * axis + 1] = min(dims[axis], ((bounds[2 * axis + 1] + 63) // 64) * 64)
        z0, z1, y0, y1, x0, x1 = bounds
        target = np.empty((z1 - z0, y1 - y0, x1 - x0), dtype=old_array.dtype if old_array is not None else np.uint8)
        slab_depth = max(1, (1024 * 1024) // max(1, target.shape[1] * target.shape[2] * target.itemsize))
        for start in range(0, target.shape[0], slab_depth):
            end = min(start + slab_depth, target.shape[0])
            slab = target[start:end]
            slab.fill(0)
            if old_array is not None:
                low, high = max(z0 + start, oz0), min(z0 + end, oz1 + 1)
                if low < high:
                    target[low - z0:high - z0, oy0 - y0:oy1 + 1 - y0, ox0 - x0:ox1 + 1 - x0] = old_array[low - oz0:high - oz0]
            yield None
        current_segment = segmentation.GetSegment(segment_id)
        if current_segment is None or current_segment.GetRepresentation(binary_name) != old_image:
            raise RuntimeError("Live label changed during extent preparation")
        if int(current_segment.GetLabelValue()) != old_label_value or (
            old_revision is not None
            and (old_image.GetMTime(), old_image.GetPointData().GetScalars().GetMTime()) != old_revision
        ):
            raise RuntimeError("Live label changed during extent preparation")
        image = vtkSegmentationCore.vtkOrientedImageData()
        image.SetExtent(x0, x1 - 1, y0, y1 - 1, z0, z1 - 1)
        # numpy_to_vtk retains the NumPy owner for this zero-copy scalar buffer.
        image.GetPointData().SetScalars(numpy_support.numpy_to_vtk(target.reshape(-1), deep=False))
        image.SetGeometryFromImageToWorldMatrix(matrix)
        segment.RemoveAllRepresentations(binary_name)
        segment.AddRepresentation(binary_name, image)
        segment.Modified()

    @classmethod
    def update_segment_binary_labelmap_crop(
        cls,
        current_crop,
        target_crop,
        voxel_bbox,
        segmentation_node,
        segment_id,
        reference_volume_node,
    ):
        """Apply additions/removals only inside a small reference-geometry crop."""
        current = np.asarray(current_crop, dtype=np.uint8)
        target = np.asarray(target_crop, dtype=np.uint8)
        if current.shape != target.shape or current.ndim != 3:
            return False
        changed = current != target
        if not np.any(changed):
            return True
        segmentation = segmentation_node.GetSegmentation()
        segment = segmentation.GetSegment(segment_id)
        if segment is None:
            return False
        binary_name = (
            vtkSegmentationCore.vtkSegmentationConverter.GetSegmentationBinaryLabelmapRepresentationName()
        )
        image = segment.GetRepresentation(binary_name)
        reference_matrix = cls._reference_ijk_to_segmentation_ras_matrix(
            reference_volume_node, segmentation_node
        )
        if image is not None and image.GetPointData().GetScalars() is not None and reference_matrix is not None:
            image_matrix = vtk.vtkMatrix4x4()
            image.GetImageToWorldMatrix(image_matrix)
            z0, z1, y0, y1, x0, x1 = [int(value) for value in voxel_bbox]
            ix0, ix1, iy0, iy1, iz0, iz1 = [int(value) for value in image.GetExtent()]
            contained = (
                ix0 <= x0 < x1 <= ix1 + 1
                and iy0 <= y0 < y1 <= iy1 + 1
                and iz0 <= z0 < z1 <= iz1 + 1
            )
            # Only isolated layers can be edited directly: a packed sibling
            # may use a different label value in the same VTK scalar array.
            isolated = not any(
                str(other_id) != str(segment_id)
                and segmentation.GetSegment(other_id).GetRepresentation(binary_name) == image
                for other_id in segmentation.GetSegmentIDs()
            )
            if contained and isolated and cls._matrices_match(image_matrix, reference_matrix):
                internal = slicer.util.arrayFromSegmentInternalBinaryLabelmap(
                    segmentation_node, segment_id
                )
                region = internal[z0 - iz0:z1 - iz0, y0 - iy0:y1 - iy0, x0 - ix0:x1 - ix0]
                region[changed] = np.where(target[changed] != 0, int(segment.GetLabelValue()), 0)
                image.GetPointData().GetScalars().Modified()
                # This layer is isolated. Invalidate only its derived meshes;
                # image.Modified() invalidates every label's representation
                # through vtkSegmentation's global source-image observer.
                segment.RemoveAllRepresentations(binary_name)
                segment.Modified()
                return True
        if segment.GetRepresentation(binary_name) is None:
            segmentation.CreateRepresentation(binary_name)
        logic = slicer.modules.segmentations.logic()
        removals = np.logical_and(changed, target == 0)
        if np.any(removals):
            keep_mask = np.ones(target.shape, dtype=np.uint8)
            keep_mask[removals] = 0
            image = cls._oriented_image_from_reference_crop(
                keep_mask, voxel_bbox, reference_volume_node, segmentation_node
            )
            if image is None or not logic.SetBinaryLabelmapToSegment(
                image, segmentation_node, segment_id, logic.MODE_MERGE_MIN
            ):
                return False
        additions = np.logical_and(changed, target != 0)
        if np.any(additions):
            add_mask = np.zeros(target.shape, dtype=np.uint8)
            add_mask[additions] = 1
            image = cls._oriented_image_from_reference_crop(
                add_mask, voxel_bbox, reference_volume_node, segmentation_node
            )
            if image is None or not logic.SetBinaryLabelmapToSegment(
                image, segmentation_node, segment_id, logic.MODE_MERGE_MAX
            ):
                return False
        return True

    @staticmethod
    def refresh_segmentation_display(segmentation_node, segment_id):
        display_node = segmentation_node.GetDisplayNode()
        if display_node is None:
            segmentation_node.CreateDefaultDisplayNodes()
            display_node = segmentation_node.GetDisplayNode()
        # Native labelmap/segment notifications already invalidate the affected
        # view. Re-emitting broad Modified events causes redundant redraws and
        # must not turn a user's hidden label visible on every remote edit.


class LiveSegmentationTest(ScriptedLoadableModuleTest):
    def runTest(self):
        self.delayDisplay("Live Segmentation module loaded")
        self.assertEqual(PLUGIN_VERSION, "0.14.6")
