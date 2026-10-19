"""Room order is small, ordered metadata, never a voxel/selection update."""
import uuid

import numpy as np
import pytest

from tests import controller_harness as h


def order_operation(ids):
    return {"client_operation_id": str(uuid.uuid4()), "segment_id": ids[0],
            "segment_name": ids[0], "segment_order": ids,
            **h.collaboration.encode_metadata_update(h.SHAPE)}


def seed(peers, log):
    for sid in ("A", "B", "C"):
        operation = log.commit({"client_operation_id": str(uuid.uuid4()),
            "segment_id": sid, "segment_name": sid, "metadata_update": True,
            **h.collaboration.encode_metadata_update(h.SHAPE)}, "creator")
        for peer in peers:
            peer.apply(operation)


@pytest.fixture
def room_peers(monkeypatch):
    scene, _ = h.install_fake_slicer(monkeypatch)
    peers = [h.Peer(name, scene) for name in ("Jenny", "Pauline")]
    log = h.RoomLog()
    seed(peers, log)
    return peers, log, scene


def test_local_drag_queues_only_order_without_reading_voxels(room_peers):
    peers, log, _ = room_peers
    first = peers[0]
    first.node.GetSegmentation().SetSegmentIndex("C", 0)
    first.controller._read_mask_crop = lambda *args: pytest.fail("Order read a voxel mask")
    first.controller._probe_segment_revisions()
    operations = first.controller.outgoing
    assert len(operations) == 1
    assert operations[0]["segment_order"] == ["C", "A", "B"]
    assert not first.controller.dirty_segments
    stored = log.commit(operations[0], first.name)
    first.finish_push(accepted=operations)
    for peer in peers:
        peer.apply(stored)
        assert peer.node.GetSegmentation().GetSegmentIDs() == ["C", "A", "B"]
        peer.controller._probe_segment_revisions()
        assert not peer.controller.outgoing, "Received order echoed back"
        assert all(not np.any(peer.mask(sid)) for sid in ("A", "B", "C"))
        assert not peer.errors
    assert not first.controller.awaiting_echo


def test_concurrent_orders_merge_unknown_labels_and_never_revive_deleted(room_peers):
    peers, log, _ = room_peers
    operations = [log.commit(order_operation(["C", "A", "B"]), "Jenny"),
                  log.commit(order_operation(["B", "C", "A"]), "Pauline")]
    operations.append(log.commit({"client_operation_id": str(uuid.uuid4()),
        "segment_id": "D", "segment_name": "D", "metadata_update": True,
        **h.collaboration.encode_metadata_update(h.SHAPE)}, "creator"))
    operations.append(log.commit({"client_operation_id": str(uuid.uuid4()),
        "segment_id": "C", "segment_name": "C", "segment_deleted": True,
        **h.collaboration.encode_metadata_update(h.SHAPE)}, "creator"))
    # A drag started before D's creation and C's deletion; omit neither live D
    # nor revive C just because its ID remains in the stale order list.
    operations.append(log.commit(order_operation(["A", "C", "B"]), "Jenny"))
    for peer in peers:
        for index, operation in enumerate(operations):
            peer.apply(operation)
            if index == 1:
                assert peer.node.GetSegmentation().GetSegmentIDs() == ["B", "C", "A"]
        assert peer.node.GetSegmentation().GetSegmentIDs() == ["A", "B", "D"]
        assert not peer.errors


def test_drag_waiting_for_native_callback_is_not_lost_to_remote_order(room_peers):
    peers, log, _ = room_peers
    local = peers[0]
    local.node.GetSegmentation().SetSegmentIndex("C", 0)
    local.apply(log.commit(order_operation(["B", "A", "C"]), "Pauline"))
    assert local.controller.outgoing[0]["segment_order"] == ["C", "A", "B"]
    stored = log.commit(local.controller.outgoing[0], "Jenny")
    for peer in peers:
        if peer is not local:
            peer.apply(log.operations[-2])
        peer.apply(stored)
        assert peer.node.GetSegmentation().GetSegmentIDs() == ["C", "A", "B"]


def test_viewer_drag_is_local_reverted_without_publishing(room_peers):
    peers, _, _ = room_peers
    peer = peers[0]
    peer.controller._current_role = lambda: "viewer"
    peer.node.GetSegmentation().SetSegmentIndex("C", 0)
    peer.controller._probe_segment_revisions()
    assert peer.node.GetSegmentation().GetSegmentIDs() == ["A", "B", "C"]
    assert not peer.controller.outgoing


def test_compaction_rejoin_and_history_preserve_order_and_masks(tmp_path, room_peers):
    peers, log, scene = room_peers
    client = h.collaboration.SharedFolderRoomClient(tmp_path, "Jenny")
    room = client.join("ordering", "a" * 64)
    for operation in log.operations:
        client.push_operation(room["id"], operation)
    operation = order_operation(["C", "B", "A"])
    client.push_operation(room["id"], operation)
    peers[0].apply(client.operations(room["id"], 3)[0])
    snapshot = peers[0].controller._snapshot_operations()
    manifest = client.publish_room_snapshot(room["id"], snapshot, expected_sequence=4)
    assert manifest and not manifest.get("skipped")
    late = h.Peer("late", scene)
    for stored in client.operations(room["id"], 0):
        late.apply(stored)
    assert late.node.GetSegmentation().GetSegmentIDs() == ["C", "B", "A"]
    assert all(not np.any(late.mask(sid)) for sid in ("A", "B", "C"))
    historical = client.state_at_sequence(room["id"], manifest["last_sequence"])
    assert historical[-1]["segment_order"] == ["C", "B", "A"]
    client.leave(room["id"])


def test_stale_order_anchor_cannot_hide_a_deletion_from_compaction(tmp_path, room_peers):
    peers, log, _ = room_peers
    client = h.collaboration.SharedFolderRoomClient(tmp_path, "Jenny")
    room = client.join("order-after-delete", "a" * 64)
    for operation in log.operations:
        client.push_operation(room["id"], operation)
    client.push_operation(room["id"], {"client_operation_id": str(uuid.uuid4()),
        "segment_id": "C", "segment_name": "C", "segment_deleted": True,
        **h.collaboration.encode_metadata_update(h.SHAPE)})
    client.push_operation(room["id"], order_operation(["C", "B", "A"]))
    for operation in client.operations(room["id"], 3):
        peers[0].apply(operation)
    client.publish_room_snapshot(room["id"], peers[0].controller._snapshot_operations(), expected_sequence=5)
    # The other peer missed the deletion and can now read only the checkpoint.
    for operation in client.operations(room["id"], 3):
        peers[1].apply(operation)
    assert peers[1].node.GetSegmentation().GetSegmentIDs() == ["B", "A"]
    client.leave(room["id"])


@pytest.mark.parametrize("ids", [["A", "A"], [], ["A", 3], [" "], ["x" * 201]])
def test_invalid_order_rejected(ids):
    operation = order_operation(["A"])
    operation["segment_order"] = ids
    assert h.collaboration.operation_defect(operation) is not None


def test_order_cannot_hide_voxel_changes():
    operation = order_operation(["A"])
    operation.update(h.collaboration.encode_mask_delta(np.zeros(h.SHAPE, np.uint8), np.ones(h.SHAPE, np.uint8)))
    assert h.collaboration.operation_defect(operation) is not None


def test_work_log_keeps_only_valid_typed_order_lists(tmp_path):
    logger = h.collaboration.RoomWorkLog(tmp_path)
    details = h.collaboration.operation_details(order_operation(["B", "A"]))
    assert logger._record("segmentation.operation", "actor", "session", details)["details"]["segment_order"] == ["B", "A"]
    bad = {"segment_order": ["A", {"secret": "not a label ID"}]}
    assert "segment_order" not in logger._record("segmentation.operation", "actor", "session", bad)["details"]


def test_http_roundtrip_persists_order(client, headers):
    room = client.post("/api/live/rooms/join", json={"room_name": "order-test", "volume_signature": "a" * 64}, headers=headers)
    assert room.status_code == 200
    room_id = room.json()["id"]
    operation = order_operation(["C", "A", "B"])
    response = client.post(f"/api/live/rooms/{room_id}/operations", json=operation, headers=headers)
    assert response.status_code == 201, response.text
    assert response.json()["segment_order"] == ["C", "A", "B"]
    replay = client.get(f"/api/live/rooms/{room_id}/operations", headers=headers).json()
    assert replay[0]["segment_order"] == ["C", "A", "B"]
