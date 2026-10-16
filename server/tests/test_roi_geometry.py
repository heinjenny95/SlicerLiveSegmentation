"""ROI geometry must never require a complete reference-volume mask."""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest


def geometry():
    from LiveSegmentationLib import roi_geometry
    return roi_geometry


def test_integer_roi_offset_is_read_without_a_reference_volume():
    source = np.zeros((5, 6, 7), dtype=np.uint16)
    source[2, 3, 4] = 300
    transform = np.eye(4)
    transform[:3, 3] = [-101, -203, -307]
    bounds = [307, 312, 203, 209, 101, 108]
    result = geometry().sample_region(source, [0, 6, 0, 5, 0, 4], transform, bounds, 300)
    assert result.shape == source.shape and result.sum() == 1
    assert result[2, 3, 4] == 1


@pytest.mark.parametrize("offset", [(7, -4, 3), (-13, 9, -2), (0, 0, 0)])
def test_extent_and_label_value_are_preserved(offset):
    source = np.arange(60, dtype=np.uint8).reshape(3, 4, 5)
    transform = np.eye(4)
    transform[:3, 3] = offset
    extent = [-8, -4, 11, 14, 6, 8]
    x, y, z = offset
    bounds = [6-z, 9-z, 11-y, 15-y, -8-x, -3-x]
    mask = geometry().sample_region(source, extent, transform, bounds, 37)
    assert np.array_equal(mask, source == 37)


def test_affine_nearest_neighbor_matches_scalar_reference():
    rng = np.random.default_rng(37)
    source = rng.integers(0, 4, (7, 8, 9), dtype=np.uint8)
    matrix = np.array([[0.8, -0.2, 0.1, 2], [0.2, 1.1, 0, -1], [0, 0.1, 0.9, 3], [0, 0, 0, 1.]])
    extent = [-2, 6, 3, 10, -1, 5]
    bounds = [-5, 7, -2, 12, -7, 8]
    result = geometry().sample_region(source, extent, matrix, bounds, 2)
    expected = np.zeros_like(result)
    for z, y, x in np.ndindex(result.shape):
        point = matrix @ [x+bounds[4], y+bounds[2], z+bounds[0], 1]
        xi, yi, zi = np.floor(point[:3]+0.5+1e-10).astype(int) - [-2, 3, -1]
        if 0 <= zi < 7 and 0 <= yi < 8 and 0 <= xi < 9:
            expected[z, y, x] = source[zi, yi, xi] == 2
    assert np.array_equal(result, expected)


def test_large_reference_dimensions_only_clip_roi_bounds():
    transform = np.eye(4)
    transform[:3, 3] = [-4000, -3000, -2000]
    assert geometry().reference_bounds([0, 31, 0, 47, 0, 63], transform, (8192, 8192, 8192)) == [2000, 2064, 3000, 3048, 4000, 4032]
    assert geometry().reference_bounds([0, 31, 0, 47, 0, 63], transform, (16, 16, 16)) is None


def test_affine_sampling_crosses_multiple_scratch_chunks():
    matrix = np.diag([0.5, 0.5, 0.5, 1.])
    matrix[:3, 3] = 0.25
    source = np.full((40, 40, 40), 7, dtype=np.uint8)
    mask = geometry().sample_region(source, [0, 39]*3, matrix, [0, 67, 0, 55, 0, 43], 7)
    assert mask.size > 65536 and np.all(mask)


@pytest.mark.parametrize("bad", [np.zeros((4, 4)), np.full((4, 4), np.nan), np.eye(3)])
def test_invalid_geometry_is_not_silently_treated_as_empty(bad):
    with pytest.raises(ValueError):
        geometry().reference_bounds([0, 3, 0, 3, 0, 3], bad, (32, 32, 32))


def test_live_capture_has_no_full_reference_fallback():
    path = Path(__file__).resolve().parents[2] / "LiveSegmentation" / "LiveSegmentation.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    methods = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    calls = {node.func.attr for node in ast.walk(methods["capture_segment_mask_steps"])
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert "segment_mask_in_reference_geometry" not in calls


def test_controller_refuses_a_full_mask_after_failed_bounded_read():
    from LiveSegmentationLib.collaboration import LiveCollaborationController
    owner = SimpleNamespace(get_volume_node=lambda: None,
                            segment_mask_region_in_reference_geometry=lambda *args: None,
                            segment_mask_crop_in_reference_geometry=lambda *args: None)
    def forbidden(*args):
        raise AssertionError("Full-volume fallback was called")
    controller = SimpleNamespace(owner=owner, _read_mask=forbidden)
    with pytest.raises(RuntimeError, match="no full-volume"):
        LiveCollaborationController._read_mask_region(controller, None, "label", [0, 1]*3)
    with pytest.raises(RuntimeError, match="no full-volume"):
        LiveCollaborationController._read_mask_crop(controller, None, "label")

