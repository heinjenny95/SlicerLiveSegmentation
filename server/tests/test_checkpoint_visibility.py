"""Checkpoint prefixes must not alter visible materials or undo local edits."""
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
