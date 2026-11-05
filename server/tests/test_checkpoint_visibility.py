"""Checkpoint prefixes must not alter visible materials or undo local edits."""
import weakref

import numpy as np

from tests import controller_harness as harness


def checkpoint(peer, start=2):
    operations = peer.controller._snapshot_operations()
    return [{**op, "sequence": start + index, "author": "author",
             "client_operation_id": f"checkpoint-{index}", "system_snapshot": True,
             "snapshot_group_id": "checkpoint", "snapshot_group_index": index,
             "snapshot_group_count": len(operations)} for index, op in enumerate(operations)]


def test_unchanged_checkpoint_never_removes_a_visible_component(monkeypatch):
    monkeypatch.setattr(harness, "SHAPE", (128, 128, 128))
    scene, _ = harness.install_fake_slicer(monkeypatch)
    peer = harness.Peer("receiver", scene)
    shape = harness.SHAPE
    peer.controller.volume_shape = shape
    mask = np.zeros(shape, np.uint8)
    mask[1, 1, 1] = mask[126, 126, 126] = 1
    peer.apply({**harness.collaboration.encode_mask_delta(np.zeros_like(mask), mask, replace=True),
                "segment_id": "A", "segment_name": "A", "color_hex": "#FF0000",
                "author": "author", "sequence": 1, "client_operation_id": "initial"})
    writes = []
    old_write = peer.controller.owner.update_segment_binary_labelmap_crop

    def write(*args):
        result = old_write(*args)
        writes.append(peer.mask("A").copy())
        return result

    peer.controller.owner.update_segment_binary_labelmap_crop = write
    ops = checkpoint(peer)
    assert len(ops) >= 2
    peer.apply(ops[0])
    assert np.array_equal(peer.mask("A"), mask), "Receiving a prefix hides an existing component"
    for op in ops[1:]:
        peer.apply(op)
    assert np.array_equal(peer.mask("A"), mask)
    assert not writes, "An unchanged checkpoint must not rewrite MRML at all"
    assert peer.controller.last_sequence == ops[-1]["sequence"]


def test_pending_local_stroke_survives_unchanged_checkpoint(monkeypatch):
    monkeypatch.setattr(harness, "SHAPE", (128, 128, 128))
    scene, _ = harness.install_fake_slicer(monkeypatch)
    peer = harness.Peer("receiver", scene)
    peer.controller.volume_shape = harness.SHAPE
    mask = np.zeros(harness.SHAPE, np.uint8)
    mask[1, 1, 1] = mask[126, 126, 126] = 1
    peer.apply({**harness.collaboration.encode_mask_delta(np.zeros_like(mask), mask, replace=True),
                "segment_id": "A", "author": "author", "sequence": 1, "client_operation_id": "initial"})
    ops = checkpoint(peer)
    peer.paint("A", (60, 60, 60))
    for op in ops:
        peer.apply(op)
        assert peer.mask("A")[60, 60, 60] == 1
    assert not peer.errors


def test_changed_checkpoint_is_staged_and_preserves_local_edits(monkeypatch):
    monkeypatch.setattr(harness, "SHAPE", (128, 128, 128))
    scene, _ = harness.install_fake_slicer(monkeypatch)
    peer = harness.Peer("receiver", scene)
    peer.controller.volume_shape = harness.SHAPE
    initial = np.zeros(harness.SHAPE, np.uint8)
    initial[1, 1, 1] = initial[126, 126, 126] = 1
    peer.apply({**harness.collaboration.encode_mask_delta(np.zeros_like(initial), initial, replace=True),
                "segment_id": "A", "author": "author", "sequence": 1, "client_operation_id": "initial"})
    target = initial.copy()
    target[1, 1, 1] = 0
    target[2, 2, 2] = 1
    baseline = harness.collaboration.ChunkedMaskBaseline.from_crop(harness.SHAPE, target, [0, 128, 0, 128, 0, 128])
    encoded = harness.collaboration.encode_chunked_mask_snapshot(baseline)
    ops = [{**op, "segment_id": "A", "sequence": 2+i, "author": "author",
            "client_operation_id": f"new-{i}", "system_snapshot": True,
            "snapshot_group_id": "new", "snapshot_group_index": i, "snapshot_group_count": len(encoded)}
           for i, op in enumerate(encoded)]
    peer.paint("A", (60, 60, 60))
    visible = peer.mask("A").copy()
    for op in ops[:-1]:
        peer.apply(op)
        assert np.array_equal(peer.mask("A"), visible)
    # A local stroke that arrives while the private native image is staged must
    # invalidate it and survive the cooperative retry.
    real_stage = peer.controller.owner.stage_checkpoint_segment_steps
    edited = []

    def stage(*args):
        result = yield from real_stage(*args)
        if not edited:
            peer.paint("A", (61, 61, 61))
            edited.append(True)
        return result

    peer.controller.owner.stage_checkpoint_segment_steps = stage
    peer.apply(ops[-1])
    target[60, 60, 60] = target[61, 61, 61] = 1
    assert np.array_equal(peer.mask("A"), target)
    assert not peer.errors


def test_missing_checkpoint_part_keeps_visible_state_and_cursor(monkeypatch):
    monkeypatch.setattr(harness, "SHAPE", (128, 128, 128))
    scene, _ = harness.install_fake_slicer(monkeypatch)
    peer = harness.Peer("receiver", scene)
    peer.controller.volume_shape = harness.SHAPE
    mask = np.zeros(harness.SHAPE, np.uint8)
    mask[1, 1, 1] = mask[126, 126, 126] = 1
    peer.apply({**harness.collaboration.encode_mask_delta(np.zeros_like(mask), mask, replace=True),
                "segment_id": "A", "author": "author", "sequence": 1, "client_operation_id": "initial"})
    ops = checkpoint(peer)
    peer.apply(ops[0])
    bad = {**ops[-1], "snapshot_group_index": 5}
    peer.controller._incoming_operations.append(bad)
    for _ in range(3):
        peer.controller._incoming_retry_at = 0
        peer.controller._drain_incoming_operations()
    assert peer.controller.last_sequence == 1
    assert peer.controller._incoming_operations
    assert peer.errors
    assert np.array_equal(peer.mask("A"), mask)


def test_ingested_checkpoint_releases_payloads_after_assembling_baseline():
    from checkpoints import CheckpointGroup

    c = harness.collaboration
    operation = {**c.encode_mask_crop_snapshot(np.ones((2, 2, 2), np.uint8), [0, 2]*3, (8, 8, 8)),
                 "segment_id": "A", "segment_name": "Organ", "color_hex": "#123456",
                 "sequence": 1, "snapshot_group_id": "memory", "snapshot_group_index": 0,
                 "snapshot_group_count": 1, "system_snapshot": True}
    packed = c.PackedOperationMask(operation)
    reference = weakref.ref(packed)
    operation["_packed"] = packed
    group = CheckpointGroup(operation, (8, 8, 8), c.ChunkedMaskBaseline, c.iter_operation_tiles)
    list(group.ingest(operation))
    assert "payload" not in group.records[0]
    assert not {"payload", "_packed", "_decoded"}.intersection(group.metadata["A"])
    assert group.metadata["A"]["color_hex"] == "#123456"
    assert group.baselines["A"].to_dense().sum() == 8
    del operation["_packed"], packed
    assert reference() is None, "Checkpoint metadata retains a decoded packed operation"


def test_committed_checkpoint_releases_old_images_before_native_surfaces(monkeypatch):
    from checkpoints import CheckpointGroup

    scene, _ = harness.install_fake_slicer(monkeypatch)
    peer = harness.Peer("receiver", scene)
    c = harness.collaboration
    original = np.zeros(harness.SHAPE, np.uint8)
    original[1, 1, 1] = 1
    peer.apply({**c.encode_mask_delta(np.zeros_like(original), original, replace=True),
                "segment_id": "A", "author": "peer", "sequence": 1})
    target = original.copy()
    target[2, 2, 2] = 1
    operation = {**c.encode_mask_delta(np.zeros_like(target), target, replace=True),
                 "segment_id": "A", "author": "peer", "sequence": 2,
                 "snapshot_group_id": "release", "snapshot_group_index": 0,
                 "snapshot_group_count": 1, "system_snapshot": True}
    group = CheckpointGroup(operation, harness.SHAPE, c.ChunkedMaskBaseline, c.iter_operation_tiles)
    list(group.ingest(operation))
    original_stage = peer.controller.owner.stage_checkpoint_segment_steps
    references = []

    class OldNativeImage:
        pass

    def stage(*args):
        result = yield from original_stage(*args)
        image = OldNativeImage()
        references.append(weakref.ref(image))
        result["old_image"] = image
        return result

    def finish_surfaces():
        assert references and references[0]() is None, "Derived surface work retains the replaced native image"
        return True

    peer.controller.owner.stage_checkpoint_segment_steps = stage
    peer.controller._finish_incoming_native_surfaces = finish_surfaces
    list(peer.controller._commit_checkpoint_steps(group))
    np.testing.assert_array_equal(peer.mask("A"), target)
