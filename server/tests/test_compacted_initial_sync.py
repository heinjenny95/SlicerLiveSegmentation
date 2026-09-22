"""A small initial-sync page must work even after a much larger archived gap."""

from __future__ import annotations

import numpy as np
import pytest

from tests.controller_harness import collaboration


@pytest.mark.parametrize(("cursor", "limit"), [(0, 1), (0, 8), (4, 1)])
def test_rejoin_pages_start_at_the_compacted_snapshot(tmp_path, cursor, limit):
    client = collaboration.SharedFolderRoomClient(tmp_path, "alice")
    room = client.join("room", "s" * 64)
    empty = np.zeros((4, 4, 4), np.uint8)
    mask = empty.copy()
    mask[1:3, 1:3, 1:3] = 1
    snapshot = {
        "segment_id": "LiveSeg-A", "segment_name": "A", "color_hex": "#FF0000",
        **collaboration.encode_mask_delta(empty, mask, replace=True),
    }
    for index in range(9):
        client.push_operation(room["id"], dict(snapshot, client_operation_id=f"edit-{index}"))
    client._artifact_queue.join()
    manifest = client.publish_room_snapshot(room["id"], [snapshot], compact=True)
    newcomer = collaboration.SharedFolderRoomClient(tmp_path, "bob")
    newcomer.join("room", "s" * 64)
    page = newcomer.operations(room["id"], cursor, limit=limit)
    assert [item["sequence"] for item in page] == [manifest["first_sequence"]]
    np.testing.assert_array_equal(collaboration.apply_mask_delta(empty, page[0]), mask)
