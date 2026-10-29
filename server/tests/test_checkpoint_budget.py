"""Large automatic SSH checkpoints must not monopolize active-room I/O."""
from tests import controller_harness as h
from tests.test_checkpoint_lane import controller


def test_large_ssh_checkpoint_is_manual_while_edits_remain_durable(monkeypatch):
    c = controller(monkeypatch)
    c.client.transport_kind = 'ssh-folder'
    c._snapshot_operations.return_value = [{'segment_id':str(i)} for i in range(300)]
    c._start_edit_workers(100)
    if c._snapshot_worker:
        c._snapshot_worker.join(2)
    c.client.publish_room_snapshot.assert_not_called()
    c._snapshot_requested = True
    c._start_edit_workers(106)
    c._snapshot_worker.join(2)
    c.client.publish_room_snapshot.assert_called_once()


def test_stale_checkpoint_rejects_before_waiting_for_artifact_queue(tmp_path, monkeypatch):
    client = h.collaboration.SharedFolderRoomClient(tmp_path, 'Alice')
    room = client.join('study', 'v'*64)
    client._write_sequence_head(client._room_path, 2)
    def blocked_join():
        raise AssertionError('Stale checkpoint waited for background I/O')
    monkeypatch.setattr(client._artifact_queue, 'join', blocked_join)
    result = client.publish_room_snapshot(room['id'], [{'segment_id':'A'}], expected_sequence=1)
    assert result['skipped']=='stale'
