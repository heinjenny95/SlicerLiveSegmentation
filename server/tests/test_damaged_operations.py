"""One damaged operation must not stop a room for everybody (review finding L-09)."""

from __future__ import annotations

import base64
import json
import os
import queue
import sys
import time
import types
import zlib
from collections import deque
from pathlib import Path

import numpy as np
import pytest

LIVE_CORE = Path(__file__).resolve().parents[2] / "LiveSegmentation" / "LiveSegmentationLib"
sys.path.insert(0, str(LIVE_CORE))

import collaboration as collaboration_module  # noqa: E402
from collaboration import (  # noqa: E402
    LiveCollaborationController,
    LiveCollaborationError,
    SharedFolderRoomClient,
    decode_mask_delta,
    encode_mask_delta,
    operation_defect,
)

SIGNATURE = "s" * 64
SHAPE = (4, 4, 4)


def operation_payload(segment_id, previous, current, operation_id="op"):
    return {
        "client_operation_id": operation_id,
        "segment_id": segment_id,
        "segment_name": segment_id,
        "color_hex": "#37E8B8",
        **encode_mask_delta(previous, current),
    }


def painted(index):
    mask = np.zeros(SHAPE, np.uint8)
    mask[index] = 1
    return mask


def damaged_variants():
    good = operation_payload("S1", np.zeros(SHAPE, np.uint8), painted((0, 0, 0)))
    return {
        "garbage payload": {**good, "payload": base64.b64encode(b"not zlib").decode()},
        "payload shorter than its bounds": {
            **good,
            "payload": base64.b64encode(zlib.compress(b"\x01")).decode(),
        },
        "bounds outside the volume": {**good, "voxel_bbox": [0, 1, 0, 1, 0, 99]},
        "negative bounds": {**good, "voxel_bbox": [-1, 1, 0, 1, 0, 1]},
        "empty bounds": {**good, "voxel_bbox": [1, 1, 0, 1, 0, 1]},
        "unknown encoding": {**good, "encoding": "something-else"},
        "missing bounds": {key: value for key, value in good.items() if key != "voxel_bbox"},
    }


@pytest.mark.parametrize("name", sorted(damaged_variants()))
def test_damaged_operation_is_refused_before_it_gets_a_sequence_number(tmp_path, name):
    alice = SharedFolderRoomClient(tmp_path, "alice")
    room_id = alice.join("room", SIGNATURE)["id"]
    assert operation_defect(damaged_variants()[name]) is not None
    with pytest.raises(LiveCollaborationError, match="damaged live operation"):
        alice.push_operation(room_id, damaged_variants()[name])
    assert not list((alice._room_path / "operations").glob("*.json"))
    good = operation_payload("S1", np.zeros(SHAPE, np.uint8), painted((0, 0, 0)), "a-1")
    assert alice.push_operation(room_id, good)["sequence"] == 1


def test_valid_operations_and_deletions_have_no_defect():
    good = operation_payload("S1", np.zeros(SHAPE, np.uint8), painted((0, 0, 0)))
    assert operation_defect(good) is None
    assert operation_defect(good, SHAPE) is None
    assert operation_defect(good, (8, 8, 8)) == "operation belongs to a different volume geometry"
    assert operation_defect({"segment_deleted": True, "segment_id": "S1"}) is None


def test_decoding_never_inflates_more_than_the_bounds_allow():
    good = operation_payload("S1", np.zeros(SHAPE, np.uint8), painted((0, 0, 0)))
    bomb = {
        **good,
        "payload": base64.b64encode(zlib.compress(b"\x00" * (64 * 1024 * 1024))).decode(),
    }
    started = time.perf_counter()
    with pytest.raises(ValueError, match="payload length"):
        decode_mask_delta(bomb)
    assert operation_defect(bomb) is not None
    assert time.perf_counter() - started < 2.0


def write_foreign_operation(room_path, sequence, operation, age_seconds=120.0, raw=None):
    """An operation file as an older client or a damaged NAS would leave it."""
    path = room_path / "operations" / f"{sequence:020d}--{'ab' * 10}.json"
    path.write_text(
        raw if raw is not None else json.dumps({**operation, "sequence": sequence}),
        encoding="utf-8",
    )
    old = time.time() - age_seconds
    os.utime(path, (old, old))
    (room_path / "sequence-head.json").unlink(missing_ok=True)
    (room_path / "sequence-state.json").unlink(missing_ok=True)
    return path


def pull(client, room_id, after):
    controller = LiveCollaborationController.__new__(LiveCollaborationController)
    controller._worker_results = queue.Queue()
    controller.volume_shape = SHAPE
    controller._edit_pull_lane(7, client, room_id, after)
    return controller._worker_results.get_nowait()


def test_reader_passes_over_a_damaged_operation_instead_of_stopping(tmp_path):
    alice = SharedFolderRoomClient(tmp_path, "alice")
    bob = SharedFolderRoomClient(tmp_path, "bob")
    room_id = alice.join("room", SIGNATURE)["id"]
    bob.join("room", SIGNATURE)
    empty = np.zeros(SHAPE, np.uint8)
    alice.push_operation(room_id, operation_payload("S1", empty, painted((0, 0, 0)), "a-1"))
    alice._artifact_queue.join()
    write_foreign_operation(
        alice._room_path,
        2,
        {**damaged_variants()["garbage payload"], "author": "mallory", "client_operation_id": "m"},
    )
    write_foreign_operation(
        alice._room_path,
        3,
        {
            **operation_payload("S1", empty, painted((3, 3, 3)), "a-3"),
            "author": "alice",
        },
    )

    result = pull(bob, room_id, 0)

    assert "error" not in result
    assert [item["sequence"] for item in result["operations"]] == [1, 2, 3]
    assert [bool(item.get("_undecodable")) for item in result["operations"]] == [
        False,
        True,
        False,
    ]
    assert "_packed" not in result["operations"][1]
    assert "_packed" in result["operations"][2]


def test_truncated_operation_file_is_skipped_only_once_it_is_clearly_permanent(tmp_path):
    alice = SharedFolderRoomClient(tmp_path, "alice")
    bob = SharedFolderRoomClient(tmp_path, "bob")
    room_id = alice.join("room", SIGNATURE)["id"]
    bob.join("room", SIGNATURE)
    empty = np.zeros(SHAPE, np.uint8)
    alice.push_operation(room_id, operation_payload("S1", empty, painted((0, 0, 0)), "a-1"))
    alice._artifact_queue.join()
    truncated = write_foreign_operation(
        alice._room_path, 2, None, age_seconds=0.0, raw='{"segment_id": "S1", "payl'
    )
    # A file that has only just appeared may still become readable: keep waiting.
    assert "error" in pull(bob, room_id, 1)
    old = time.time() - 120.0
    os.utime(truncated, (old, old))
    bob._last_operation_recovery_scan = 0.0  # the recovery listing is throttled to 1 Hz
    result = pull(bob, room_id, 1)
    assert "error" not in result
    assert [item["sequence"] for item in result["operations"]] == [2]
    assert "cannot be read" in result["operations"][0]["_undecodable"]


def test_controller_advances_past_a_damaged_operation_and_reports_it(monkeypatch):
    messages = []
    monkeypatch.setitem(
        sys.modules,
        "slicer",
        types.SimpleNamespace(
            util=types.SimpleNamespace(
                showStatusMessage=lambda text, *args: messages.append(text)
            )
        ),
    )
    controller = LiveCollaborationController.__new__(LiveCollaborationController)
    activity = []
    controller.connected = True
    controller._incoming_retry_at = 0.0
    controller._incoming_iterator = None
    controller._incoming_piece = None
    controller._incoming_continuation_scheduled = False
    controller.initial_sync_complete = True
    controller.initial_sequence = 0
    controller.last_sequence = 1
    controller.user_name = "bob"
    controller.awaiting_echo = [{"client_operation_id": "mine"}]
    controller.outgoing = []
    controller._applied_local_operation_ids = set()
    controller._append_activity = activity.append
    controller._sync_operation_journal = lambda: None
    controller.session_metrics = types.SimpleNamespace(
        increment=lambda name, amount=1: activity.append(f"metric:{name}"),
        operation_acknowledged=lambda operation_id: None,
        record=lambda *args: None,
    )
    controller._incoming_operations = deque(
        [
            {
                "sequence": 2,
                "author": "bob",
                "segment_id": "S1",
                "client_operation_id": "mine",
                "_undecodable": "payload cannot be decoded",
            }
        ]
    )

    controller._drain_incoming_operations()

    assert controller.last_sequence == 2
    assert not controller._incoming_operations
    # The participant's own damaged operation no longer blocks later edits.
    assert controller.awaiting_echo == []
    assert "metric:damaged_operations_skipped" in activity
    assert any("Skipped damaged edit #2" in entry for entry in activity)
    assert messages and "skipped damaged edit #2" in messages[0]


def test_lan_relay_method_list_is_unchanged_by_validation():
    assert "push_operation" in collaboration_module.LAN_RELAY_METHODS
