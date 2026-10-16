from __future__ import annotations

from pathlib import Path

import numpy as np
import qt
import slicer
import vtk
import vtkSegmentationCorePython as vtkSegmentationCore
from LiveSegmentationLib.collaboration import LiveCollaborationController
from LiveSegmentationLib.features import stable_user_color, validate_material_template
from LiveSegmentationLib.roi_geometry import reference_bounds, sample_region
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
            "network folder, Linux SSH folder, trusted direct LAN relay, or remote HTTPS server. Segmentation tools "
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
        self._previous_segment_editor_undo_limit = None
        self._recovery_editor = None
        self._previous_editor_read_only = None
        self.navigation_toolbar = None
        self.navigation_action = None
        self.ir_navigation_action = None

    def setup(self):
        super().setup()
        import ctk

        # Match Slicer's field/content inset instead of placing introductory
        # and group text directly against the module panel's left edge.
        outer_layout = self.layout
        self.content_widget = qt.QWidget(self.parent)
        self.layout = qt.QVBoxLayout(self.content_widget)
        self.layout.setContentsMargins(12, 6, 12, 6)
        outer_layout.addWidget(self.content_widget)
        data_group = ctk.ctkCollapsibleButton()
        data_group.text = "1 · Data"
        data_group.collapsed = False
        data_form = qt.QFormLayout(data_group)
        data_form.setLabelAlignment(qt.Qt.AlignLeft | qt.Qt.AlignVCenter)

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
        self.segmentation_selector.setToolTip(
            "Leave this empty for a new shared segmentation. To start a new room "
            "from existing work, select that segmentation before joining. Existing "
            "rooms always load their own shared state."
        )

        actions = qt.QHBoxLayout()
        self.open_segment_editor_button = qt.QPushButton("Open Segment Editor")
        self.open_segment_editor_button.enabled = False
        actions.addWidget(self.open_segment_editor_button)
        data_form.addRow(actions)
        self.recovery_notice = qt.QLabel()
        self.recovery_notice.setWordWrap(True)
        self.recovery_notice.setStyleSheet("color: #b26a00;")
        self.recovery_notice.hide()
        data_form.addRow(self.recovery_notice)
        self.save_recovery_button = qt.QPushButton("Save offline recovery…")
        self.save_recovery_button.hide()
        self.save_recovery_button.connect("clicked()", self._save_offline_recovery)
        data_form.addRow(self.save_recovery_button)
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
        self._setup_navigation_toolbar()
        self.layout.addStretch(1)

        self._select_scene_defaults()
        self._module_selector = slicer.util.moduleSelector()
        self._module_selector.connect("moduleSelected(QString)", self._on_module_selected)
        self._recovery_editor = self._standard_segment_editor_widget()
        if self._recovery_editor is not None:
            self._recovery_editor.connect("segmentationNodeChanged(vtkMRMLSegmentationNode*)", self._guard_recovery_editing)
            self._guard_recovery_editing(self._recovery_editor.segmentationNode())

    def cleanup(self):
        if getattr(self, "_module_selector", None) is not None:
            self._module_selector.disconnect("moduleSelected(QString)", self._on_module_selected)
            self._module_selector = None
        if self.navigation_toolbar is not None:
            slicer.util.mainWindow().removeToolBar(self.navigation_toolbar)
            self.navigation_toolbar.setObjectName("")
            self.navigation_toolbar.deleteLater()
            self.navigation_toolbar = None
            self.navigation_action = None
            self.ir_navigation_action = None
        if self.live_collaboration is not None:
            self.live_collaboration.cleanup()
        if self._recovery_editor is not None:
            self._recovery_editor.disconnect("segmentationNodeChanged(vtkMRMLSegmentationNode*)", self._guard_recovery_editing)
            self._guard_recovery_editing(None)
            self._recovery_editor = None

    def _guard_recovery_editing(self, node):
        """Segment Editor may auto-select the first node when reopened offline."""
        editor = self._recovery_editor
        if editor is None:
            return
        if node is not None and node.GetAttribute("LiveSegmentation.LocalRecovery") == "1":
            if self._previous_editor_read_only is None:
                self._previous_editor_read_only = bool(editor.readOnly)
            editor.setReadOnly(True)
        elif self._previous_editor_read_only is not None:
            previous = self._previous_editor_read_only
            self._previous_editor_read_only = None
            editor.setReadOnly(previous)

    def enter(self):
        self._refresh_recovery_notice()
        self._refresh_optional_navigation()

    def _on_module_selected(self, module_name):
        """Bind only on actual Segment Editor navigation, never on live/IR entry."""
        if module_name != "SegmentEditor" or not self._live_session_active:
            return
        segmentation = self.get_segmentation_node()
        volume = self.get_volume_node()
        editor = self._standard_segment_editor_widget()
        if segmentation is not None and volume is not None and editor is not None:
            self._configure_standard_segment_editor(editor, segmentation, volume)

    def _setup_navigation_toolbar(self):
        """Return to collaboration without changing sessions or editor targets."""
        if self.navigation_toolbar is not None:
            self._refresh_optional_navigation()
            return
        main_window = slicer.util.mainWindow()
        if main_window is None:
            return
        toolbar = qt.QToolBar("Live Segmentation", main_window)
        toolbar.setObjectName("LiveSegmentationNavigation")
        toolbar.setToolButtonStyle(qt.Qt.ToolButtonTextBesideIcon)
        action = qt.QAction(qt.QIcon(str(Path(__file__).parent / "Resources" / "Icons" / "LiveSegmentation.svg")),
                            "Live Segmentation", toolbar)
        action.setObjectName("LiveSegmentationReturnAction")
        action.setToolTip("Return to Live Segmentation (Ctrl+Alt+L); the room stays connected")
        action.setShortcut(qt.QKeySequence("Ctrl+Alt+L"))
        action.setShortcutContext(qt.Qt.WindowShortcut)
        action.setAutoRepeat(False)
        action.triggered.connect(lambda checked=False: slicer.util.selectModule("LiveSegmentation"))
        toolbar.addAction(action)
        ir_action = qt.QAction(qt.QIcon(str(Path(__file__).parent / "Resources" / "Icons" / "nnInteractiveIR.svg")),
                               "nnInteractive IR", toolbar)
        ir_action.setObjectName("LiveSegmentationIRAction")
        ir_action.setToolTip("Open nnInteractive IR (Ctrl+Alt+I); the room stays connected")
        ir_action.setShortcut(qt.QKeySequence("Ctrl+Alt+I"))
        ir_action.setShortcutContext(qt.Qt.WindowShortcut)
        ir_action.setAutoRepeat(False)
        ir_action.triggered.connect(lambda checked=False: self._open_nninteractive_ir())
        toolbar.addAction(ir_action)
        chat = toolbar.addAction("Chat")
        chat.triggered.connect(lambda checked=False: self.live_collaboration.show_chat_dock())
        chat.setToolTip("Open the persistent collaboration chat without switching modules")
        main_window.addToolBar(qt.Qt.TopToolBarArea, toolbar)
        self.navigation_toolbar = toolbar
        self.navigation_action = action
        self.ir_navigation_action = ir_action
        from LiveSegmentationLib.active_work import WorkTimeUI

        self.live_collaboration.work_time = WorkTimeUI(self.live_collaboration, toolbar)
        self._refresh_optional_navigation()

    def _refresh_optional_navigation(self):
        if self.ir_navigation_action is None:
            return
        available = getattr(slicer.modules, "slicernninteractiveir", None) is not None
        self.ir_navigation_action.setVisible(available)
        self.ir_navigation_action.setEnabled(available)

    def _open_nninteractive_ir(self):
        """Navigate only; never initialize inference or replace editor targets."""
        if getattr(slicer.modules, "slicernninteractiveir", None) is None:
            self._refresh_optional_navigation()
            return
        slicer.util.selectModule("SlicerNNInteractiveIR")

    def _save_offline_recovery(self):
        slicer.util.openSaveDataDialog()
        self._refresh_recovery_notice()

    def _refresh_recovery_notice(self):
        if not hasattr(self, "recovery_notice"):
            return
        recoveries = [node for node in slicer.util.getNodesByClass("vtkMRMLSegmentationNode")
                      if node.GetAttribute("LiveSegmentation.LocalRecovery") == "1"]
        self.recovery_notice.setVisible(bool(recoveries))
        self.save_recovery_button.setVisible(bool(recoveries))
        if recoveries:
            self.recovery_notice.setText(
                f"{len(recoveries)} offline recovery segmentation(s) retained in this scene. "
                "They are hidden and are NOT shared. Save the OFFLINE RECOVERY nodes "
                "to local .seg.nrrd files before closing Slicer; they are not automatic disk backups."
            )

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
            self._guard_segment_editor_undo(editor)
        else:
            self._restore_segment_editor_overwrite_mode(editor)
            self._restore_segment_editor_undo(editor)

    def _guard_segment_editor_undo(self, editor):
        """Native Undo restores the whole segmentation, including colleagues' work."""
        if editor is None:
            return
        if self._previous_segment_editor_undo_limit is None:
            self._previous_segment_editor_undo_limit = int(editor.maximumNumberOfUndoStates)
        # This clears old snapshots and prevents new ones. setUndoEnabled only
        # changes button visibility in Slicer; it does not disable undo().
        editor.setMaximumNumberOfUndoStates(0)

    def _restore_segment_editor_undo(self, editor):
        previous = self._previous_segment_editor_undo_limit
        self._previous_segment_editor_undo_limit = None
        if editor is not None and previous is not None:
            editor.setMaximumNumberOfUndoStates(previous)

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
        if node is not None and node.GetAttribute("LiveSegmentation.LocalRecovery") == "1":
            self.segmentation_selector.setCurrentNode(None)
            return
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

        # Binding a hidden editor invokes every effect's source-volume callback,
        # including full-source resampling in Threshold. IR does not need it.
        if slicer.util.selectedModule() == "SegmentEditor":
            editor = self._standard_segment_editor_widget()
            if editor is not None:
                self._configure_standard_segment_editor(
                    editor, segmentation_node, self.get_volume_node()
                )
        return segmentation_node

    def clear_live_segmentation(self, segmentation_node_id=None, preserve_recovery=False):
        """Detach replicas, retaining unsettled work without copying large labelmaps."""
        recovery_node = None
        if preserve_recovery and segmentation_node_id:
            recovery_node = slicer.mrmlScene.GetNodeByID(segmentation_node_id)
            if recovery_node is not None:
                recovery_node.SetAttribute("LiveSegmentation.SharedReplica", None)
                recovery_node.SetAttribute("LiveSegmentation.LocalRecovery", "1")
                recovery_node.SetName("OFFLINE RECOVERY – " + recovery_node.GetName())
                recovery_node.SetSaveWithScene(True)
                if recovery_node.GetDisplayNode() is not None:
                    recovery_node.GetDisplayNode().SetVisibility(False)
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
            if node.GetAttribute("LiveSegmentation.LocalRecovery") == "1":
                continue
            if node.GetAttribute("LiveSegmentation.SharedReplica") == "1" or (
                segmentation_node_id and node.GetID() == segmentation_node_id
            ):
                removable.append(node)
        for node in removable:
            slicer.mrmlScene.RemoveNode(node)
        self.clear_remote_highlights()
        self._refresh_recovery_notice()
        return recovery_node

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
        if editor.segmentationNode() != segmentation_node:
            editor.setSegmentationNode(segmentation_node)
        current_source = (editor.sourceVolumeNode() if hasattr(editor, "sourceVolumeNode")
                          else editor.masterVolumeNode() if hasattr(editor, "masterVolumeNode") else None)
        if current_source == volume_node:
            pass
        elif hasattr(editor, "setSourceVolumeNode"):
            editor.setSourceVolumeNode(volume_node)
        elif hasattr(editor, "setMasterVolumeNode"):
            editor.setMasterVolumeNode(volume_node)
        if self._live_session_active:
            self._enable_exclusive_segment_editing(editor)
            self._guard_segment_editor_undo(editor)

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
    def _label_reference_geometry(cls, segmentation_node, segment_id, reference_volume_node):
        """Native view and affine reference->label mapping; no full image export."""
        segment = segmentation_node.GetSegmentation().GetSegment(segment_id)
        if segment is None:
            raise RuntimeError(f"Segment no longer exists: {segment_id}")
        binary_name = (
            vtkSegmentationCore.vtkSegmentationConverter.GetSegmentationBinaryLabelmapRepresentationName()
        )
        image = segment.GetRepresentation(binary_name)
        if image is None or image.GetPointData().GetScalars() is None:
            return None
        reference_matrix = cls._reference_ijk_to_segmentation_ras_matrix(
            reference_volume_node, segmentation_node
        )
        if reference_matrix is None:
            raise RuntimeError("Live collaboration requires linear relative transforms. "
                               "Harden the non-linear transform before joining; no full-volume fallback was attempted.")
        image_matrix = vtk.vtkMatrix4x4()
        image.GetImageToWorldMatrix(image_matrix)
        native = np.array([[image_matrix.GetElement(r, c) for c in range(4)] for r in range(4)])
        reference = np.array([[reference_matrix.GetElement(r, c) for c in range(4)] for r in range(4)])
        try:
            mapping = np.linalg.solve(native, reference)
        except np.linalg.LinAlgError as exc:
            raise RuntimeError("Live label has non-invertible geometry") from exc
        extent = [int(value) for value in image.GetExtent()]
        if extent[0] > extent[1] or extent[2] > extent[3] or extent[4] > extent[5]:
            return None
        internal = slicer.util.arrayFromSegmentInternalBinaryLabelmap(
            segmentation_node, segment_id
        )
        return image, internal, extent, mapping, int(segment.GetLabelValue())

    @classmethod
    def segment_mask_crop_in_reference_geometry(cls, segmentation_node, segment_id, reference_volume_node):
        """Read only the native label's reference-space footprint, including ROI offsets."""
        capture = cls.capture_segment_mask_steps(segmentation_node, segment_id, reference_volume_node)
        while True:
            try:
                next(capture)
            except StopIteration as stopped:
                return stopped.value

    @classmethod
    def capture_segment_mask_steps(cls, segmentation_node, segment_id, reference_volume_node):
        """Copy a stable mask for background diffing, yielding between slabs."""
        geometry = cls._label_reference_geometry(segmentation_node, segment_id, reference_volume_node)
        if geometry is None:
            return None, None
        image, internal, extent, mapping, label_value = geometry
        revision = (image.GetMTime(), image.GetPointData().GetScalars().GetMTime())
        binary_name = vtkSegmentationCore.vtkSegmentationConverter.GetSegmentationBinaryLabelmapRepresentationName()

        def verify_capture():
            current = segmentation_node.GetSegmentation().GetSegment(segment_id)
            if current is None or current.GetRepresentation(binary_name) != image or int(current.GetLabelValue()) != label_value or (
                image.GetMTime(), image.GetPointData().GetScalars().GetMTime()
            ) != revision:
                raise RuntimeError("Live label changed during mask capture")
            latest = cls._label_reference_geometry(segmentation_node, segment_id, reference_volume_node)
            if latest is None or not np.allclose(latest[3], mapping, rtol=0, atol=1e-10):
                raise RuntimeError("Live label changed during mask capture")
        # A packed layer can cover distant siblings. Find this label's footprint
        # cooperatively before allocating its capture, not a copy of the layer.
        native_bounds = None
        scan_depth = max(1, (1024 * 1024) // max(1, internal.shape[1] * internal.shape[2]))
        for start in range(0, internal.shape[0], scan_depth):
            slab = internal[start:start+scan_depth] == label_value
            z = np.flatnonzero(np.any(slab, axis=(1, 2)))
            if z.size:
                y = np.flatnonzero(np.any(slab, axis=(0, 2)))
                x = np.flatnonzero(np.any(slab, axis=(0, 1)))
                found = [start+int(z[0]), start+int(z[-1])+1, int(y[0]), int(y[-1])+1, int(x[0]), int(x[-1])+1]
                if native_bounds is None:
                    native_bounds = found
                else:
                    for axis in range(3):
                        native_bounds[2*axis] = min(native_bounds[2*axis], found[2*axis])
                        native_bounds[2*axis+1] = max(native_bounds[2*axis+1], found[2*axis+1])
            yield None
            verify_capture()
        if native_bounds is None:
            return None, None
        nz0, nz1, ny0, ny1, nx0, nx1 = native_bounds
        occupied_extent = [extent[0]+nx0, extent[0]+nx1-1, extent[2]+ny0, extent[2]+ny1-1,
                           extent[4]+nz0, extent[4]+nz1-1]
        shape = tuple(reversed(reference_volume_node.GetImageData().GetDimensions()))
        region = reference_bounds(occupied_extent, mapping, shape)
        if region is None:
            return None, None
        rz0, rz1, ry0, ry1, rx0, rx1 = region
        copy = np.empty((rz1-rz0, ry1-ry0, rx1-rx0), dtype=np.uint8)
        depth = max(1, (1024 * 1024) // max(1, copy.shape[1] * copy.shape[2]))
        bounds = None
        for start in range(0, copy.shape[0], depth):
            end = min(start + depth, copy.shape[0])
            slab = copy[start:end]
            slab[:] = sample_region(internal, extent, mapping,
                                    [rz0+start, rz0+end, ry0, ry1, rx0, rx1], label_value)
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
            verify_capture()
        if bounds is None:
            return None, None
        z0, z1, y0, y1, x0, x1 = bounds
        return copy[z0:z1, y0:y1, x0:x1], [rz0 + z0, rz0 + z1, ry0 + y0, ry0 + y1, rx0 + x0, rx0 + x1]

    @classmethod
    def segment_mask_region_in_reference_geometry(
        cls, segmentation_node, segment_id, reference_volume_node, voxel_bbox
    ):
        """Read only this region, also for shifted/scaled/rotated affine ROIs."""
        z0, z1, y0, y1, x0, x1 = [int(value) for value in voxel_bbox]
        geometry = cls._label_reference_geometry(segmentation_node, segment_id, reference_volume_node)
        if geometry is None:
            return np.zeros((z1-z0, y1-y0, x1-x0), dtype=np.uint8)
        _, internal, extent, mapping, label_value = geometry
        return sample_region(internal, extent, mapping, voxel_bbox, label_value)

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
        # The release number lives in LiveSegmentationLib/version.py only; a
        # literal here went stale for three releases and failed every run.
        self.assertRegex(PLUGIN_VERSION, r"^\d+\.\d+\.\d+$")
