"""Catch-up must pipeline bounded pages without requesting a completed page twice."""

from unittest.mock import MagicMock

import numpy as np
import pytest

from tests import controller_harness as h


def peer(monkeypatch):
    scene, _ = h.install_fake_slicer(monkeypatch)
    controller = h.Peer("alice", scene).controller
    controller.initial_sync_complete = False
    controller.initial_sequence = 200
    controller._edit_pull_pending = False
    return controller


def operation(sequence):
    empty = np.zeros(h.SHAPE, np.uint8)
    mask = empty.copy()
    mask[1, 1, 1] = 1
    return {"segment_id": "label", "sequence": sequence,
            **h.collaboration.encode_mask_delta(empty, mask)}


class FinishedThread:
    """Finish between the timer's result-drain and scheduling phases."""

    def __init__(self, *, target, args, **_kwargs):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)

    def is_alive(self):
        return False


def test_completed_worker_result_is_consumed_before_next_request(monkeypatch):
    c = peer(monkeypatch)
    c.client.operations.return_value = [operation(1)]
    monkeypatch.setattr(h.collaboration.threading, "Thread", FinishedThread)
    c._start_edit_pull_worker(100.0, True)
    c._start_edit_pull_worker(101.0, True)
    assert c.client.operations.call_count == 1, "same cursor requested while its result waited in queue"
    assert c._edit_pull_pending
    c._drain_worker_results()
    assert not c._edit_pull_pending
    c._start_edit_pull_worker(102.0, True)
    assert c.client.operations.call_args.args[1] == 1


@pytest.mark.parametrize("message", [None, "SSH request 'operations' timed out"])
def test_pending_request_released_on_result_even_after_error(monkeypatch, message):
    c = peer(monkeypatch)
    c._edit_pull_pending = True
    result = {"lane": "edit-pull", "session_token": c._session_token}
    if message:
        result["error"] = message
    c._worker_results.put(result)
    c._drain_worker_results()
    assert not c._edit_pull_pending


def test_old_session_result_cannot_release_current_request(monkeypatch):
    c = peer(monkeypatch)
    c._edit_pull_pending = True
    c._worker_results.put({"lane": "edit-pull", "session_token": c._session_token - 1})
    c._drain_worker_results()
    assert c._edit_pull_pending


def test_catchup_page_uses_available_queue_slots(monkeypatch):
    c = peer(monkeypatch)
    c.client.operations.return_value = []
    cap = h.collaboration.MAX_PENDING_INITIAL_OPERATIONS
    c._incoming_operations.extend(operation(n) for n in range(1, cap - 2))
    monkeypatch.setattr(h.collaboration.threading, "Thread", FinishedThread)
    c._start_edit_pull_worker(100.0, True)
    assert c.client.operations.call_args.kwargs["limit"] == 3


def test_catchup_never_starts_when_queue_memory_budget_is_used(monkeypatch):
    c = peer(monkeypatch)
    monkeypatch.setattr(h.collaboration, "incoming_operation_bytes", lambda _op: 100)
    monkeypatch.setattr(h.collaboration, "MAX_INCOMING_PACKED_BYTES", 200)
    c._incoming_operations.extend([operation(1), operation(2)])
    c._start_edit_pull_worker(100.0, True)
    c.client.operations.assert_not_called()


@pytest.mark.parametrize("phase", ["construct", "start"])
def test_thread_start_failure_does_not_leave_catchup_permanently_pending(monkeypatch, phase):
    c = peer(monkeypatch)
    failed = MagicMock()
    failed.start.side_effect = RuntimeError("cannot start new thread")
    def factory(**_kwargs):
        if phase == "construct":
            raise RuntimeError("cannot start new thread")
        return failed
    monkeypatch.setattr(h.collaboration.threading, "Thread", factory)
    with pytest.raises(RuntimeError, match="cannot start"):
        c._start_edit_pull_worker(100.0, True)
    assert not c._edit_pull_pending
