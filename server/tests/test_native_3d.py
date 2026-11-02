"""Collaboration must leave native surface controls and requested meshes intact."""
import ast
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def widget_methods(*names):
    path = Path(__file__).resolve().parents[2] / "LiveSegmentation/LiveSegmentation.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    original = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "LiveSegmentationWidget")
    original.bases, original.keywords = [], []
    original.body = [n for n in original.body if isinstance(n, ast.FunctionDef) and n.name in names]
    namespace = {"slicer": SimpleNamespace(util=SimpleNamespace(RenderBlocker=nullcontext)),
                 "vtkSegmentationCore": SimpleNamespace(vtkSegmentationConverter=SimpleNamespace(
                     GetSegmentationBinaryLabelmapRepresentationName=lambda: "Binary labelmap"))}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[original], type_ignores=[])), str(path), "exec"), namespace)
    return namespace["LiveSegmentationWidget"]


def test_join_and_leave_do_not_bind_a_native_3d_override():
    cls = widget_methods("set_live_session_active")
    owner = cls()
    owner.surface_preview = Mock()
    owner.open_segment_editor_button = Mock()
    owner._standard_segment_editor_widget = Mock(return_value=Mock())
    owner.get_segmentation_node = Mock(return_value=object())
    for method in ("_enable_exclusive_segment_editing", "_guard_segment_editor_undo",
                   "_restore_segment_editor_overwrite_mode", "_restore_segment_editor_undo"):
        setattr(owner, method, Mock())
    owner.set_live_session_active(True)
    owner.set_live_session_active(False)
    owner.surface_preview.bind.assert_not_called()


@pytest.mark.parametrize("surface_exists", [True, False])
def test_checkpoint_pointer_swap_preserves_user_requested_native_surfaces(surface_exists):
    cls = widget_methods("commit_checkpoint_segments")
    cls._reference_ijk_to_segmentation_ras_matrix = Mock(return_value="matrix")
    cls._matrices_match = Mock(return_value=True)
    old, image, volume = object(), object(), object()
    segment = Mock()
    segment.GetRepresentation.return_value = old
    segmentation = Mock()
    segmentation.GetSegment.return_value = segment
    segmentation.ContainsRepresentation.return_value = surface_exists
    node = Mock()
    node.GetSegmentation.return_value = segmentation
    stage = dict(segment_id="A", segment=segment, old_image=old, old_revision=None,
                 volume=volume, matrix="matrix", image=image)
    assert cls.commit_checkpoint_segments(node, [stage])
    if surface_exists:
        segmentation.CreateRepresentation.assert_called_once_with("Closed surface")
    else:
        segmentation.CreateRepresentation.assert_not_called()
