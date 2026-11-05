"""Archive bounds exclude old conflict windows without altering candidate data."""

import json
import zipfile

import pytest

from tests.test_room_snapshot_safety import SIGNATURE
from tests.test_room_snapshot_safety import collaboration_module as c


def archived_room(tmp_path, filename, sequences):
    client = c.SharedFolderRoomClient(tmp_path, "reader")
    client.join("room", SIGNATURE)
    archive = client._room_path / "operation-archives" / filename
    with zipfile.ZipFile(archive, "w") as bundle:
        for sequence in sequences:
            name = f"{sequence:020d}--{sequence:020x}.json"
            bundle.writestr(name, json.dumps({"sequence": sequence, "source": "archive"}))
    return client, archive


@pytest.mark.parametrize("first,last,after,before", [(1, 10, 10, 20), (15, 25, 10, 15)])
def test_wholly_outside_archive_is_not_opened(tmp_path, monkeypatch, first, last, after, before):
    client, archive = archived_room(tmp_path, f"{first:020d}-{last:020d}--abcdef.zip", range(first, last + 1))
    real_zip = c.zipfile.ZipFile

    def guarded_zip(path, *args, **kwargs):
        assert path != archive, "An archive outside the conflict window was opened"
        return real_zip(path, *args, **kwargs)

    monkeypatch.setattr(c.zipfile, "ZipFile", guarded_zip)
    assert client._conflict_candidates(client._room_path, after, before) == []


@pytest.mark.parametrize("filename", [
    "00000000000000000001-00000000000000000015--abcdef.zip",
    "00000000000000000020-00000000000000000001--reversed.zip",
    "legacy-history.zip",
    "1-15--short-invalid-range.zip",
])
def test_intersecting_and_untrusted_archive_names_preserve_exact_candidates(tmp_path, monkeypatch, filename):
    client, archive = archived_room(tmp_path, filename, [10, 11, 12, 13, 14, 15])
    real_zip, opened = c.zipfile.ZipFile, []

    def observed_zip(path, *args, **kwargs):
        opened.append(path)
        return real_zip(path, *args, **kwargs)

    monkeypatch.setattr(c.zipfile, "ZipFile", observed_zip)
    assert client._conflict_candidates(client._room_path, 10, 15) == [
        {"sequence": sequence, "source": "archive"} for sequence in range(11, 15)
    ]
    assert archive in opened


def test_filtered_archive_scan_keeps_loose_precedence_and_system_ranges(tmp_path):
    client, _ = archived_room(tmp_path, "00000000000000000001-00000000000000000020--abcdef.zip", [11, 12, 13, 14])
    c._write_json_atomic(client._room_path / "operations" / f"{12:020d}--abcdef.json",
                         {"sequence": 12, "source": "loose"})
    c._write_json_atomic(client._room_path / "snapshots" / "system.json",
                         {"first_sequence": 13, "last_sequence": 13})
    c._write_json_atomic(client._room_path / "sequence-state.json",
                         {"inline_operations": [{"sequence": 12, "source": "inline"}]})
    assert client._conflict_candidates(client._room_path, 10, 15) == [
        {"sequence": 11, "source": "archive"},
        {"sequence": 12, "source": "loose"},
        {"sequence": 14, "source": "archive"},
    ]
