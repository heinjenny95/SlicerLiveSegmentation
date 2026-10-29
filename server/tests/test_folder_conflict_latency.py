"""SSH edit ACKs must not reread entire checkpoint groups per tile."""
import json
import threading
from unittest.mock import MagicMock

import numpy as np

from tests import controller_harness as h
from tests.test_room_snapshot_safety import operation_payload


def test_ssh_folder_push_uses_deferred_conflict_analysis(monkeypatch):
    scene, _ = h.install_fake_slicer(monkeypatch)
    c = h.Peer('Alice', scene).controller
    client = MagicMock()
    client.transport_kind = 'ssh-folder'
    operation = {'client_operation_id':'one-edit', 'segment_id':'A'}
    c._edit_push_lane(c._session_token, client, 'room', [operation], [], '')
    client.push_operation.assert_called_once_with('room', operation, defer_conflicts=True)


def test_conflict_scan_skips_certified_checkpoint_payloads(tmp_path, monkeypatch):
    c = h.collaboration
    client = c.SharedFolderRoomClient(tmp_path, 'Alice')
    room = client.join('study', 'v'*64)
    root = client._room_path
    empty = np.zeros((3,)*3, np.uint8)
    painted = np.ones_like(empty)
    for seq in range(2, 302):
        item = dict(operation_payload('A', empty, painted), sequence=seq, author='Bob', system_snapshot=True)
        (root/'operations'/f'{seq:020d}--{seq:020x}.json').write_text(json.dumps(item))
    (root/'snapshots'/('00000000000000000301--'+'a'*32+'.json')).write_text(json.dumps(
        dict(id='a'*32, first_sequence=2, last_sequence=301, before_sequence=1)))
    previous = dict(operation_payload('A', empty, painted), sequence=302, author='Bob')
    (root/'operations'/'00000000000000000302--0000000000000000012e.json').write_text(json.dumps(previous))
    c._write_json_atomic(root/'sequence-state.json', {'latest_sequence':302})
    client._write_sequence_head(root, 302)
    original = c._read_json_file
    reads = []
    def read(path):
        if path.parent.name == 'operations':
            reads.append(client._operation_sequence(path))
        return original(path)
    monkeypatch.setattr(c, '_read_json_file', read)
    operation = dict(operation_payload('A', empty, painted), base_sequence=1)
    conflicts = client._analyze_operation_conflicts(room['id'], root, operation,
                                                   operation, 303, 1)
    assert conflicts, 'The actual conflicting peer edit was lost'
    assert reads == [302], 'Checkpoint voxel payloads were read for conflict analysis'


def test_deferred_analysis_coalesces_on_one_worker_and_keeps_room_boundaries(tmp_path, monkeypatch):
    client = h.collaboration.SharedFolderRoomClient(tmp_path, 'Alice')
    calls, threads = [], set()
    monkeypatch.setattr(client, '_conflict_candidates', lambda path, *unused: [str(path)])
    def analyze(*job, candidates):
        calls.append((str(job[1]), candidates))
        threads.add(threading.get_ident())
    monkeypatch.setattr(client, '_analyze_operation_conflicts', analyze)
    for index in range(50):
        path = tmp_path/('roomA' if index % 2 else 'roomB')
        client._queue_conflict_analysis('room', path, {}, {}, index+2, 1)
    client._conflict_queue.join()
    assert len(calls) == 50 and len(threads) == 1
    assert all(candidate == [path] for path, candidate in calls)
