"""Received tiles coalesce native surface conversion without caching stale meshes."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

BINARY = "Binary labelmap"
SURFACE = "Closed surface"


class Segment:
    def __init__(self, values):
        self.values = values
        self.image = Mock()
        self.image.GetExtent.return_value = (0, 7, 0, 7, 0, 7)
        self.representations = {BINARY: self.image, SURFACE: object()}
        self.Modified = Mock()

    def GetRepresentation(self, name):
        return self.representations.get(name)

    def RemoveAllRepresentations(self, except_name):
        self.representations = {except_name: self.representations[except_name]}

    def AddRepresentation(self, name, value):
        self.representations[name] = value

    def GetLabelValue(self):
        return 1


class Segmentation:
    def __init__(self):
        self.segments = {sid: Segment(np.zeros((8,)*3, np.uint8)) for sid in ("A", "B")}
        self.global_conversions = 0

    def GetSegmentIDs(self):
        return list(self.segments)

    def GetSegment(self, sid):
        return self.segments.get(sid)

    def ContainsRepresentation(self, name):
        # Native Slicer checks the first segment, assuming representation types
        # are uniform. Removing that segment's mesh must not cancel a batch.
        return self.segments["A"].GetRepresentation(name) is not None

    def CreateRepresentation(self, name):
        self.global_conversions += 1
        for segment in self.segments.values():
            if segment.GetRepresentation(name) is None:
                segment.AddRepresentation(name, object())
        return True


def native_scope():
    path = Path(__file__).resolve().parents[2] / "LiveSegmentation/LiveSegmentation.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    batch = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "NativeSurfaceUpdateBatch")
    widget = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "LiveSegmentationWidget")
    widget.bases, widget.keywords = [], []
    widget.body = [n for n in widget.body if isinstance(n, ast.FunctionDef)
                   and n.name == "update_segment_binary_labelmap_crop"]
    segmentation = Segmentation()
    node = SimpleNamespace(GetSegmentation=lambda: segmentation)
    single = Mock(side_effect=lambda seg, sid, name: object())
    slicer = SimpleNamespace(
        modules=SimpleNamespace(segmentations=SimpleNamespace(logic=lambda: SimpleNamespace(
            CreateRepresentationForOneSegment=single))),
        util=SimpleNamespace(arrayFromSegmentInternalBinaryLabelmap=lambda node, sid: segmentation.segments[sid].values))
    namespace = {"slicer": slicer, "np": np, "vtk": SimpleNamespace(vtkMatrix4x4=object),
                 "vtkSegmentationCore": SimpleNamespace(vtkSegmentationConverter=SimpleNamespace(
                     GetSegmentationBinaryLabelmapRepresentationName=lambda: BINARY))}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[batch, widget], type_ignores=[])), str(path), "exec"), namespace)
    cls = namespace["LiveSegmentationWidget"]
    cls._reference_ijk_to_segmentation_ras_matrix = Mock(return_value=object())
    cls._matrices_match = Mock(return_value=True)
    return namespace["NativeSurfaceUpdateBatch"], cls, node, segmentation, single


def write_tiles(cls, node, segmentation, **options):
    values = segmentation.GetSegment("A").values
    for _ in range(12):
        before = values[:2, :2, :2].copy()
        assert cls.update_segment_binary_labelmap_crop(
            before, 1-before, [0, 2, 0, 2, 0, 2], node, "A", object(), **options)


def test_many_remote_tiles_convert_once_with_native_single_segment_converter():
    batch_type, cls, node, segmentation, single = native_scope()
    batch = batch_type(node)
    other_surface = segmentation.GetSegment("B").GetRepresentation(SURFACE)
    write_tiles(cls, node, segmentation, surface_updates=batch)
    assert segmentation.global_conversions == 0
    assert not segmentation.ContainsRepresentation(SURFACE)
    assert segmentation.GetSegment("A").GetRepresentation(SURFACE) is None
    assert batch.requested and batch.dirty_ids == {"A"}
    assert list(batch.finish_steps()) == [None]
    single.assert_called_once_with(segmentation, "A", SURFACE)
    assert segmentation.GetSegment("A").GetRepresentation(SURFACE) is not None
    assert segmentation.GetSegment("B").GetRepresentation(SURFACE) is other_surface
    assert not np.any(segmentation.GetSegment("B").values)


def test_independent_writer_keeps_normal_synchronous_native_behavior():
    _, cls, node, segmentation, single = native_scope()
    write_tiles(cls, node, segmentation)
    assert segmentation.global_conversions == 12
    assert segmentation.ContainsRepresentation(SURFACE)
    single.assert_not_called()


def test_batch_respects_native_show_3d_off_and_deleted_labels():
    batch_type, _, node, segmentation, single = native_scope()
    batch = batch_type(node)
    batch.record(node, "A", True)
    for segment in segmentation.segments.values():
        segment.RemoveAllRepresentations(BINARY)
    assert list(batch.finish_steps()) == []
    single.assert_not_called()
    assert not batch.dirty_ids
    segmentation.CreateRepresentation(SURFACE)
    batch = batch_type(node)
    batch.record(node, "A", True)
    del segmentation.segments["A"]
    assert list(batch.finish_steps()) == []
    single.assert_not_called()


def test_2d_only_batch_does_not_create_native_surfaces():
    batch_type, cls, node, segmentation, single = native_scope()
    for segment in segmentation.segments.values():
        segment.RemoveAllRepresentations(BINARY)
    batch = batch_type(node)
    write_tiles(cls, node, segmentation, surface_updates=batch)
    assert list(batch.finish_steps()) == []
    assert segmentation.global_conversions == 0
    single.assert_not_called()


def test_batch_uses_existing_other_surface_when_first_native_cache_is_missing():
    batch_type, _, node, segmentation, single = native_scope()
    segmentation.GetSegment("A").RemoveAllRepresentations(BINARY)
    batch = batch_type(node)
    batch.record(node, "A", False)
    assert list(batch.finish_steps()) == [None]
    single.assert_called_once_with(segmentation, "A", SURFACE)


def test_show_3d_off_between_conversions_stops_an_all_label_cohort():
    batch_type, _, node, segmentation, single = native_scope()
    batch = batch_type(node)
    for sid, segment in segmentation.segments.items():
        batch.record(node, sid, True)
        segment.RemoveAllRepresentations(BINARY)
    steps = batch.finish_steps()
    assert next(steps) is None
    for segment in segmentation.segments.values():
        segment.RemoveAllRepresentations(BINARY)
    assert list(steps) == []
    single.assert_called_once_with(segmentation, "A", SURFACE)
    assert not batch.dirty_ids


def received_cohorts(monkeypatch, *, expire_after_first=False):
    from tests import controller_harness as harness

    scene, _ = harness.install_fake_slicer(monkeypatch)
    peer = harness.Peer("receiver", scene)
    controller = peer.controller
    batches, converted = [], []

    def begin(node):
        batch = SimpleNamespace(last_segment_id=None, conversion_count=0, dirty=set())

        def finish():
            if batch.dirty:
                batch.last_segment_id = "A"
                batch.conversion_count += 1
                converted.append(set(batch.dirty))
                batch.dirty.clear()
                yield None

        batch.finish_steps = finish
        batches.append(batch)
        return batch

    def apply(operations, *, surface_updates=None):
        assert surface_updates is not None
        for operation in operations:
            surface_updates.dirty.add(operation["segment_id"])
            controller.last_sequence = operation["sequence"]
            if expire_after_first and operation["sequence"] == 1:
                controller._incoming_surface_started_at = -1000
        return True

    controller.owner.begin_native_surface_updates = begin
    controller._probe_segment_order = Mock()
    controller._current_segment_metadata = Mock(return_value={})
    controller._iter_incoming_operation = lambda operation, node, baseline: iter([operation])
    controller._apply_operations = apply
    controller._incoming_operations.extend({"sequence": index, "segment_id": "A"} for index in (1, 2, 3))
    for _ in range(20):
        controller._drain_incoming_operations()
        if not controller._incoming_operations and controller._incoming_surface_updates is None:
            break
    assert not peer.errors
    assert controller.last_sequence == 3
    assert controller._incoming_surface_updates is None
    assert controller._incoming_surface_iterator is None
    return batches, converted


def test_queued_wire_operations_for_one_material_share_a_native_conversion(monkeypatch):
    batches, converted = received_cohorts(monkeypatch)
    assert len(batches) == 1
    assert converted == [{"A"}]


def test_cohort_age_limit_flushes_between_operations_under_continuous_input(monkeypatch):
    batches, converted = received_cohorts(monkeypatch, expire_after_first=True)
    assert len(batches) == 2
    assert converted == [{"A"}, {"A"}]
