"""Decode trusted local bitsets once, with a bounded-memory legacy fallback."""

import base64
import gc
import queue
import zlib
from types import SimpleNamespace

import numpy as np
import pytest

from tests.controller_harness import collaboration as core


def encoded(shape=(64, 64, 64)):
    empty = np.zeros(shape, np.uint8)
    mask = empty.copy()
    mask[1::3, 2::4, 3::5] = 1
    operation = {"segment_id": "label", "sequence": 1,
                 **core.encode_mask_delta(empty, mask, replace=True)}
    b = operation["voxel_bbox"]
    return operation, mask[b[0]:b[1], b[2]:b[3], b[4]:b[5]]


def pull(operations, **kwargs):
    c = core.LiveCollaborationController.__new__(core.LiveCollaborationController)
    c._worker_results = queue.Queue()
    c.volume_shape = operations[0]["volume_shape"]
    c._edit_pull_lane(1, SimpleNamespace(operations=lambda *a, **k: operations), "room", 0, **kwargs)
    return c._worker_results.get_nowait()


def test_pull_validates_and_inflates_each_payload_once(monkeypatch):
    op, expected = encoded()
    original = core.PackedOperationMask
    calls = []
    def decoder(item, **kwargs):
        calls.append(item)
        return original(item, **kwargs)
    monkeypatch.setattr(core, "PackedOperationMask", decoder)
    # A peer-supplied private cache must never bypass validation.
    result = pull([{**op, "_packed": "untrusted", "_decoded": "untrusted"}])
    assert "error" not in result
    assert len(calls) == 1
    item = result["operations"][0]
    assert "_decoded" not in item
    changed, values = item["_packed"].region(op["voxel_bbox"])
    np.testing.assert_array_equal(values[changed], expected[changed])


def test_valid_large_legacy_payload_uses_file_backing_and_cleans_it_up(monkeypatch):
    op, expected = encoded()
    monkeypatch.setattr(core, "PACKED_RESIDENT_LIMIT_BYTES", 1024)
    result = pull([op])
    packed = result["operations"][0]["_packed"]
    assert isinstance(packed.bytes, np.memmap)
    assert packed.resident_bytes == 0
    backing = packed._backing_file
    changed, values = packed.region(op["voxel_bbox"])
    np.testing.assert_array_equal(values[changed], expected[changed])
    del packed, result
    gc.collect()
    assert backing.closed


def test_budget_stops_before_decompressing_an_unqueueable_second_payload(monkeypatch):
    op, _ = encoded()
    original = core.PackedOperationMask
    calls = []
    def decoder(item, **kwargs):
        calls.append(item)
        return original(item, **kwargs)
    monkeypatch.setattr(core, "PackedOperationMask", decoder)
    budget = len(op["payload"]) + 4096 + 2 * ((np.prod(op["volume_shape"]) + 7) // 8)
    result = pull([op, {**op, "sequence": 2}], byte_budget=int(budget))
    assert len(result["operations"]) == 1
    assert len(calls) == 1


@pytest.mark.parametrize("raw", [b"", b"\xff", b"\xff" * 1024])
def test_file_backed_decoder_rejects_wrong_payload_length(monkeypatch, raw):
    op, _ = encoded()
    monkeypatch.setattr(core, "PACKED_RESIDENT_LIMIT_BYTES", 1)
    invalid = {**op, "payload": base64.b64encode(zlib.compress(raw)).decode()}
    assert core.operation_defect(invalid) is not None
    result = pull([invalid])
    assert "_undecodable" in result["operations"][0]
    assert "_packed" not in result["operations"][0]


def test_queue_budget_includes_encoded_strings_and_metadata():
    op, _ = encoded()
    packed = core.PackedOperationMask(op)
    assert core.incoming_operation_bytes({**op, "_packed": packed}) >= len(op["payload"]) + packed.bytes.nbytes + 4096


def test_first_operation_spills_if_remaining_queue_budget_is_small():
    op, _ = encoded()
    metadata = core.incoming_operation_bytes({key: value for key, value in op.items() if key != "payload"})
    result = pull([op], byte_budget=metadata)
    item = result["operations"][0]
    assert item["_packed"].resident_bytes == 0
    assert core.incoming_operation_bytes(item) <= metadata
    assert "payload" not in item


def test_file_backed_changed_count_matches_dense_mask(monkeypatch):
    op, expected = encoded()
    monkeypatch.setattr(core, "PACKED_RESIDENT_LIMIT_BYTES", 1)
    assert core.operation_changed_voxel_count(op) == int(np.count_nonzero(expected))


def test_prepared_checkpoint_applies_without_reinflating_encoded_copy(monkeypatch):
    from tests import controller_harness as h

    scene, _ = h.install_fake_slicer(monkeypatch)
    peer = h.Peer("receiver", scene)
    op, expected = encoded(h.SHAPE)
    op.update(system_snapshot=True, snapshot_group_id="prepared", snapshot_group_index=0,
              snapshot_group_count=1)
    result = pull([op])
    prepared = result["operations"][0]
    assert "payload" not in prepared
    peer.controller._incoming_operations.append(prepared)
    for _ in range(100):
        if not peer.controller._incoming_operations:
            break
        peer.controller._incoming_retry_at = 0
        peer.controller._drain_incoming_operations()
    assert not peer.errors
    b = op["voxel_bbox"]
    np.testing.assert_array_equal(peer.mask("label")[b[0]:b[1], b[2]:b[3], b[4]:b[5]], expected)
    assert peer.controller.last_sequence == 1


def test_large_label_order_metadata_is_part_of_queue_budget():
    order = {"segment_order": [f"id-{i:05d}-" + "x" * 190 for i in range(10000)]}
    assert core.incoming_operation_bytes(order) >= 2_000_000


def test_next_request_waits_for_metadata_budget_before_decoding(monkeypatch):
    op, _ = encoded()
    def unexpected_decode(*_args, **_kwargs):
        pytest.fail("decoded despite insufficient metadata budget")
    monkeypatch.setattr(core, "PackedOperationMask", unexpected_decode)
    result = pull([op], byte_budget=1)
    assert result["operations"] == []
