"""Recovery retries original identities and scopes them to one room/user."""
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'LiveSegmentation'/'LiveSegmentationLib'))

from work_log import RoomWorkLog  # noqa: E402
from work_time_transfer import WorkIntervalTransfer  # noqa: E402


def sample(identifier):
    return dict(sample_id=identifier, session_id='session-one', measurement_version=1,
                category='work', start_epoch=100, end_epoch=105, duration_seconds=5,
                idle_threshold_seconds=60, nninteractive_busy=False)


def test_restart_recovers_only_matching_user_and_acknowledged_ids_stay_acked(tmp_path):
    old = RoomWorkLog(tmp_path/('a'*32), create_room=True)
    one, two = sample('original-sample-1'), sample('original-sample-2')
    old.submit('work.interval', 'Alice', 'session-one', one)
    old.submit('work.interval', 'Bob', 'session-one', sample('another-person'))
    assert old.flush(3)
    current = RoomWorkLog(tmp_path/('b'*32), create_room=True)
    current.submit('work.interval', 'Alice', 'session-one', two)
    assert current.flush(3)
    transfer = WorkIntervalTransfer(current, 'Alice')
    batch, _ = transfer.choose([two])
    assert {s['sample_id'] for s in batch} == {one['sample_id'], two['sample_id']}
    transfer.acknowledge([one['sample_id']])
    restarted = WorkIntervalTransfer(current, 'Alice')
    batch, _ = restarted.choose([two])
    assert [s['sample_id'] for s in batch] == [two['sample_id']]
    restarted.acknowledge([two['sample_id']])
    assert not restarted.has_pending
    assert old.room_path.joinpath('work-log-data').is_dir()


def test_unknown_server_receipt_cannot_acknowledge_an_unsent_interval(tmp_path):
    log = RoomWorkLog(tmp_path/('c'*32), create_room=True)
    log.room_path.mkdir(parents=True)
    transfer = WorkIntervalTransfer(log, 'Alice')
    one, two = sample('pending-sample-one'), sample('pending-sample-two')
    batch, _ = transfer.choose([one, two], limit=1)
    sent = batch[0]['sample_id']
    other = two['sample_id'] if sent == one['sample_id'] else one['sample_id']
    transfer.acknowledge([other])
    assert transfer.has_pending
    batch, _ = transfer.choose([])
    assert {s['sample_id'] for s in batch} == {one['sample_id'], two['sample_id']}


def test_controller_retries_old_journal_even_without_new_current_samples(tmp_path, monkeypatch):
    from tests import controller_harness as h
    scene, _ = h.install_fake_slicer(monkeypatch)
    c = h.Peer('Alice', scene).controller
    old = RoomWorkLog(tmp_path/('d'*32), create_room=True)
    item = sample('prior-session-sample')
    old.submit('work.interval', 'Alice', 'session-one', item)
    assert old.flush(3)
    current = RoomWorkLog(tmp_path/('e'*32), create_room=True)
    current.room_path.mkdir()
    current.interval_transfer = WorkIntervalTransfer(current, 'Alice')
    client = MagicMock()
    client.record_work_intervals.return_value = {'accepted_ids':[item['sample_id']]}
    c._work_time_lane(c._session_token, client, 'room', [], current)
    assert client.record_work_intervals.call_args.args[1] == [item]
    assert not current.interval_transfer.has_pending


def test_time_transfer_reports_error_text_and_elapsed_time(monkeypatch):
    from tests import controller_harness as h
    scene, _ = h.install_fake_slicer(monkeypatch)
    c = h.Peer('Alice', scene).controller
    c._diagnostic_event = MagicMock()
    c.work_time = MagicMock()
    local = MagicMock()
    local.flush.return_value = True
    local.interval_transfer = None
    client = MagicMock()
    client.record_work_intervals.side_effect = RuntimeError('Explicit transfer failure')
    c._work_time_lane(c._session_token, client, 'room', [sample('sample-transfer-error')], local)
    result = c._worker_results.get_nowait()
    assert 'duration' in result
    c._worker_results.put(result)
    c._drain_worker_results()
    assert any(call.args[0]=='work_time.transfer' and call.kwargs.get('error')=='Explicit transfer failure'
               for call in c._diagnostic_event.call_args_list)
