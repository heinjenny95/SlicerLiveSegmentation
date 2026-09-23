"""Retry receipts outlive loose operation files (L-16)."""

import hashlib

import numpy as np
import pytest

from tests.controller_harness import collaboration as c


@pytest.mark.parametrize("remove_receipt", [False, True])
def test_lost_ack_retry_after_compaction_keeps_original_sequence(tmp_path, remove_receipt):
    alice = c.SharedFolderRoomClient(tmp_path, "alice")
    room_id = alice.join("room", "s" * 64)["id"]
    empty = np.zeros((4, 4, 4), np.uint8)
    painted = empty.copy()
    painted[1, 1, 1] = 1
    old = {
        "client_operation_id": "lost-ack", "segment_id": "A", "segment_name": "A",
        "color_hex": "#FF0000", **c.encode_mask_delta(empty, painted),
    }
    assert alice.push_operation(room_id, old)["sequence"] == 1
    alice._artifact_queue.join()
    receipt = (alice._room_path / "operation-index"
               / (hashlib.sha256(b"lost-ack").hexdigest()[:20] + ".json"))
    if remove_receipt:
        # A publisher may have exited before its asynchronous index write.
        receipt.unlink()
    bob = c.SharedFolderRoomClient(tmp_path, "bob")
    bob.join("room", "s" * 64)
    manifest = bob.publish_room_snapshot(room_id, [dict(
        old, **c.encode_mask_delta(empty, empty, replace=True)
    )])
    assert not list((alice._room_path / "operations").glob("00000000000000000001--*"))
    restarted = c.SharedFolderRoomClient(tmp_path, "alice")
    restarted.join("room", "s" * 64)
    assert restarted.push_operation(room_id, old) == {"sequence": 1, "duplicate": True}
    assert restarted._latest_sequence(restarted._room_path) == manifest["last_sequence"]
    assert restarted.operations(room_id, manifest["last_sequence"]) == []

