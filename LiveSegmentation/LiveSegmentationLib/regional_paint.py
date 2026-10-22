"""Bounded local ownership for native Paint, without changing its brush rasterizer.

Slicer 5.12's global overwrite pads even distant independent layers to the
brush extent before erasing them. The room can instead resolve just the newly
claimed voxels after native callbacks finish. Confirmed baselines are NOT
changed here: ordinary ordered operations still decide shared ownership.
"""
import numpy as np


class NativePaintGuard:
    """Scope the workaround to large live-room Paint/Everywhere editing only.

    Other effects, source-intensity masks, custom editable-area geometries and
    standalone Segment Editor keep Slicer's original implementation.
    """
    def __init__(self, owner, editor):
        self.owner, self.editor = owner, editor
        self.parameter = editor.mrmlSegmentEditorNode()
        self.previous = self.parameter.GetOverwriteMode()
        self.active = self.updating = self.closed = False
        self.observer = self.parameter.AddObserver("ModifiedEvent", self.refresh)
        self.refresh()

    def supports_mask(self, mask_mode):
        live = self.owner.live_collaboration
        node = self.editor.segmentationNode()
        volume = self.editor.sourceVolumeNode()
        image = volume.GetImageData() if volume is not None else None
        mapping = getattr(self.owner, "_reference_ijk_to_segmentation_ras_matrix", None)
        linear = not callable(mapping) or (node is not None and volume is not None and mapping(volume, node) is not None)
        return bool(
            live is not None and live.connected and live._segmentation_node() == node
            and node is not None and node.GetSegmentation().GetNumberOfSegments() >= 8
            and image is not None and image.GetNumberOfPoints() >= 8_000_000
            and self.parameter.GetActiveEffectName() == "Paint"
            and linear and mask_mode == node.EditAllowedEverywhere
            and not self.parameter.GetSourceVolumeIntensityMask())

    def refresh(self, *unused):
        if self.closed or self.updating:
            return
        self.updating = True
        try:
            was_active = self.active
            self.active = self.supports_mask(self.parameter.GetMaskMode())
            if self.active:
                if not was_active:
                    # Live rooms enforce a material partition outside this
                    # scoped Paint path too. IR's temporary OverwriteNone must
                    # not leak into subsequent unmasked native effects.
                    self.previous = self.parameter.OverwriteAllSegments
                if self.parameter.GetOverwriteMode() != self.parameter.OverwriteNone:
                    self.parameter.SetOverwriteMode(self.parameter.OverwriteNone)
            elif was_active:
                node = self.editor.segmentationNode()
                # IR's lock guard may have just taken ownership by selecting
                # OutsideAll/OverwriteNone. Never overwrite another safeguard.
                outside = getattr(node, "EditAllowedOutsideAllSegments", 3)
                if self.parameter.GetMaskMode() != outside:
                    self.parameter.SetOverwriteMode(self.previous)
        finally:
            self.updating = False

    def target(self, node):
        self.refresh()
        return self.editor.currentSegmentID() if self.active and self.editor.segmentationNode() == node else None

    def protects_editor(self, editor, node, original_mask_mode):
        # IR and the standard editor can be TWO views of ONE parameter node.
        # Match the editing context, not the PythonQt widget identity; otherwise
        # the other view's lock rows immediately undo our regional policy.
        return bool(not self.closed and self.parameter == editor.mrmlSegmentEditorNode()
                    and self.editor.segmentationNode() == node and editor.segmentationNode() == node
                    and self.supports_mask(original_mask_mode))

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.parameter.RemoveObserver(self.observer)
        if self.active:
            self.parameter.SetOverwriteMode(self.previous)


def apply_claims(controller, node, segment_id):
    if controller._current_role() == "viewer" or controller._locked_by_other(segment_id):
        return set()
    crop, bounds = controller._read_mask_crop(node, segment_id)
    if crop is None or bounds is None:
        return set()
    baseline = controller.baselines.get((node.GetID(), segment_id))
    before = (baseline.region(bounds) if baseline is not None else np.zeros_like(crop))
    expected = controller._visible_pending_region(before, bounds, segment_id)
    claims = (crop != 0) & (expected == 0)
    positions = np.where(claims)
    if not positions[0].size:
        return set()
    slices = tuple(slice(int(axis.min()), int(axis.max())+1) for axis in positions)
    region = [bounds[2*a]+value for a, part in enumerate(slices) for value in (part.start, part.stop)]
    claimed = claims[slices]
    current = np.ascontiguousarray(crop[slices])
    updater = controller.owner.update_segment_binary_labelmap_crop

    def write(sid, old, new):
        changed = np.where(old != new)
        if not changed[0].size:
            return True
        parts = tuple(slice(int(axis.min()), int(axis.max())+1) for axis in changed)
        box = [region[2*a]+value for a, part in enumerate(parts) for value in (part.start, part.stop)]
        return updater(old[parts], new[parts], box, node, sid, controller.owner.get_volume_node())

    applying = controller._applying_remote
    controller._applying_remote = True
    attempted = []
    try:
        allowed = claimed.copy()
        siblings = []
        for sid in node.GetSegmentation().GetSegmentIDs():
            if sid == segment_id:
                continue
            mask = controller._read_mask_region(node, sid, region)
            occupied = mask != 0
            if not np.any(occupied & claimed):
                continue
            if controller._locked_by_other(sid):
                allowed &= ~occupied
            else:
                siblings.append((sid, mask))
        changes = []
        target = current.copy()
        target[claimed & ~allowed] = 0
        if np.any(target != current):
            changes.append((segment_id, current, target))
        for sid, mask in siblings:
            after = mask.copy()
            after[allowed] = 0
            if np.any(after != mask):
                changes.append((sid, mask, after))
        for sid, old, new in changes:
            attempted.append((sid, old, new))
            if not write(sid, old, new):
                raise RuntimeError("Regional Paint could not safely update a touched label")
    except Exception:
        for sid, old, new in reversed(attempted):
            if not write(sid, new, old):
                raise RuntimeError("Regional Paint rollback failed; save local recovery before continuing") from None
        # Reject the new Paint claim too, rather than leave an overlap after a
        # failed sibling write. Existing target content/pending edits survive.
        rollback = current.copy()
        rollback[claimed] = 0
        if not write(segment_id, current, rollback):
            raise RuntimeError("Regional Paint target recovery failed; save local recovery") from None
        raise
    finally:
        controller._applying_remote = applying
    return {sid for sid, _, _ in changes}
