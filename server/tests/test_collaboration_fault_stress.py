"""Real folder transport + controller replay with retries, lag and compaction."""
import numpy as np
import pytest

from tests import controller_harness as h


@pytest.mark.parametrize("seed", [7, 31, 93])
def test_interleaved_edits_lost_ack_compaction_offline_work_and_rejoin(tmp_path, monkeypatch, seed):
    monkeypatch.setattr(h, "SHAPE", (32,)*3)
    scene, _ = h.install_fake_slicer(monkeypatch)
    peers = [h.Peer("A-user", scene), h.Peer("B-user", scene)]
    clients = [h.collaboration.SharedFolderRoomClient(tmp_path, peer.name) for peer in peers]
    room = clients[0].join("fault-stress", "f"*64)
    clients[1].join("fault-stress", "f"*64)
    clients[1].presence(room["id"], {})
    labels = ["A", "B"]
    colors = ["#FF0000", "#0000FF"]
    expected = {sid: np.zeros(h.SHAPE, np.uint8) for sid in labels}
    rng = np.random.default_rng(seed)

    def pull(index):
        peer = peers[index]
        for op in clients[index].operations(room["id"], peer.controller.last_sequence, limit=500):
            peer.apply(op)

    def push(index, lose_ack=False):
        peer, client = peers[index], clients[index]
        peer.prepare()
        for op in list(peer.controller.outgoing):
            first = client.push_operation(room["id"], op)
            if lose_ack:
                # The first write committed but its reply never reached sender.
                retry = client.push_operation(room["id"], op)
                assert retry["sequence"] == first["sequence"] and retry["duplicate"]
            peer.finish_push(accepted=[op])

    for sid, color in zip(labels, colors):
        clients[0].push_operation(room["id"], {"client_operation_id": "create-"+sid,
            "segment_id": sid, "segment_name": sid, "color_hex": color,
            "metadata_update": True, **h.collaboration.encode_metadata_update(h.SHAPE)})
    pull(0)
    pull(1)
    for round_index in range(12):
        for actor in rng.permutation(2):
            sid = labels[actor]
            index = (2+20*int(actor), int(rng.integers(1, 31)), int(rng.integers(1, 31)))
            peers[actor].paint(sid, index)
            expected[sid][index] = 1
            push(actor, lose_ack=(round_index % 4 == 0))
        pull(0)
        # An offline/slow receiver keeps its local view and catches up later.
        if round_index % 3 == 2:
            pull(1)
    pull(1)
    peers[1].paint("B", (23, 15, 15))
    expected["B"][23, 15, 15] = 1
    before_offline = peers[1].mask("B").copy()
    snapshot = peers[0].controller._snapshot_operations()
    manifest = clients[0].publish_room_snapshot(room["id"], snapshot,
                    expected_sequence=peers[0].controller.last_sequence)
    assert manifest is not None
    for operation in clients[1].operations(room["id"], peers[1].controller.last_sequence, limit=500):
        peers[1].apply(operation)
        np.testing.assert_array_equal(peers[1].mask("B"), before_offline)
    push(1, lose_ack=True)
    pull(0)
    pull(1)
    clients[1].leave(room["id"])
    assert "B-user" not in {user["user"] for user in clients[0].presence(room["id"], {})}
    clients[1].join("fault-stress", "f"*64)
    clients[1].presence(room["id"], {})
    assert "B-user" in {user["user"] for user in clients[0].presence(room["id"], {})}
    late = h.Peer("late", scene)
    late_client = h.collaboration.SharedFolderRoomClient(tmp_path, "late")
    late_client.join("fault-stress", "f"*64)
    for operation in late_client.operations(room["id"], 0, limit=500):
        late.apply(operation)
    for peer in [*peers, late]:
        for sid, color in zip(labels, colors):
            np.testing.assert_array_equal(peer.mask(sid), expected[sid])
            assert peer.node.GetSegmentation().GetSegment(sid).GetColor() == tuple(int(color[i:i+2], 16)/255 for i in (1, 3, 5))
        assert not peer.errors
    for client in [*clients, late_client]:
        client.leave(room["id"])
