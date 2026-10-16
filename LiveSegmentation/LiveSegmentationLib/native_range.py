"""Version-gated native range preparation for Slicer's integer source images.

VTK 9.6.2's vtkImageData override visits every point through GetComponent, even
without blanking. The exported vtkDataSet implementation uses the optimized
array reductions and maintains the SAME image-owned cache and revision rules.
This bridge explicitly calls that implementation, never writes private fields,
patches machine code, substitutes pixels, swaps images or changes geometry.

ComputeScalarRange is a protected C++ API, not a supported Python API. Its ABI
is therefore deliberately restricted to a tested Slicer/VTK/platform profile,
the installed application's own library, and a startup equivalence self-test.
Unknown profiles and images retain unmodified native Slicer behavior.
"""
from __future__ import annotations

import ctypes
import logging
import platform
import re
import sys
import time
from pathlib import Path


def supported_profile(platform_name, pointer_size, slicer_version, vtk_version, *, architecture="AMD64"):
    return (platform_name == "win32" and pointer_size == 8
            and architecture.lower() in {"amd64", "x86_64"}
            and tuple(slicer_version) == (5, 12, 3, "34627")
            and tuple(vtk_version) == (9, 6, 2))


class NativeScalarRange:
    def __init__(self, compute, *, integer_types, library=None):
        self.compute = compute
        self.integer_types = frozenset(integer_types)
        self.library = library  # Keep the exact library alive with its callable.
        self.calls = 0
        self.seconds = 0.0

    @classmethod
    def from_slicer(cls, slicer, vtk):
        app = slicer.app
        version_text = getattr(app, "applicationVersion", "")
        version_text = version_text() if callable(version_text) else version_text
        match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", str(version_text))
        if match is None:
            return None, "Slicer application version is not recognized"
        version = (*map(int, match.groups()), str(getattr(app, "revision", "")))
        vtk_version = (vtk.vtkVersion.GetVTKMajorVersion(), vtk.vtkVersion.GetVTKMinorVersion(),
                       vtk.vtkVersion.GetVTKBuildVersion())
        if not supported_profile(sys.platform, ctypes.sizeof(ctypes.c_void_p), version, vtk_version,
                                 architecture=platform.machine()):
            return None, "native range bridge is not validated for this Slicer/VTK/platform profile"
        directory = Path(app.applicationDirPath()).resolve()
        integer_types = {getattr(vtk, name) for name in (
            "VTK_CHAR", "VTK_SIGNED_CHAR", "VTK_UNSIGNED_CHAR", "VTK_SHORT", "VTK_UNSIGNED_SHORT",
            "VTK_INT", "VTK_UNSIGNED_INT", "VTK_LONG", "VTK_UNSIGNED_LONG", "VTK_LONG_LONG",
            "VTK_UNSIGNED_LONG_LONG", "VTK_ID_TYPE")}
        for name in ("vtkCommon-9.6.dll", "vtkCommonDataModel-9.6.dll"):
            path = (directory / name).resolve()
            if path.parent != directory or not path.is_file():
                continue
            try:
                library = ctypes.CDLL(str(path))
                # MSVC x64: protected virtual void vtkDataSet::ComputeScalarRange().
                # Explicit non-virtual call; x86 thiscall/other ABIs are not used.
                compute = getattr(library, "?ComputeScalarRange@vtkDataSet@@MEAAXXZ")
                compute.argtypes = [ctypes.c_void_p]
                compute.restype = None
                backend = cls(compute, integer_types=integer_types, library=library)
                backend._self_test(vtk)
                backend.calls = 0
                backend.seconds = 0.0
                return backend, "validated Windows x64 / Slicer 5.12.3 r34627 / VTK 9.6.2"
            except (OSError, AttributeError, ValueError, AssertionError) as exc:
                return None, f"native range bridge validation failed ({type(exc).__name__})"
        return None, "validated application's VTK library was not found"

    def prime(self, image):
        if image is None or image.GetClassName() != "vtkImageData":
            return False
        points, cells = image.GetPointData(), image.GetCellData()
        scalars = points.GetScalars()
        if (scalars is None or scalars.GetDataType() not in self.integer_types
                or scalars.GetNumberOfComponents() != 1
                or not scalars.HasStandardMemoryLayout()
                or scalars.GetNumberOfTuples() != image.GetNumberOfPoints()
                or image.GetNumberOfPoints() == 0
                or cells.GetScalars() is not None
                or points.GetArray("vtkGhostType") is not None
                or cells.GetArray("vtkGhostType") is not None):
            return False
        address = re.fullmatch(r"Addr=(?:0x)?([0-9a-fA-F]+)",
                               image.GetAddressAsString("vtkDataSet").strip())
        if address is None:
            return False
        pointer = int(address.group(1), 16)
        if not 0 < pointer < 2 ** (8 * ctypes.sizeof(ctypes.c_void_p)):
            return False
        start = time.monotonic()
        self.compute(pointer)
        self.calls += 1
        self.seconds += time.monotonic()-start
        return True

    def _self_test(self, vtk):
        for values in ((0, 0), (-9, -3), (-9, 23), (0, 32767)):
            image = vtk.vtkImageData()
            image.SetDimensions(4, 4, 4)
            image.AllocateScalars(vtk.VTK_SHORT, 1)
            scalars = image.GetPointData().GetScalars()
            scalars.FillComponent(0, values[0])
            scalars.SetValue(7, values[1])
            scalars.Modified()
            reference = vtk.vtkImageData()
            reference.DeepCopy(image)  # Only 128 bytes, never a real source image.
            expected = reference.GetScalarRange()
            revision = image.GetMTime()
            if (not self.prime(image) or image.GetScalarRange() != expected
                    or image.GetMTime() != revision):
                raise ValueError("Native range bridge changed range/revision semantics")
            scalars.SetValue(7, 30000)
            scalars.Modified()
            if not self.prime(image) or image.GetScalarRange()[1] != 30000:
                raise ValueError("Native range bridge did not invalidate modified scalars")


class SourceRangeGuard:
    """Prime before native source slots, including manual navigation and edits."""

    def __init__(self, editor, *, slicer_api=None, vtk_api=None, backend=None):
        if slicer_api is None:
            import slicer as slicer_api
        if vtk_api is None:
            import vtk as vtk_api
        self.editor, self.scene = editor, slicer_api.mrmlScene
        self.closed = self.busy = self.warned = False
        self.parameter = self.parameter_tag = self.source = self.source_tag = None
        self.last_token = None
        self.backend, self.reason = ((backend, "injected test backend") if backend is not None
                                     else NativeScalarRange.from_slicer(slicer_api, vtk_api))
        self.modified_event = vtk_api.vtkCommand.ModifiedEvent
        def node_added(caller, event, node):
            self._node_added(caller, event, node)

        self.node_callback = vtk_api.calldata_type(vtk_api.VTK_OBJECT)(node_added)
        self.scene_tag = self.scene.AddObserver(self.scene.NodeAddedEvent, self.node_callback, 10.0)
        self._attach_parameter(editor.mrmlSegmentEditorNode())

    def _attach_parameter(self, parameter):
        if parameter == self.parameter:
            return
        if self.parameter is not None and self.parameter_tag is not None:
            self.parameter.RemoveObserver(self.parameter_tag)
        self.parameter, self.parameter_tag = parameter, None
        if parameter is not None:
            self.parameter_tag = parameter.AddObserver(self.modified_event, self._parameter_changed, 10.0)
        self._parameter_changed()

    def _node_added(self, caller, event, node):
        del caller, event
        if (not self.closed and node is not None and node.IsA("vtkMRMLSegmentEditorNode")
                and node.GetSingletonTag() == "SegmentEditor"):
            self._attach_parameter(node)

    def _parameter_changed(self, caller=None, event=None):
        del caller, event
        if self.closed or self.busy:
            return
        volume = self.parameter.GetSourceVolumeNode() if self.parameter is not None else None
        if volume != self.source:
            if self.source is not None and self.source_tag is not None:
                self.source.RemoveObserver(self.source_tag)
            self.source, self.source_tag = volume, None
            self.last_token = None
            if volume is not None:
                self.source_tag = volume.AddObserver(volume.ImageDataModifiedEvent, self._source_changed, 10.0)
        self.prepare(volume)

    def _source_changed(self, caller=None, event=None):
        del caller, event
        self.prepare(self.source)

    def prepare(self, volume):
        if self.closed or self.busy or volume is None:
            return False
        image = volume.GetImageData()
        if image is None:
            return False
        token = (image, image.GetMTime())
        if token == self.last_token:
            return True
        self.busy = True
        try:
            if self.backend is not None and self.backend.prime(image):
                self.last_token = (image, image.GetMTime())
                return True
            if not self.warned and image.GetActualMemorySize()*1024 >= 64*1024*1024:
                logging.warning("Live Segmentation: source range fast path unavailable for this image; native preparation retained (%s)", self.reason)
                self.warned = True
        except Exception:
            logging.exception("Source range acceleration failed; retaining native Slicer preparation")
        finally:
            self.busy = False
        return False

    def cleanup(self):
        if self.closed:
            return
        self.closed = True
        self.scene.RemoveObserver(self.scene_tag)
        if self.parameter is not None and self.parameter_tag is not None:
            self.parameter.RemoveObserver(self.parameter_tag)
        if self.source is not None and self.source_tag is not None:
            self.source.RemoveObserver(self.source_tag)
        self.parameter = self.source = self.last_token = self.editor = None
