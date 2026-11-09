"""Personal per-label display preferences; no voxel, lock or room operation writes.

Kept byte-identical in the standalone IR bundle. Both modules reuse one live
manager per MRML node via slicer.modules, so they cannot race to save defaults.
"""
import hashlib
import json
import logging
from pathlib import Path

PERSONAL_VISIBILITY_ENABLED = "SegmentationPersonalView/Enabled"


def defer_segment_table_action(table, action, pending_property):
    """Run one pending table action after the native cell editor closes."""
    import qt

    viewport = table.viewport()
    active_editor = next(
        (line for line in table.findChildren("QLineEdit")
         if line.parentWidget() == viewport and not line.isHidden()),
        None,
    )
    if active_editor is not None:
        if not table.property(pending_property):
            table.setProperty(pending_property, True)

            def retry():
                try:
                    if table.property(pending_property):
                        table.setProperty(pending_property, False)
                        action()
                except (RuntimeError, ValueError):
                    return  # The editor, table or scene may already be gone.

            active_editor.destroyed.connect(lambda *unused: qt.QTimer.singleShot(0, retry))
        return True

    table.setProperty(pending_property, False)
    return False


def refresh_segment_table_view(view, node, editor=None):
    """Repair native row controls after the user closes an active cell editor."""
    if view.segmentationNode() != node:
        return False
    table = view.findChild("QTableView", "SegmentsTable")
    if table is None or defer_segment_table_action(
        table, lambda: refresh_segment_table_view(view, node, editor),
        "LiveSegmentationOrderRebindPending",
    ):
        return False

    selected = view.selectedSegmentIDs
    selected = selected() if callable(selected) else selected
    active_segment = editor.currentSegmentID() if editor is not None else None
    scroll = table.verticalScrollBar().value
    blocked = view.blockSignals(True)
    try:
        view.setSegmentationNode(None)
        view.setSegmentationNode(node)
        view.setSelectedSegmentIDs(selected)
        table.verticalScrollBar().setValue(scroll)
    finally:
        view.blockSignals(blocked)
    if active_segment and node.GetSegmentation().GetSegment(active_segment) is not None:
        editor.setCurrentSegmentID(active_segment)
    return True


def personal_view_settings():
    import qt

    # The safe starters use --disable-settings. The application's default
    # Slicer-tmp settings are discarded on startup, so use our OWN stable file.
    directory = Path(qt.QStandardPaths.writableLocation(qt.QStandardPaths.GenericDataLocation))
    directory = directory / "SlicerLiveSegmentation"
    directory.mkdir(parents=True, exist_ok=True)
    return qt.QSettings(str(directory / "personal-label-view.ini"), qt.QSettings.IniFormat)


def personal_visibility_enabled():
    import slicer

    enabled = getattr(slicer.modules, "_personal_segment_visibility_enabled", None)
    if type(enabled) is not bool:
        value = personal_view_settings().value(PERSONAL_VISIBILITY_ENABLED, True)
        enabled = str(value).strip().lower() not in {"false", "0"}
        slicer.modules._personal_segment_visibility_enabled = enabled
    return enabled


def observe_personal_visibility_enabled(callback):
    """Subscribe an existing module UI to the shared setting until its cleanup."""
    import slicer

    observers = getattr(slicer.modules, "_personal_segment_visibility_observers", None)
    if observers is None:
        observers = set()
        slicer.modules._personal_segment_visibility_observers = observers
    observers.add(callback)
    return lambda: observers.discard(callback)


def set_personal_visibility_enabled(enabled):
    import slicer

    enabled = bool(enabled)
    if personal_visibility_enabled() == enabled:
        return
    settings = personal_view_settings()
    settings.setValue(PERSONAL_VISIBILITY_ENABLED, enabled)
    settings.sync()
    slicer.modules._personal_segment_visibility_enabled = enabled
    if not enabled:
        registry = getattr(slicer.modules, "_personal_segment_views", {})
        for manager in tuple(registry.values()):
            manager.close(save=False)
        registry.clear()
    for callback in tuple(getattr(slicer.modules, "_personal_segment_visibility_observers", ())):
        try:
            callback(enabled)
        except Exception:
            logging.exception("Personal label visibility option could not update a module")


class PersonalVisibility:
    def __init__(self, node, settings, scope, schedule):
        self.node, self.settings, self.schedule = node, settings, schedule
        self.scope = None
        self.pending = self.restoring = False
        self.seen, self.states = set(), {}
        self.display = node.GetDisplayNode()
        self.segmentation = node.GetSegmentation()
        self.display_tag = self.display.AddObserver("ModifiedEvent", self.changed)
        self.segment_tag = self.segmentation.AddObserver(self.segmentation.SegmentAdded, self.changed)
        self.set_scope(scope)

    def set_scope(self, scope):
        if scope == self.scope:
            return
        if self.scope is not None:
            self.flush()
        self.scope = scope
        self.key = "SegmentationPersonalView/v1/"+hashlib.sha256(scope.encode("utf-8")).hexdigest()
        try:
            values = json.loads(str(self.settings.value(self.key, "{}")))
            self.states = {sid: state for sid, state in values.items()
                           if isinstance(sid, str) and len(sid) <= 200
                           and isinstance(state, list) and len(state) == 4
                           and all(type(v) is bool for v in state)} if isinstance(values, dict) else {}
        except (ValueError, TypeError):
            self.states = {}
        self.seen = set()
        self._restore_added()

    def _restore_added(self):
        if self.node is None:
            return
        self.restoring = True
        try:
            for sid in self.segmentation.GetSegmentIDs():
                if sid in self.seen:
                    continue
                self.seen.add(sid)
                state = self.states.get(sid)
                if state is not None:
                    self.display.SetSegmentVisibility(sid, state[0])
                    self.display.SetSegmentVisibility2DFill(sid, state[1])
                    self.display.SetSegmentVisibility2DOutline(sid, state[2])
                    self.display.SetSegmentVisibility3D(sid, state[3])
        finally:
            self.restoring = False

    def changed(self, *unused):
        if self.node is None or self.restoring or self.pending:
            return
        self.pending = True
        self.schedule(self.flush)

    def flush(self):
        self.pending = False
        if self.node is None:
            return
        # During a rejoin, labels arrive after bind. Restore by stable ID BEFORE
        # collecting defaults from their new display node.
        self._restore_added()
        changed = False
        for sid in self.segmentation.GetSegmentIDs():
            state = [bool(self.display.GetSegmentVisibility(sid)),
                     bool(self.display.GetSegmentVisibility2DFill(sid)),
                     bool(self.display.GetSegmentVisibility2DOutline(sid)),
                     bool(self.display.GetSegmentVisibility3D(sid))]
            changed |= self.states.get(sid) != state
            self.states[sid] = state
        if changed:
            # Persist personal per-label choices. Global view visibility stays
            # under the native Slicer controls, not these room preferences.
            self.settings.setValue(self.key, json.dumps(self.states, sort_keys=True))
            self.settings.sync()

    def close(self, *, save=True):
        if self.node is None:
            return
        if save:
            self.flush()
        display, segmentation = self.display, self.segmentation
        self.node = self.display = self.segmentation = None
        display.RemoveObserver(self.display_tag)
        segmentation.RemoveObserver(self.segment_tag)


def bind_personal_visibility(node, source=None, scope=None):
    """Bind existing data only. Scope hashes paths; never store credentials."""
    import qt
    import slicer
    import vtk

    if node is None or node.GetScene() != slicer.mrmlScene or node.GetDisplayNode() is None:
        return None
    attribute = "LiveSegmentation.PersonalViewScope"
    if scope is not None:
        node.SetAttribute(attribute, scope)
    else:
        scope = node.GetAttribute(attribute)
    # Keep the room scope authoritative when either extension re-enables the
    # preference. Disabling the setting never touches the native display state.
    if not personal_visibility_enabled():
        return None
    if not scope:
        storage = node.GetStorageNode()
        filename = storage.GetFileName() if storage is not None else None
        source_storage = source.GetStorageNode() if source is not None else None
        source_file = source_storage.GetFileName() if source_storage is not None else None
        geometry = ""
        if source is not None:
            matrix = vtk.vtkMatrix4x4()
            source.GetIJKToRASMatrix(matrix)
            geometry = str((source.GetImageData().GetDimensions() if source.GetImageData() else (),
                            [matrix.GetElement(r, c) for r in range(4) for c in range(4)]))
        identity = json.dumps([filename or node.GetName(), source_file or "", geometry])
        scope = "standalone:"+hashlib.sha256(identity.encode("utf-8")).hexdigest()

    registry = getattr(slicer.modules, "_personal_segment_views", None)
    if registry is None:
        registry = {}
        slicer.modules._personal_segment_views = registry

        @vtk.calldata_type(vtk.VTK_OBJECT)
        def removed(caller, event, removed_node):
            if removed_node is not None:
                for key, manager in list(registry.items()):
                    if removed_node not in (manager.node, manager.display):
                        continue
                    manager.close()
                    registry.pop(key, None)

        def close_all():
            for manager in list(registry.values()):
                manager.close()
            registry.clear()

        tag = slicer.mrmlScene.AddObserver(slicer.mrmlScene.NodeAboutToBeRemovedEvent, removed)
        close_tag = slicer.mrmlScene.AddObserver(slicer.mrmlScene.StartCloseEvent, lambda *unused: close_all())
        slicer.app.aboutToQuit.connect(close_all)
        slicer.modules._personal_segment_view_cleanup = (removed, close_all, tag, close_tag)

    manager = registry.get(node.GetID())
    if manager is None:
        manager = PersonalVisibility(node, personal_view_settings(), scope,
                                     lambda callback: qt.QTimer.singleShot(200, callback))
        registry[node.GetID()] = manager
    else:
        manager.set_scope(scope)
    return manager


def safe_bind_personal_visibility(*args, **kwargs):
    try:
        return bind_personal_visibility(*args, **kwargs)
    except Exception:
        logging.exception("Personal label visibility preferences could not be restored")
        return None
