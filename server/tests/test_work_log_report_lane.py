"""Derived TXT generation must never prevent new timing samples becoming durable."""

import threading

import pytest

from tests.test_work_log import RoomWorkLog, read_records


def test_new_journal_batch_is_durable_while_prior_txt_report_is_blocked(tmp_path, monkeypatch):
    import work_log

    entered, release = threading.Event(), threading.Event()
    original = work_log.render_report

    def blocked_report(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(work_log, "render_report", blocked_report)
    log = RoomWorkLog(tmp_path)
    log.submit("test", "Alice", "session", event_id="first-durable")
    assert log.flush(1, report=False)
    assert entered.wait(1)
    try:
        log.submit("test", "Alice", "session", event_id="new-during-report")
        assert log.flush(1, report=False), "A slow derived TXT blocked a new journal fsync"
        assert {r["id"] for r in read_records(tmp_path)[0]} == {"first-durable", "new-during-report"}
        assert not log.flush(.05, report=True), "Full report barrier acknowledged an unfinished report"
    finally:
        release.set()
        assert log.flush(5, report=True)
    assert "new-during-report" not in tmp_path.joinpath("work-log.txt").read_text(encoding="utf-8")
    # The report prints actions rather than internal journal IDs.
    assert tmp_path.joinpath("work-log.txt").read_text(encoding="utf-8").count(" | test | ") == 2


def test_failed_derived_report_does_not_reject_or_duplicate_new_durable_records(tmp_path, monkeypatch):
    import work_log

    log = RoomWorkLog(tmp_path)
    log.submit("room.join", "Alice", "session", event_id="join")
    assert log.flush(5)
    previous = tmp_path.joinpath("work-log.txt").read_bytes()
    replace = work_log.os.replace

    def fail_report(*args):
        raise PermissionError("TXT is open elsewhere")

    monkeypatch.setattr(work_log.os, "replace", fail_report)
    log.submit("test", "Alice", "session", event_id="new-while-txt-fails")
    assert log.flush(1, report=False), "TXT replacement failure rejected a durable journal"
    assert not log.flush(1, report=True)
    assert tmp_path.joinpath("work-log.txt").read_bytes() == previous
    assert [r["id"] for r in read_records(tmp_path)[0]].count("new-while-txt-fails") == 1
    monkeypatch.setattr(work_log.os, "replace", replace)
    assert log.flush(5)
    assert [r["id"] for r in read_records(tmp_path)[0]].count("new-while-txt-fails") == 1


def test_empty_first_flush_creates_local_scope_before_report_starts(tmp_path):
    log = RoomWorkLog(tmp_path / "new-local-scope", create_room=True)
    assert log.flush(5, report=True)
    assert log.room_path.joinpath("work-log.txt").is_file()


def test_failed_journal_fsync_is_never_acknowledged_as_durable(tmp_path, monkeypatch):
    import work_log

    log = RoomWorkLog(tmp_path)
    fsync = work_log.os.fsync

    def fail_fsync(*args):
        raise OSError("journal storage unavailable")

    monkeypatch.setattr(work_log.os, "fsync", fail_fsync)
    log.submit("test", "Alice", "session", event_id="must-retry")
    assert not log.flush(1, report=False)
    assert not log.flush(1, report=True)
    monkeypatch.setattr(work_log.os, "fsync", fsync)
    assert log.flush(5, report=True)
    assert [r["id"] for r in read_records(tmp_path)[0]] == ["must-retry"]


@pytest.mark.parametrize("failure_stage", ["construct", "start"])
def test_report_thread_start_failure_never_requeues_durable_journal(tmp_path, monkeypatch, failure_stage):
    import work_log

    real_thread = work_log.threading.Thread

    def unavailable(*args, **kwargs):
        if kwargs.get("name") != "LiveSegmentation-work-report":
            return real_thread(*args, **kwargs)
        if failure_stage == "construct":
            raise RuntimeError("report thread allocation unavailable")
        worker = real_thread(*args, **kwargs)

        def fail_start():
            raise RuntimeError("report thread start unavailable")

        worker.start = fail_start
        return worker

    monkeypatch.setattr(work_log.threading, "Thread", unavailable)
    log = RoomWorkLog(tmp_path)
    log.submit("test", "Alice", "session", event_id="durable-once")
    assert log.flush(1, report=False)
    assert not log.flush(1, report=True)
    assert not log._journal_error
    assert log._report_worker is None
    assert log._report_error
    assert not log._pending
    monkeypatch.setattr(work_log.threading, "Thread", real_thread)
    assert log.flush(5, report=True)
    assert not log.error
    physical_lines = [line for path in tmp_path.joinpath("work-log-data").glob("*.jsonl")
                      for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(physical_lines) == 1, "Report startup failure re-appended an already durable journal batch"
