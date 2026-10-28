"""The approximate view must not mutate masks or mix packed material values."""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/"LiveSegmentation"/"LiveSegmentationLib"))
from surface_preview import preview_mask  # noqa: E402


def test_closed_editor_wrappers_do_not_break_scanning_or_restore(monkeypatch):
    from types import SimpleNamespace

    from surface_preview import SurfacePreview

    from tests import controller_harness as h

    h.install_fake_slicer(monkeypatch)
    preview = SurfacePreview.__new__(SurfacePreview)
    def deleted(*args):
        raise ValueError('Internal C++ object already deleted')
    dead = SimpleNamespace(segmentationNode=deleted, setVisible=deleted,
                           setShow3DButtonVisible=deleted, hide=deleted,
                           deleteLater=deleted)
    node = object()
    live = SimpleNamespace(segmentationNode=lambda: node)
    preview.node, preview.owner = node, SimpleNamespace(_recovery_editor=dead)
    preview.editors, preview.buttons = [(dead, True), (live, True)], [(dead, True)]
    preview.proxies = [(dead, dead)]
    slicer = sys.modules['slicer']
    slicer.util.findChildren = lambda **kwargs: []
    slicer.util.findChild = deleted
    preview.scan_editors()
    assert preview.editors == [(live, True)] and not preview.buttons
    preview.editors.append((dead, True))
    preview.buttons.append((dead, True))
    live.setShow3DButtonVisible = lambda unused: None
    preview._restore_editors()
    assert not preview.editors and not preview.buttons and not preview.proxies


def test_repeated_dirty_bounds_are_scanned_once_per_pooling_cell():
    from surface_preview import update_preview_mask

    class CountedArray:
        shape = (1024,)*3
        touched = 0
        def __getitem__(self, slices):
            shape = tuple(s.stop-s.start for s in slices)
            self.touched += int(np.prod(shape))
            return np.zeros(shape, np.uint8)
    array = CountedArray()
    _, scanned = update_preview_mask(array, 1, np.zeros((128,)*3, np.uint8), 8,
                                     [[1, 2, 1, 2, 1, 2]]*31)
    assert array.touched == scanned == 512


def test_obsolete_pooling_stops_before_reading_another_large_slab():
    import threading

    from surface_preview import PreviewSuperseded, _pool_at_factor

    cancel = threading.Event()
    class SupersededArray:
        shape = (1024,)*3
        touched = 0
        def __getitem__(self, slices):
            shape = (slices.stop-slices.start, 1024, 1024)
            self.touched += int(np.prod(shape))
            cancel.set()
            return np.zeros(shape, np.uint8)
    array = SupersededArray()
    with pytest.raises(PreviewSuperseded):
        _pool_at_factor(array, 1, 8, cancel=cancel)
    assert array.touched <= 1024*1024


def test_allocated_zero_extent_label_does_not_schedule_a_preview_job():
    from types import SimpleNamespace

    from surface_preview import SurfacePreview

    image = SimpleNamespace(GetPointData=lambda: SimpleNamespace(GetScalars=lambda: SimpleNamespace(GetMTime=lambda: 1)),
                            GetExtent=lambda: (0, -1, 0, -1, 0, -1),
                            GetAddressAsString=lambda unused: "empty", GetMTime=lambda: 1)
    segment = SimpleNamespace(GetRepresentation=lambda unused: image, GetLabelValue=lambda: 1)
    assert SurfacePreview.revision(segment) is None


def test_incremental_pooling_matches_full_pooling_after_additions_and_removals():
    from surface_preview import update_preview_mask

    mask = np.zeros((131, 133, 135), np.uint16)
    mask[6:9, 6:9, 6:9] = 7
    old, factor = preview_mask(mask, 7)
    mask[6:9, 6:9, 6:9] = 0
    mask[128:131, 130:133, 132:135] = 7
    mask[30, 30, 30] = 8
    coarse, scanned = update_preview_mask(mask, 7, old, factor,
                                          [[6, 9, 6, 9, 6, 9], [128, 131, 130, 133, 132, 135]])
    expected, _ = preview_mask(mask, 7)
    np.testing.assert_array_equal(coarse, expected)
    assert scanned < 1000
    assert old[6//factor, 6//factor, 6//factor] == 1, "A worker mutated the published cache"


def test_incremental_pooling_reads_only_whole_cells_touching_the_changed_region():
    from surface_preview import update_preview_mask

    class CountedArray:
        shape = (1024, 1024, 1024)
        touched = 0
        def __getitem__(self, slices):
            shape = tuple(s.stop-s.start for s in slices)
            self.touched += int(np.prod(shape))
            return np.zeros(shape, np.uint8)

    array = CountedArray()
    pooled = np.ones((128,)*3, np.uint8)
    result, scanned = update_preview_mask(array, 1, pooled, 8, [[1, 2, 1, 2, 1, 2]])
    assert array.touched == scanned == 512
    assert result[0, 0, 0] == 0 and result[1, 1, 1] == 1


def test_preview_occupancy_retains_thin_and_boundary_components():
    mask = np.zeros((257, 259, 261), np.uint8)
    mask[0, 0, 0] = mask[256, 258, 260] = mask[120, 120, 120] = 1
    coarse, factor = preview_mask(mask, 1)
    assert factor == 3 and max(coarse.shape) <= 128
    assert coarse[0, 0, 0] and coarse[-1, -1, -1] and coarse[40, 40, 40]
    assert int(mask.sum()) == 3


def test_preview_keeps_material_identity_in_shared_scalar_layers():
    mask = np.zeros((130, 130, 130), np.uint16)
    mask[2, 2, 2] = 7
    mask[120, 120, 120] = 8
    before = mask.copy()
    coarse, _ = preview_mask(mask, 7)
    assert int(coarse.sum()) == 1 and coarse[1, 1, 1]
    np.testing.assert_array_equal(mask, before)


def test_preview_is_exact_below_its_grid_limit_and_handles_empty_material():
    source = np.zeros((11, 19, 23), np.uint8)
    source[3:5, 8:10, 2:4] = 1
    result, factor = preview_mask(source, 1)
    assert factor == 1
    np.testing.assert_array_equal(result, source)
    empty, _ = preview_mask(source, 2)
    assert not np.any(empty)


def test_old_room_worker_output_is_never_published(monkeypatch):
    import queue
    import types

    from surface_preview import SurfacePreview

    from tests import controller_harness as h

    h.install_fake_slicer(monkeypatch)
    preview = SurfacePreview.__new__(SurfacePreview)
    job = {"generation": 2, "sid": "A", "revision": "old"}
    preview.results = queue.SimpleQueue()
    preview.results.put((job, {"mesh": object()}))
    preview.job, preview.active, preview.generation = job, True, 3
    preview.node = types.SimpleNamespace(GetSegmentation=lambda: (_ for _ in ()).throw(AssertionError("Old result accessed a new room")))
    preview._drain()
    assert preview.job is None


def test_changed_material_worker_output_is_not_published(monkeypatch):
    import queue
    import types

    from surface_preview import SurfacePreview

    from tests import controller_harness as h

    h.install_fake_slicer(monkeypatch)
    preview = SurfacePreview.__new__(SurfacePreview)
    job = {"generation": 3, "sid": "A", "revision": "old"}
    preview.results = queue.SimpleQueue()
    preview.results.put((job, {"mesh": object()}))
    preview.job, preview.active, preview.generation = job, True, 3
    preview.node = types.SimpleNamespace(GetSegmentation=lambda: types.SimpleNamespace(GetSegment=lambda unused: object()))
    preview.revision = lambda unused: "new"
    events = []
    preview.log = lambda event, **kw: events.append(event)
    preview._drain()
    assert events == ["preview.stale"] and preview.job is None
