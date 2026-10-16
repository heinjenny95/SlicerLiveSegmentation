"""Avoid preparing an unused built-in tool in the standard Segment Editor."""
from __future__ import annotations


class InactiveThresholdSource:
    """Defer only inactive Threshold's full-source alignment.

    Slicer's C++ bridge caches bound Python callbacks, so replacing the Python
    sourceVolumeNodeChanged method after widget creation has no effect. Adapt
    only this effect instance's Python accessor instead. Active Threshold and
    every other method delegate to the original C++ effect unchanged.
    """

    def __init__(self, original):
        self.original = original

    def __getattr__(self, name):
        return getattr(self.original, name)

    def sourceVolumeImageData(self):
        if not self.original.active():
            return None
        return self.original.sourceVolumeImageData()


class EditorPreprocessingGuard:
    """Reversible, instance-scoped adapter; no global patches or scene changes."""

    def __init__(self, editor):
        self.threshold = self.adapter = None
        effect = editor.effectByName("Threshold")
        if effect is None or not hasattr(effect, "self"):
            return
        threshold = effect.self()
        if (type(threshold).__name__ != "SegmentEditorThresholdEffect"
                or threshold.scriptedEffect != effect or not hasattr(effect, "active")):
            return  # Unknown/custom effect: retain its native behavior.
        self.threshold = threshold
        self.adapter = InactiveThresholdSource(effect)
        threshold.scriptedEffect = self.adapter

    def cleanup(self):
        if self.threshold is not None and self.threshold.scriptedEffect is self.adapter:
            self.threshold.scriptedEffect = self.adapter.original
        self.threshold = self.adapter = None
