"""No image scanning, pointer calls or changed semantics on unsupported inputs."""
from types import SimpleNamespace

import pytest
from LiveSegmentationLib import native_range
from LiveSegmentationLib.native_range import NativeScalarRange, SourceRangeGuard, supported_profile


def image(*, kind=3, components=1, ghost=False, cell=False, name="vtkImageData"):
    scalars = SimpleNamespace(GetDataType=lambda: kind, GetNumberOfComponents=lambda: components,
                              HasStandardMemoryLayout=lambda: True, GetNumberOfTuples=lambda: 64)
    points = SimpleNamespace(GetScalars=lambda: scalars, GetArray=lambda name: object() if ghost else None)
    cells = SimpleNamespace(GetScalars=lambda: object() if cell else None, GetArray=lambda name: None)
    return SimpleNamespace(GetClassName=lambda: name, GetPointData=lambda: points,
                           GetCellData=lambda: cells, GetNumberOfPoints=lambda: 64,
                           GetAddressAsString=lambda name: "Addr=0x1234")


def test_exact_profile_only_not_merely_same_major_version():
    assert supported_profile("win32", 8, (5, 12, 0, "34621"), (9, 6, 2))
    assert supported_profile("win32", 8, (5, 12, 3, "34627"), (9, 6, 2))
    assert not supported_profile("win32", 8, (5, 12, 3, "34627"), (9, 6, 2), architecture="ARM64")
    for platform, size, slicer, vtk in [
        ("linux", 8, (5, 12, 3, "34627"), (9, 6, 2)),
        ("win32", 4, (5, 12, 3, "34627"), (9, 6, 2)),
        ("win32", 8, (5, 12, 3, "different"), (9, 6, 2)),
        ("win32", 8, (5, 12, 3, "34627"), (9, 6, 3)),
    ]:
        assert not supported_profile(platform, size, slicer, vtk)


def test_only_known_integer_image_uses_the_exported_method():
    calls = []
    backend = NativeScalarRange(calls.append, integer_types={3, 4})
    assert backend.prime(image())
    assert calls == [0x1234]


@pytest.mark.parametrize("kwargs", [
    {"kind": 10}, {"components": 2}, {"ghost": True}, {"cell": True},
    {"name": "vtkOrientedImageData"}, {"name": "CustomImage"},
])
def test_unsupported_inputs_never_call_into_native_code(kwargs):
    calls = []
    backend = NativeScalarRange(calls.append, integer_types={3, 4})
    assert not backend.prime(image(**kwargs))
    assert not calls


def test_pointer_string_is_validated_before_native_call():
    calls = []
    backend = NativeScalarRange(calls.append, integer_types={3})
    candidate = image()
    candidate.GetAddressAsString = lambda name: "untrusted/malformed"
    assert not backend.prime(candidate)
    assert not calls


def test_nonstandard_layout_or_wrong_tuple_count_is_not_primed():
    calls = []
    backend = NativeScalarRange(calls.append, integer_types={3})
    candidate = image()
    scalars = candidate.GetPointData().GetScalars()
    scalars.HasStandardMemoryLayout = lambda: False
    assert not backend.prime(candidate)
    scalars.HasStandardMemoryLayout = lambda: True
    scalars.GetNumberOfTuples = lambda: 2
    assert not backend.prime(candidate)
    assert not calls


class Events:
    NodeAddedEvent = "added"
    ImageDataModifiedEvent = "image"

    def __init__(self):
        self.tags = {}
        self.next_tag = 0
        self.source = None
        self.image = SimpleNamespace(revision=1, GetActualMemorySize=lambda: 1024*1024)
        self.image.GetMTime = lambda: self.image.revision

    def AddObserver(self, event, callback, priority=0):
        self.next_tag += 1
        self.tags[self.next_tag] = event, callback, priority
        return self.next_tag

    def RemoveObserver(self, tag):
        self.tags.pop(tag, None)

    def emit(self, event, *data):
        for tag, (kind, callback, _) in sorted(list(self.tags.items()), key=lambda item: -item[1][2]):
            if kind == event and tag in self.tags:
                callback(self, event, *data)

    def GetSourceVolumeNode(self):
        return self.source

    def GetImageData(self):
        return self.image

    def IsA(self, name):
        return name == "vtkMRMLSegmentEditorNode"

    def GetSingletonTag(self):
        return "SegmentEditor"


def guard_harness():
    parameter, scene, volume = Events(), Events(), Events()
    calls = []
    editor = SimpleNamespace(mrmlSegmentEditorNode=lambda: parameter)

    def calldata_type(kind):
        def decorate(function):
            function._calldata_type = kind  # A bound method cannot hold this.
            return function
        return decorate

    vtk = SimpleNamespace(vtkCommand=SimpleNamespace(ModifiedEvent="modified"),
                          VTK_OBJECT=1, calldata_type=calldata_type)
    backend = SimpleNamespace(prime=lambda image: calls.append(image.GetMTime()) or True)
    guard = SourceRangeGuard(editor, slicer_api=SimpleNamespace(mrmlScene=scene),
                             vtk_api=vtk, backend=backend)
    return guard, parameter, scene, volume, calls


def test_guard_prepares_before_native_source_observer():
    guard, parameter, _, volume, calls = guard_harness()
    order = []
    parameter.AddObserver("modified", lambda *args: order.append(list(calls)))
    parameter.source = volume
    parameter.emit("modified")
    assert order == [[1]] and calls == [1]
    parameter.emit("modified")  # Selected-label/name changes do not rescan source.
    assert calls == [1]
    volume.image.revision = 2
    volume.emit("image")
    assert calls == [1, 2]
    guard.cleanup()


def test_source_switch_detaches_old_image_observer_and_cleanup_is_scoped():
    guard, parameter, scene, volume, calls = guard_harness()
    native_tag = parameter.AddObserver("modified", lambda *args: None)
    parameter.source = volume
    parameter.emit("modified")
    new = Events()
    parameter.source = new
    parameter.emit("modified")
    before = len(calls)
    volume.image.revision = 3
    volume.emit("image")
    assert len(calls) == before
    guard.cleanup()
    guard.cleanup()
    assert list(parameter.tags) == [native_tag]
    assert not scene.tags and not new.tags and not volume.tags
    assert not guard.prepare(new)


def test_new_standard_parameter_after_scene_reset_is_observed():
    guard, old, scene, volume, calls = guard_harness()
    new = Events()
    scene.emit("added", new)
    assert not old.tags
    new.source = volume
    new.emit("modified")
    assert calls == [1]
    guard.cleanup()
    assert not new.tags


def test_image_replacement_at_same_volume_reprepares():
    guard, parameter, _, volume, calls = guard_harness()
    parameter.source = volume
    parameter.emit("modified")
    volume.image = Events().image  # Same MTime, distinct image object.
    volume.emit("image")
    assert calls == [1, 1]
    guard.cleanup()


def test_unknown_version_or_missing_library_never_loads_native_code(monkeypatch, tmp_path):
    loaded = []
    monkeypatch.setattr(native_range.ctypes, "CDLL", lambda p: loaded.append(p))
    app = SimpleNamespace(applicationVersion=lambda: "unrecognized")
    assert NativeScalarRange.from_slicer(SimpleNamespace(app=app), None)[0] is None
    app.applicationVersion = lambda: "5.12.3"
    app.revision = "34627"
    app.applicationDirPath = lambda: str(tmp_path)
    version = SimpleNamespace(GetVTKMajorVersion=lambda: 9, GetVTKMinorVersion=lambda: 6,
                              GetVTKBuildVersion=lambda: 2)
    vtk = SimpleNamespace(vtkVersion=version)
    for name in ("VTK_CHAR", "VTK_SIGNED_CHAR", "VTK_UNSIGNED_CHAR", "VTK_SHORT", "VTK_UNSIGNED_SHORT",
                 "VTK_INT", "VTK_UNSIGNED_INT", "VTK_LONG", "VTK_UNSIGNED_LONG", "VTK_LONG_LONG",
                 "VTK_UNSIGNED_LONG_LONG", "VTK_ID_TYPE"):
        setattr(vtk, name, 3)
    monkeypatch.setattr(native_range.sys, "platform", "win32")
    assert NativeScalarRange.from_slicer(SimpleNamespace(app=app), vtk)[0] is None
    assert not loaded


def test_backend_failure_never_caches_success_or_breaks_source_selection(caplog):
    guard, parameter, _, volume, calls = guard_harness()

    def broken(image):
        raise ValueError("injected backend failure")

    guard.backend.prime = broken
    parameter.source = volume
    parameter.emit("modified")
    assert guard.last_token is None and not guard.busy and not calls
    assert parameter.GetSourceVolumeNode() is volume
    assert "retaining native Slicer preparation" in caplog.text
    guard.cleanup()
