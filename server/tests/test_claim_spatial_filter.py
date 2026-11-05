"""A received tile must not allocate a dense crop for every distant label."""
from types import SimpleNamespace

import numpy as np
import pytest

from tests.controller_harness import collaboration as c


def test_many_distant_labels_are_not_sampled_for_a_local_claim(monkeypatch):
    shape = (2193, 1371, 1368)
    baseline = c.ChunkedMaskBaseline.from_crop(shape, np.ones((1, 1, 1), np.uint8),
                                            [640, 641, 640, 641, 640, 641])
    ids = [f"L{index}" for index in range(165)]
    node = SimpleNamespace(GetID=lambda: "node", GetSegmentation=lambda: SimpleNamespace(
        GetSegmentIDs=lambda: ids, GetSegment=lambda sid: object()))
    controller = c.LiveCollaborationController.__new__(c.LiveCollaborationController)
    controller.baselines = {("node", sid): baseline.fork() for sid in ids}
    controller._deleted_baselines = lambda: {}
    reads = []
    original = c.ChunkedMaskBaseline.region

    def read(self, bounds):
        reads.append(bounds)
        return original(self, bounds)

    monkeypatch.setattr(c.ChunkedMaskBaseline, "region", read)
    controller._apply_exclusive_voxel_claims(
        node, "L0", [0, 64, 0, 64, 0, 64], np.ones((64, 64, 64), bool))
    assert reads == [], "Distant labels must be rejected by sparse chunk keys before dense sampling"


def test_chunk_occupancy_is_conservative_at_edges_and_after_clearing():
    baseline = c.ChunkedMaskBaseline((131, 129, 130))
    crop = np.ones((1, 1, 1), np.uint8)
    bounds = [128, 129, 128, 129, 129, 130]
    baseline.set_region(bounds, crop, crop)
    assert baseline.has_stored_region([127, 131, 127, 129, 127, 130])
    assert not baseline.has_stored_region([0, 64, 0, 64, 0, 64])
    baseline.set_region(bounds, crop, np.zeros_like(crop))
    assert not baseline.has_stored_region(bounds)
    with pytest.raises(ValueError):
        baseline.has_stored_region([-1, 1, 0, 1, 0, 1])


def test_spatial_filter_still_clears_overlapping_retained_deleted_baselines():
    shape = (128, 128, 128)
    bounds = [1, 2, 1, 2, 1, 2]
    crop = np.ones((1, 1, 1), np.uint8)
    overlap = c.ChunkedMaskBaseline.from_crop(shape, crop, bounds)
    distant = c.ChunkedMaskBaseline.from_crop(shape, crop, [100, 101, 100, 101, 100, 101])
    deleted = overlap.fork()
    controller = c.LiveCollaborationController.__new__(c.LiveCollaborationController)
    controller.baselines = {("node", "B"): overlap, ("node", "C"): distant}
    controller._deleted_baselines = lambda: {"D": deleted}
    node = SimpleNamespace(GetID=lambda: "node", GetSegmentation=lambda: SimpleNamespace(
        GetSegmentIDs=lambda: ["A", "B", "C"], GetSegment=lambda sid: None))
    controller._apply_exclusive_voxel_claims(node, "A", bounds, crop)
    assert not np.any(overlap.region(bounds))
    assert not np.any(deleted.region(bounds))
    assert distant.chunk_count == 1
