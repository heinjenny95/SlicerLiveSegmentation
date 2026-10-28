"""A slow full checkpoint must not occupy the live edit sender."""
import threading
from unittest.mock import MagicMock

from tests import controller_harness as h


def controller(monkeypatch):
    scene, _ = h.install_fake_slicer(monkeypatch)
    c = h.Peer('alice', scene).controller
    c.last_sequence = 200
    c._last_edit_activity_epoch = 0
    c._snapshot_operations = MagicMock(return_value=[{'segment_id': 'A'}])
    return c


def test_slow_checkpoint_does_not_hold_new_edit_or_acknowledgement(monkeypatch):
    c = controller(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    def publish(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {'skipped': 'stale'}
    c.client.publish_room_snapshot.side_effect = publish
    try:
        c._start_edit_workers(100)
        assert entered.wait(2)
        c.outgoing = [{'segment_id': 'B', 'client_operation_id': 'new-edit'}]
        c._start_edit_workers(101)
        c._edit_push_worker.join(2)
        assert not c._edit_push_worker.is_alive()
        c._drain_worker_results()
        assert c.outgoing == [] and c.awaiting_echo[0]['client_operation_id'] == 'new-edit'
        c._start_edit_workers(102)
        c._snapshot_operations.assert_called_once()
        c.client.push_operation.assert_called_once()
        assert c._snapshot_worker.is_alive()
    finally:
        release.set()
        if c._snapshot_worker:
            c._snapshot_worker.join(2)


def test_automatic_checkpoint_waits_for_room_idle_and_cools_down(monkeypatch):
    c = controller(monkeypatch)
    c._last_edit_activity_epoch = 90
    c._start_edit_workers(100)
    c._snapshot_operations.assert_not_called()
    c.client.publish_room_snapshot.return_value = {'skipped': 'stale'}
    c._start_edit_workers(130)
    c._snapshot_worker.join(2)
    c._drain_worker_results()
    c._start_edit_workers(131)
    c._snapshot_operations.assert_called_once()


def test_explicit_snapshot_retries_same_label_after_real_stale_guard(monkeypatch):
    c = controller(monkeypatch)
    c._last_edit_activity_epoch = 99
    c._snapshot_requested, c._snapshot_label = True, 'manual'
    c.client.publish_room_snapshot.return_value = {'skipped': 'stale'}
    c._start_edit_workers(100)
    c._snapshot_worker.join(2)
    c._drain_worker_results()
    assert c._snapshot_requested and c._snapshot_label == 'manual'
    c.client.publish_room_snapshot.assert_called_once_with(
        'room', [{'segment_id': 'A'}], compact=True, label='manual', expected_sequence=200)
    c._start_edit_workers(101)
    c._snapshot_operations.assert_called_once()


def test_checkpoint_timeout_keeps_manual_request_and_does_not_gate_edit_sender(monkeypatch):
    c = controller(monkeypatch)
    c._snapshot_requested, c._snapshot_label = True, 'manual'
    c.client.publish_room_snapshot.side_effect = h.collaboration.LiveCollaborationError('SSH snapshot timed out')
    c._start_edit_workers(100)
    c._snapshot_worker.join(2)
    c._drain_worker_results()
    assert c._snapshot_requested and c._snapshot_label == 'manual'
    assert not c._transport_retry_ready('checkpoint')
    assert c._transport_retry_ready('edit-push')
    c.outgoing = [{'segment_id': 'B', 'client_operation_id': 'retry-safe'}]
    c._start_edit_workers(101)
    c._edit_push_worker.join(2)
    c.client.push_operation.assert_called_once()
