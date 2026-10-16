"""Opening the standard editor must not align a source for inactive Threshold."""
from types import SimpleNamespace

from LiveSegmentationLib.editor_preprocessing import (
    EditorPreprocessingGuard,
    InactiveThresholdSource,
)


def test_inactive_threshold_does_not_request_source_image():
    calls = []
    effect = SimpleNamespace(active=lambda: False,
                             sourceVolumeImageData=lambda: calls.append("aligned"))
    assert InactiveThresholdSource(effect).sourceVolumeImageData() is None
    assert calls == []


def test_active_threshold_uses_unmodified_native_image_and_parameters():
    image = object()
    effect = SimpleNamespace(active=lambda: True, sourceVolumeImageData=lambda: image,
                             doubleParameter=lambda name: 42, selectEffect=lambda name: name)
    proxy = InactiveThresholdSource(effect)
    assert proxy.sourceVolumeImageData() is image
    assert proxy.doubleParameter("MaximumThreshold") == 42
    assert proxy.selectEffect("Paint") == "Paint"


def test_every_request_rechecks_activation():
    effect = SimpleNamespace(enabled=False, sourceVolumeImageData=lambda: "native")
    effect.active = lambda: effect.enabled
    proxy = InactiveThresholdSource(effect)
    assert proxy.sourceVolumeImageData() is None
    effect.enabled = True
    assert proxy.sourceVolumeImageData() == "native"
    effect.enabled = False
    assert proxy.sourceVolumeImageData() is None


def builtin():
    threshold = type("SegmentEditorThresholdEffect", (), {})()
    effect = SimpleNamespace(active=lambda: False, self=lambda: threshold)
    threshold.scriptedEffect = effect
    return threshold, effect, SimpleNamespace(effectByName=lambda name: effect)


def test_only_the_one_standard_effect_instance_is_adapted_and_restored():
    threshold, original, editor = builtin()
    other, other_original, _ = builtin()
    guard = EditorPreprocessingGuard(editor)
    assert isinstance(threshold.scriptedEffect, InactiveThresholdSource)
    assert other.scriptedEffect is other_original
    guard.cleanup()
    guard.cleanup()
    assert threshold.scriptedEffect is original


def test_unknown_or_missing_effect_is_left_untouched():
    EditorPreprocessingGuard(SimpleNamespace(effectByName=lambda name: None)).cleanup()
    threshold, original, editor = builtin()
    threshold.__class__ = type("CustomThresholdEffect", (), {})
    guard = EditorPreprocessingGuard(editor)
    assert threshold.scriptedEffect is original
    guard.cleanup()


def test_cleanup_does_not_clobber_another_extension_accessor():
    threshold, _, editor = builtin()
    guard = EditorPreprocessingGuard(editor)
    threshold.scriptedEffect = another = object()
    guard.cleanup()
    assert threshold.scriptedEffect is another


def test_installing_twice_does_not_stack_adapters():
    threshold, original, editor = builtin()
    first = EditorPreprocessingGuard(editor)
    adapter = threshold.scriptedEffect
    second = EditorPreprocessingGuard(editor)
    assert threshold.scriptedEffect is adapter
    second.cleanup()
    assert threshold.scriptedEffect is adapter
    first.cleanup()
    assert threshold.scriptedEffect is original
