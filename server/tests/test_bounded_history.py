"""A recent history request must not read every old voxel payload."""
import json
import zipfile

from tests.controller_harness import collaboration as c


def record(sequence):
    return {'sequence': sequence, 'segment_id': 'A', 'author': 'alice',
            'client_operation_id': str(sequence), 'changed_voxels': 1}


def test_history_reads_only_the_requested_loose_payloads(tmp_path, monkeypatch):
    client = c.SharedFolderRoomClient(tmp_path, 'alice')
    room = client.join('history', 'v'*64)
    folder = client._room_path/'operations'
    for i in range(1, 501):
        (folder/f'{i:020d}--{i:020x}.json').write_text(json.dumps(record(i)))
    calls = []
    original = c._read_json_file
    def read(path):
        if path.parent == folder:
            calls.append(path)
        return original(path)
    monkeypatch.setattr(c, '_read_json_file', read)
    assert [r['sequence'] for r in client.room_history(room['id'], 5)] == list(range(496, 501))
    assert len(calls) <= 5, f'Read {len(calls)} old operation payloads for five rows'


def test_history_reads_only_recent_archive_members(tmp_path, monkeypatch):
    client = c.SharedFolderRoomClient(tmp_path, 'alice')
    room = client.join('history', 'v'*64)
    archive = client._room_path/'operation-archives'/'00000000000000000001-00000000000000000500--qa.zip'
    with zipfile.ZipFile(archive, 'w') as bundle:
        for i in range(1, 501):
            bundle.writestr(f'{i:020d}--{i:020x}.json', json.dumps(record(i)))
    calls = []
    original = zipfile.ZipFile.read
    def read(bundle, name, *args, **kwargs):
        calls.append(name)
        return original(bundle, name, *args, **kwargs)
    monkeypatch.setattr(zipfile.ZipFile, 'read', read)
    assert [r['sequence'] for r in client.room_history(room['id'], 5)] == list(range(496, 501))
    assert len(calls) <= 5, f'Decompressed {len(calls)} old masks for five rows'


def test_recent_history_does_not_open_ancient_archives(tmp_path, monkeypatch):
    client = c.SharedFolderRoomClient(tmp_path, 'alice')
    room = client.join('history', 'v'*64)
    for i in range(101, 106):
        (client._room_path/'operations'/f'{i:020d}--{i:020x}.json').write_text(json.dumps(record(i)))
    old = client._room_path/'operation-archives'/'00000000000000000001-00000000000000000100--old.zip'
    with zipfile.ZipFile(old, 'w') as bundle:
        bundle.writestr(f'{1:020d}--{1:020x}.json', json.dumps(record(1)))
    original = zipfile.ZipFile
    def open_archive(path, *args, **kwargs):
        assert path != old, 'Recent history unnecessarily opened an ancient archive'
        return original(path, *args, **kwargs)
    monkeypatch.setattr(c.zipfile, 'ZipFile', open_archive)
    assert [r['sequence'] for r in client.room_history(room['id'], 5)] == list(range(101, 106))


def test_recent_history_recovers_a_file_archived_during_the_read(tmp_path, monkeypatch):
    client = c.SharedFolderRoomClient(tmp_path, 'alice')
    room = client.join('history', 'v'*64)
    for i in range(1, 11):
        (client._room_path/'operations'/f'{i:020d}--{i:020x}.json').write_text(json.dumps(record(i)))
    original = c._read_json_file
    def read(path):
        if path.parent.name == 'operations' and path.name.startswith(f'{10:020d}'):
            archive = client._room_path/'operation-archives'/'00000000000000000010-00000000000000000010--race.zip'
            with zipfile.ZipFile(archive, 'w') as bundle:
                bundle.writestr(path.name, json.dumps(record(10)))
            path.unlink()
        return original(path)
    monkeypatch.setattr(c, '_read_json_file', read)
    assert [r['sequence'] for r in client.room_history(room['id'], 5)] == list(range(6, 11))
