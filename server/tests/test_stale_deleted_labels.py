"""An edit based before a deletion cannot revive that label (L-17)."""

import numpy as np
import pytest

from tests.controller_harness import collaboration as c
from tests.test_rejected_operations import LABEL, seeded_room


def edits():
    empty = np.zeros((4, 4, 4), np.uint8)
    mask = empty.copy()
    mask[1, 1, 1] = 1
    seed = {
        "client_operation_id": "seed-label", "segment_id": "A", "segment_name": "A",
        "color_hex": "#FF0000", **c.encode_mask_delta(empty, mask, replace=True),
    }
    deletion = dict(seed, client_operation_id="delete-label", base_sequence=1,
                    segment_deleted=True, **c.encode_metadata_update(empty.shape))
    late = dict(seed, client_operation_id="late-stroke", base_sequence=1)
    return seed, deletion, late


@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("lose_cache", [False, True])
def test_folder_rejects_stale_edits_even_after_restore_or_compaction(tmp_path, compact, lose_cache):
    alice = c.SharedFolderRoomClient(tmp_path, "alice")
    room_id = alice.join("room", "s" * 64)["id"]
    seed, deletion, late = edits()
    alice.push_operation(room_id, seed)
    alice.push_operation(room_id, deletion)
    alice._artifact_queue.join()
    if compact:
        alice.publish_room_snapshot(room_id, [dict(seed, segment_id="B")])
    # An explicit restore, based on seeing the deletion, remains supported.
    restored = alice.push_operation(room_id, dict(seed, client_operation_id="restore-label",
                                                 base_sequence=2))
    if lose_cache:
        (alice._room_path / "sequence-state.json").unlink()
    restarted = c.SharedFolderRoomClient(tmp_path, "bob")
    restarted.join("room", "s" * 64)
    with pytest.raises(c.LiveCollaborationError, match="Label was deleted"):
        restarted.push_operation(room_id, late)
    assert restarted._latest_sequence(restarted._room_path) == restored["sequence"]


def test_http_rejects_stale_edits_but_accepts_an_informed_restore(client, headers):
    room = client.post("/api/live/rooms/join", headers=headers,
                       json={"room_name": "room", "volume_signature": "s" * 64}).json()
    url = f"/api/live/rooms/{room['id']}/operations"
    seed, deletion, late = edits()
    assert client.post(url, headers=headers, json=seed).status_code == 201
    assert client.post(url, headers=headers, json=deletion).status_code == 201
    rejected = client.post(url, headers=headers, json=late)
    assert rejected.status_code == 409
    assert "Label was deleted" in rejected.json()["detail"]
    restored = client.post(url, headers=headers, json=dict(
        seed, client_operation_id="restore-label", base_sequence=2))
    assert restored.status_code == 201
    assert client.post(url, headers=headers, json=late).status_code == 409
    # A retry of an already committed operation is still an idempotent receipt.
    assert client.post(url, headers=headers, json=seed).json()["duplicate"] is True


@pytest.mark.parametrize("delete", [False, True])
def test_stale_rejection_is_final_not_a_reconnection_or_restore(monkeypatch, delete):
    _, alice, _, _ = seeded_room(monkeypatch)
    if delete:
        alice.delete_label(LABEL)
    else:
        alice.paint(LABEL, (1, 1, 1))
    alice.prepare()
    controller = alice.controller
    controller.client.push_operation.side_effect = c.LiveCollaborationError(
        "Label was deleted at sequence 2; this edit predates that deletion."
    )
    controller._edit_push_lane(controller._session_token, controller.client, "room",
                               list(controller.outgoing), [], "")
    result = controller._worker_results.get_nowait()
    assert "error" not in result
    assert result["stale_deletion_ids"]
    controller._worker_results.put(result)
    controller._drain_worker_results()
    assert controller.connected
    assert controller.outgoing == controller.awaiting_echo == []
    assert not controller.dirty_segments
    if delete:
        assert alice.mask(LABEL) is None
