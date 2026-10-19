"""Local, explicitly approximate 3D previews; authoritative labelmaps stay intact.

MRML and input VTK objects are read only on the GUI thread. Workers read a pinned
NumPy view and own all of their VTK pipeline/output objects. One job at a time;
results are published only if the native source revision is still current.
"""
from __future__ import annotations

import math
import queue
import threading
import time
import traceback

import numpy as np


def preview_mask(array, label_value, maximum_axis=128):
    """Occupancy pooling retains thin components, using bounded slab scratch."""
    shape = tuple(int(v) for v in array.shape)
    if len(shape) != 3 or min(shape) <= 0:
        raise ValueError("Preview requires a non-empty 3D labelmap")
    factor = max(1, math.ceil(max(shape)/maximum_axis))
    return _pool_at_factor(array, label_value, factor), factor


def _pool_at_factor(array, label_value, factor):
    shape = tuple(int(v) for v in array.shape)
    target_shape = tuple(math.ceil(v/factor) for v in shape)
    result = np.zeros(target_shape, np.uint8)
    # A slab may be wider than the scratch target; never copy the full source.
    for z in range(target_shape[0]):
        planar = np.zeros(shape[1:], bool)
        depth = max(1, (1024*1024)//max(1, shape[1]*shape[2]))
        for start in range(z*factor, min((z+1)*factor, shape[0]), depth):
            end = min(start+depth, (z+1)*factor, shape[0])
            planar |= np.any(array[start:end] == label_value, axis=0)
        padded = np.zeros((target_shape[1]*factor, target_shape[2]*factor), bool)
        padded[:shape[1], :shape[2]] = planar
        result[z] = padded.reshape(target_shape[1], factor, target_shape[2], factor).any(axis=(1, 3))
    return result


def update_preview_mask(array, label_value, previous, factor, dirty_bounds):
    """Recompute whole pooling cells, including erasures; never mutate cache."""
    result = previous.copy()
    scanned = 0
    for bounds in dirty_bounds:
        cells = [(max(0, int(bounds[2*i])//factor),
                  min(result.shape[i], math.ceil(int(bounds[2*i+1])/factor))) for i in range(3)]
        if any(hi <= lo for lo, hi in cells):
            continue
        source_slices = tuple(slice(lo*factor, min(hi*factor, array.shape[i])) for i, (lo, hi) in enumerate(cells))
        region = array[source_slices]
        scanned += int(np.prod(region.shape))
        result[tuple(slice(lo, hi) for lo, hi in cells)] = _pool_at_factor(region, label_value, factor)
    return result, scanned


def build_preview_mesh(array, label_value, extent, matrix, previous=None, dirty_bounds=None):
    """Only private VTK objects, never an MRML node or input image, in a worker."""
    import vtk
    from vtk.util.numpy_support import numpy_to_vtk

    started = time.monotonic()
    if previous is not None and dirty_bounds:
        factor = previous["factor"]
        mask, scanned = update_preview_mask(array, label_value, previous["mask"], factor, dirty_bounds)
    else:
        mask, factor = preview_mask(array, label_value)
        scanned = int(np.prod(array.shape))
    # Padding closes surfaces at the boundary of a material's native extent.
    padded = np.pad(mask, 1)
    image = vtk.vtkImageData()
    image.SetDimensions(*reversed(padded.shape))
    image.SetSpacing(factor, factor, factor)
    image.SetOrigin(*(extent[2*axis]+(factor-1)/2-factor for axis in range(3)))
    image.GetPointData().SetScalars(numpy_to_vtk(padded.reshape(-1), deep=True))
    edges = vtk.vtkDiscreteFlyingEdges3D()
    edges.SetInputData(image)
    edges.SetValue(0, 1)
    edges.Update()
    output = edges.GetOutput()
    if output.GetNumberOfPolys() > 150000:
        reduction = vtk.vtkQuadricDecimation()
        reduction.SetInputData(output)
        reduction.SetTargetReduction(1-150000/output.GetNumberOfPolys())
        reduction.Update()
        output = reduction.GetOutput()
    transform = vtk.vtkTransform()
    transform.SetMatrix(np.asarray(matrix).reshape(-1).tolist())
    to_ras = vtk.vtkTransformPolyDataFilter()
    to_ras.SetInputData(output)
    to_ras.SetTransform(transform)
    to_ras.Update()
    mesh = vtk.vtkPolyData()
    mesh.DeepCopy(to_ras.GetOutput())
    return {"mesh": mesh, "factor": factor, "polygons": mesh.GetNumberOfPolys(),
            "seconds": time.monotonic()-started, "mask": mask,
            "scanned_voxels": scanned, "incremental": bool(previous is not None and dirty_bounds)}


class SurfacePreview:
    """A room-local view, excluded from scene saves and from label exports."""
    def __init__(self, owner, action):
        import qt
        import slicer

        self.owner, self.action = owner, action
        self.node = None
        self.active = False
        self.generation = 0
        self.models = {}
        self.revisions = {}
        self.pooled = {}
        self.region_hints = {}
        self.wanted = {}
        self.editors = []
        self.buttons = []
        self.proxies = []
        self.results = queue.SimpleQueue()
        self.job = None
        self.last_editor_scan = 0.0
        self.visibility_3d = True
        self.timer = qt.QTimer()
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._on_timer)
        self.scene_tag = slicer.mrmlScene.AddObserver(slicer.vtkMRMLScene.StartCloseEvent, self._scene_closing)
        action.toggled.connect(self.set_active)
        action.setEnabled(False)

    def log(self, event, **details):
        self.owner.live_collaboration._diagnostic_event(event, **details)

    def _scene_closing(self, *unused):
        self.bind(None)

    def bind(self, node):
        if node == self.node:
            return
        self.set_active(False)
        self._restore_editors()
        self.generation += 1
        self.node = node
        self.wanted.clear()
        self.revisions.clear()
        self.pooled.clear()
        self.region_hints.clear()
        self.action.setEnabled(node is not None)
        self.action.setChecked(False)
        if node is None:
            self.timer.stop()
            return
        self.timer.start()
        self.scan_editors()
        if node.GetSegmentation().ContainsRepresentation("Closed surface"):
            # Existing derived surfaces must not make subsequent edits trigger
            # a full synchronous reconversion before the user opens the preview.
            self.action.setChecked(True)

    def set_active(self, checked):
        import slicer

        checked = bool(checked) and self.node is not None
        if checked == self.active:
            return
        self.generation += 1
        self.active = checked
        self.action.setText("3D preview · updating" if checked else "3D preview")
        if not checked:
            for model in self.models.values():
                if model.GetScene() == slicer.mrmlScene:
                    slicer.mrmlScene.RemoveNode(model)
            self.models.clear()
            self.revisions.clear()
            self.pooled.clear()
            self.region_hints.clear()
            if self.node is not None and self.node.GetDisplayNode() is not None:
                self.node.GetDisplayNode().SetVisibility3D(self.visibility_3d)
            return
        if self.node.GetSegmentation().GetSourceRepresentationName() != "Binary labelmap":
            self.active = False
            self.action.setText("3D preview")
            self.action.setChecked(False)
            self.owner.live_collaboration._show_error("3D preview requires binary-labelmap source data; labels were left unchanged")
            return
        display = self.node.GetDisplayNode()
        self.visibility_3d = bool(display.GetVisibility3D()) if display is not None else True
        if display is not None:
            display.SetVisibility3D(False)
        # Meshes are derived data only. Without them native ROI writers cannot
        # start another full-resolution conversion on the GUI thread.
        self.node.RemoveClosedSurfaceRepresentation()
        self.log("preview.enabled", approximate=True)

    @staticmethod
    def revision(segment):
        image = segment.GetRepresentation("Binary labelmap")
        if image is None or image.GetPointData().GetScalars() is None:
            return None
        extent = tuple(image.GetExtent())
        if any(extent[2*i+1] < extent[2*i] for i in range(3)):
            # Native empty materials may retain a zero-tuple scalar allocation.
            # They are not an error and must never disable every other mesh.
            return None
        return (str(image.GetAddressAsString("")), image.GetMTime(),
                image.GetPointData().GetScalars().GetMTime(), segment.GetLabelValue(), extent)

    def note_reference_region(self, sid, bounds, volume, before):
        """Accept hints only across a proven revision chain and identical geometry."""
        if not self.active or sid not in self.pooled or self.node is None:
            return
        import vtk

        segment = self.node.GetSegmentation().GetSegment(sid)
        after = self.revision(segment) if segment is not None else None
        if before is None or after is None or (before[0], before[3:]) != (after[0], after[3:]):
            self.region_hints.pop(sid, None)
            return
        geometry = self.owner._reference_ijk_to_segmentation_ras_matrix(volume, self.node)
        native = vtk.vtkMatrix4x4()
        segment.GetRepresentation("Binary labelmap").GetImageToWorldMatrix(native)
        if geometry is None or not self.owner._matrices_match(native, geometry):
            self.region_hints.pop(sid, None)
            return
        hint = self.region_hints.get(sid)
        base = self.pooled[sid]["revision"]
        if (hint["after"] if hint else base) != before:
            self.region_hints.pop(sid, None)
            return
        extent = after[4]
        native_bounds = [int(bounds[i])-extent[(2-i//2)*2] for i in range(6)]
        if hint is None:
            hint = self.region_hints[sid] = {"before": base, "after": after, "bounds": []}
        hint["after"] = after
        hint["bounds"].append(native_bounds)
        # Many edits without a completed preview are safely bounded by one union.
        if len(hint["bounds"]) > 64:
            hint["bounds"] = [[(min if i % 2 == 0 else max)(box[i] for box in hint["bounds"]) for i in range(6)]]

    def scan_editors(self):
        import slicer

        self.last_editor_scan = time.monotonic()
        for editor, visible in list(self.editors):
            try:
                if editor.segmentationNode() == self.node:
                    continue
                editor.setShow3DButtonVisible(visible)
                self._remove_preview_button(slicer.util.findChild(editor, "Show3DButton"))
            except RuntimeError:
                pass
            self.editors.remove((editor, visible))
        for button, visible in list(self.buttons):
            try:
                if button.segmentationNode() == self.node:
                    continue
                button.setVisible(visible)
                self._remove_preview_button(button)
            except RuntimeError:
                pass
            self.buttons.remove((button, visible))
        try:
            editors = slicer.util.findChildren(className="qMRMLSegmentEditorWidget")
        except RuntimeError:
            return
        # Slicer's cached module widget can be outside the main-window tree
        # until it is first shown. Live already owns this existing editor.
        standard = getattr(self.owner, "_recovery_editor", None)
        if standard is not None and not any(editor == standard for editor in editors):
            editors.append(standard)
        for editor in editors:
            if editor.segmentationNode() != self.node or any(editor == item[0] for item in self.editors):
                continue
            # Public reversible API. Use the global preview action in any module
            # instead of the synchronous native Show 3D button while in a room.
            button = slicer.util.findChild(editor, "Show3DButton")
            # isVisible() includes hidden module ancestors; isHidden() records
            # the button's own state and can be restored after module changes.
            was_visible = not button.isHidden()
            self.editors.append((editor, was_visible))
            self._add_preview_button(button)
            editor.setShow3DButtonVisible(False)
        for button in slicer.util.findChildren(className="qMRMLSegmentationShow3DButton"):
            if any(button == slicer.util.findChild(editor, "Show3DButton") for editor, _ in self.editors):
                continue
            if button.segmentationNode() == self.node and not any(button == entry[0] for entry in self.buttons):
                self.buttons.append((button, not button.isHidden()))
                self._add_preview_button(button)
                button.setVisible(False)

    def _add_preview_button(self, original):
        import qt

        proxy = qt.QToolButton(original.parentWidget())
        proxy.setObjectName("LiveSegmentationShow3DPreviewButton")
        proxy.setDefaultAction(self.action)
        proxy.setToolButtonStyle(qt.Qt.ToolButtonTextBesideIcon)
        original.parentWidget().layout().addWidget(proxy)
        proxy.show()
        self.proxies.append((original, proxy))

    def _remove_preview_button(self, original):
        for source, proxy in list(self.proxies):
            if source == original:
                proxy.hide()
                proxy.deleteLater()
                self.proxies.remove((source, proxy))

    def _restore_editors(self):
        for original, _ in list(self.proxies):
            self._remove_preview_button(original)
        for button, visible in self.buttons:
            try:
                button.setVisible(visible)
            except RuntimeError:
                pass
        self.buttons.clear()
        for editor, visible in self.editors:
            try:
                editor.setShow3DButtonVisible(visible)
            except RuntimeError:
                pass
        self.editors.clear()

    def _on_timer(self):
        try:
            self.tick()
        except Exception as exc:
            self.log("preview.error", message=traceback.format_exc())
            self.bind(None)
            self.owner.live_collaboration._show_error(f"3D preview stopped safely: {exc}; labels were not changed")

    def tick(self):
        import slicer

        if self.node is None:
            return
        if self.node.GetScene() != slicer.mrmlScene:
            self.bind(None)
            return
        if time.monotonic()-self.last_editor_scan >= .5:
            self.scan_editors()
        self._drain()
        if not self.active:
            return
        segmentation = self.node.GetSegmentation()
        # External tools may explicitly create a full surface while in a room.
        # Retain only the lightweight model preview; 2D labelmaps are untouched.
        if segmentation.ContainsRepresentation("Closed surface"):
            self.node.RemoveClosedSurfaceRepresentation()
        ids = set(segmentation.GetSegmentIDs())
        for sid in list(self.models):
            if sid not in ids:
                slicer.mrmlScene.RemoveNode(self.models.pop(sid))
                self.revisions.pop(sid, None)
                self.wanted.pop(sid, None)
                self.pooled.pop(sid, None)
                self.region_hints.pop(sid, None)
        now = time.monotonic()
        for sid in ids:
            segment = segmentation.GetSegment(sid)
            token = self.revision(segment)
            if token is None and sid in self.models:
                # An empty/cleared material can have no scalar allocation.
                # Never leave its previous surface looking like current data.
                slicer.mrmlScene.RemoveNode(self.models.pop(sid))
                self.revisions.pop(sid, None)
                self.pooled.pop(sid, None)
                self.region_hints.pop(sid, None)
            previous = self.wanted.get(sid)
            if previous is None or previous[0] != token:
                self.wanted[sid] = (token, now)
            self._style(sid)
        if self.job is not None:
            return
        pending = sorted((stamp, sid, token) for sid, (token, stamp) in self.wanted.items()
                         if sid in ids and token is not None and self.revisions.get(sid) != token)
        if not pending:
            self.action.setText("3D preview")
            return
        stamp, sid, token = pending[0]
        if now-stamp < .25:
            return
        self.action.setText("3D preview · updating")
        segment = segmentation.GetSegment(sid)
        image = segment.GetRepresentation("Binary labelmap")
        import vtk

        matrix = vtk.vtkMatrix4x4()
        image.GetImageToWorldMatrix(matrix)
        coefficients = [[matrix.GetElement(r, c) for c in range(4)] for r in range(4)]
        array = slicer.util.arrayFromSegmentInternalBinaryLabelmap(self.node, sid)
        job = {"generation": self.generation, "sid": sid, "revision": token}
        previous = self.pooled.get(sid)
        hint = self.region_hints.get(sid)
        if (previous is None or hint is None or hint["before"] != previous["revision"]
                or hint["after"] != token or previous["matrix"] != coefficients):
            previous, bounds = None, None
        else:
            bounds = [list(box) for box in hint["bounds"]]
        job["matrix"] = coefficients
        self.job = job
        self.log("preview.request", segment_id=sid, source_shape=list(array.shape))
        # pin survives room leave/scene close while a worker still reads its view.
        def worker(_pin=image, view=array, label=int(segment.GetLabelValue()), extent=tuple(image.GetExtent())):
            try:
                result = build_preview_mesh(view, label, extent, coefficients, previous, bounds)
            except Exception as exc:
                result = {"error": str(exc)}
            self.results.put((job, result))
        threading.Thread(target=worker, name="LiveSegmentation-3D-preview", daemon=True).start()

    def _drain(self):
        import slicer

        while not self.results.empty():
            job, result = self.results.get()
            if self.job is job:
                self.job = None
            if not self.active or job["generation"] != self.generation:
                continue
            sid = job["sid"]
            segment = self.node.GetSegmentation().GetSegment(sid)
            if segment is None or self.revision(segment) != job["revision"]:
                self.log("preview.stale", segment_id=sid)
                continue
            if "error" in result:
                self.owner.live_collaboration._show_error(f"3D preview failed for {sid}: {result['error']}")
                self.action.setChecked(False)
                continue
            model = self.models.get(sid)
            if model is None:
                model = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelNode", "Live 3D PREVIEW")
                model.SetHideFromEditors(True)
                model.SetSaveWithScene(False)
                model.SetAttribute("LiveSegmentation.ApproximatePreview", "1")
                model.CreateDefaultDisplayNodes()
                self.models[sid] = model
            model.SetAndObservePolyData(result["mesh"])
            self.revisions[sid] = job["revision"]
            self.pooled[sid] = {"revision": job["revision"], "matrix": job["matrix"],
                                "factor": result["factor"], "mask": result["mask"],
                                "scanned_voxels": result["scanned_voxels"], "incremental": result["incremental"],
                                "ready_at": time.monotonic()}
            hint = self.region_hints.get(sid)
            if hint is not None and hint["after"] == job["revision"]:
                self.region_hints.pop(sid, None)
            self._style(sid)
            self.log("preview.ready", segment_id=sid, pooling_factor=result["factor"],
                     polygons=result["polygons"], duration_s=result["seconds"],
                     scanned_voxels=result["scanned_voxels"], incremental=result["incremental"])

    def _style(self, sid):
        model = self.models.get(sid)
        if model is None:
            return
        segment = self.node.GetSegmentation().GetSegment(sid)
        if segment is None:
            return
        display = model.GetDisplayNode()
        source_display = self.node.GetDisplayNode()
        display.SetColor(*segment.GetColor())
        display.SetVisibility(bool(source_display and source_display.GetVisibility()
                                   and source_display.GetSegmentVisibility(sid)
                                   and source_display.GetSegmentVisibility3D(sid)))
        display.SetBackfaceCulling(False)
        display.SetOpacity(source_display.GetOpacity3D()*source_display.GetSegmentOpacity3D(sid))
        if model.GetTransformNodeID() != self.node.GetTransformNodeID():
            model.SetAndObserveTransformNodeID(self.node.GetTransformNodeID())

    def cleanup(self):
        import slicer

        self.bind(None)
        self.timer.stop()
        self.timer.deleteLater()
        slicer.mrmlScene.RemoveObserver(self.scene_tag)

