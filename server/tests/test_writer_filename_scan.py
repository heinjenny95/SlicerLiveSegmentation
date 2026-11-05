"""Fresh writers scan immutable names once without trusting cached watermarks."""
import hashlib
import os
from fnmatch import fnmatch
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from tests.test_room_snapshot_safety import SHAPE, SIGNATURE, operation_payload
from tests.test_room_snapshot_safety import collaboration_module as c


def writer(tmp_path, monkeypatch):
    client = c.SharedFolderRoomClient(tmp_path, "writer")
    room_id = client.join("writer scan", SIGNATURE)["id"]
    state_path = client._room_path / "sequence-state.json"
    state = c._read_json_file(state_path) if state_path.is_file() else {"latest_sequence": 0}
    c._write_json_atomic(state_path, {**state, "label_deletions": {}})
    monkeypatch.setattr(client, "_queue_operation_artifacts", lambda *args: None)
    monkeypatch.setattr(client, "_queue_conflict_analysis", lambda *args: None)
    empty = np.zeros(SHAPE, np.uint8)
    painted = empty.copy()
    painted.flat[0] = 1
    operation = operation_payload("A", empty, painted, operation_id="fresh-writer-scan")
    return client, room_id, operation


def watch_walks(monkeypatch, operations_path):
    walks = []
    original = os.scandir

    def scan(path):
        if isinstance(path, (str, os.PathLike)) and Path(path) == operations_path:
            walks.append(str(path))
        return original(path)

    monkeypatch.setattr(os, "scandir", scan)
    return walks


def test_fresh_writer_uses_one_immutable_directory_walk(tmp_path, monkeypatch):
    client, room_id, operation = writer(tmp_path, monkeypatch)
    walks = watch_walks(monkeypatch, client._room_path / "operations")
    result = client.push_operation(room_id, operation, defer_conflicts=True)
    assert result == {"sequence": 1, "duplicate": False}
    assert len(walks) == 1, "Legacy duplicate and next-slot globs each enumerate all loose operations"


def test_inline_and_receipt_retries_return_before_scanning_names(tmp_path, monkeypatch):
    client, room_id, operation = writer(tmp_path, monkeypatch)
    client.push_operation(room_id, operation, defer_conflicts=True)
    client._persist_operation_artifacts(client._room_path, {
        **operation, "sequence": 1, "author": "writer", "created_at": c._utc_iso(),
    })
    walks = watch_walks(monkeypatch, client._room_path / "operations")
    assert client.push_operation(room_id, operation)["duplicate"]
    state_path = client._room_path / "sequence-state.json"
    state = c._read_json_file(state_path)
    c._write_json_atomic(state_path, {**state, "inline_operations": []})
    assert client.push_operation(room_id, operation) == {"sequence": 1, "duplicate": True}
    assert walks == []


def test_legacy_duplicate_keeps_lowest_path_match_without_a_receipt(tmp_path, monkeypatch):
    client, room_id, operation = writer(tmp_path, monkeypatch)
    operation_hash = hashlib.sha256(operation["client_operation_id"].encode()).hexdigest()[:20]
    for sequence in (9, 2):
        path = client._room_path / "operations" / f"{sequence:020d}--{operation_hash}.json"
        c._write_json_atomic(path, {**operation, "sequence": sequence, "author": "writer"})
    walks = watch_walks(monkeypatch, client._room_path / "operations")
    assert client.push_operation(room_id, operation) == {"sequence": 2, "duplicate": True}
    assert len(walks) == 1


def test_stale_equal_heads_and_an_immutable_gap_cannot_reuse_old_sequence(tmp_path, monkeypatch):
    client, room_id, operation = writer(tmp_path, monkeypatch)
    previous = {**operation, "client_operation_id": "orphan-after-interrupted-head",
                "sequence": 2, "author": "peer", "created_at": c._utc_iso()}
    _, path, _ = client._operation_paths(client._room_path, previous)
    c._write_json_atomic(path, previous)
    assert client._read_sequence_head(client._room_path) == 0
    result = client.push_operation(room_id, operation, defer_conflicts=True)
    assert result["sequence"] == 3, "Both mutable heads can be stale even if their next slot is absent"


def test_fused_name_scan_preserves_filename_and_legacy_glob_semantics(tmp_path, monkeypatch):
    target = "1234567890abcdefabcd"
    names = [f"{number:020d}--{target}.json" for number in (2, 12, 3)] + [
        "00000000000000099999--abc.json", "00000000000000999999--ABC.json",
        "00000000000009999999--abc.JSON", "00000000000099999999--g.json",
        "0000000000000000003--abc.json", "0000000000000000000003--abc.json",
        "not-a-sequence--" + target + ".json", "--" + target + ".json",
        "00000000000099999999--a.json\n", "\u0660" * 18 + "\u0664\u0662--abc.json",
        "00000000000000000001--" + target.upper() + ".JSON",
    ]
    class Entries:
        def __enter__(self):
            return iter(SimpleNamespace(name=name) for name in names)

        def __exit__(self, *unused):
            pass

    monkeypatch.setattr(os, "scandir", lambda unused: Entries())
    latest, legacy = c.SharedFolderRoomClient._scan_writer_operation_names(tmp_path, target)
    expected = max(c.SharedFolderRoomClient._operation_sequence(Path(name)) or 0
                   for name in names if fnmatch(name, "*.json"))
    expected_legacy = min(Path(name) for name in names if fnmatch(name, "*--" + target + ".json"))
    assert latest == expected
    assert legacy == tmp_path / expected_legacy
