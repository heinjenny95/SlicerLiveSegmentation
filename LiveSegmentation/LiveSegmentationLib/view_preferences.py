"""Personal per-label display preferences; no voxel, lock or room operation writes.

Kept byte-identical in the standalone IR bundle. Both modules reuse one live
manager per MRML node via slicer.modules, so they cannot race to save defaults.
"""
import hashlib
import json
import logging


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
            # No global visibility flags: fast 3D preview temporarily hides the
            # original surfaces. Persisting that flag would hide ALL next time.
            self.settings.setValue(self.key, json.dumps(self.states, sort_keys=True))
            self.settings.sync()

    def close(self):
        if self.node is None:
            return
        self.flush()
        self.display.RemoveObserver(self.display_tag)
        self.segmentation.RemoveObserver(self.segment_tag)
        self.node = self.display = self.segmentation = None


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
        manager = PersonalVisibility(node, qt.QSettings(), scope,
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
