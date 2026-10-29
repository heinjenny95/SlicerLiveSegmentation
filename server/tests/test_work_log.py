from __future__ import annotations

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import numpy as np

CORE = Path(__file__).resolve().parents[2] / "LiveSegmentation" / "LiveSegmentationLib"
sys.path.insert(0, str(CORE))
from collaboration import SharedFolderRoomClient, encode_mask_delta  # noqa: E402
from work_log import RoomWorkLog, read_records, render_report  # noqa: E402


def test_durable_interval_ack_does_not_wait_for_slow_txt_render(tmp_path, monkeypatch):
    import work_log
    entered, release = threading.Event(), threading.Event()
    original = work_log.render_report
    def slow_report(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(work_log, 'render_report', slow_report)
    log = RoomWorkLog(tmp_path)
    log.submit('test', 'Alice', 'session', event_id='durable-event')
    try:
        # Force the private journal lane to start; report work may continue.
        result = log.flush(.2, report=False)
        assert entered.wait(1)
        assert result, 'Already-fsynced intervals were rejected while TXT was rendering'
        assert any(r['id']=='durable-event' for r in read_records(tmp_path)[0])
    finally:
        release.set()
        assert log.flush(5)


def test_new_interval_during_report_must_be_durable_before_ack(tmp_path, monkeypatch):
    import work_log
    entered, release = threading.Event(), threading.Event()
    original = work_log.render_report
    def slow_report(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(work_log, 'render_report', slow_report)
    log = RoomWorkLog(tmp_path)
    log.submit('test', 'Alice', 'session', event_id='first-event')
    log.flush(.1, report=False)
    assert entered.wait(1)
    try:
        log.submit('test', 'Alice', 'session', event_id='later-event')
        assert not log.flush(.05, report=False), 'An unpersisted interval was acknowledged'
    finally:
        release.set()
        assert log.flush(5)
    assert log.flush(.05, report=False)


def test_room_produces_persistent_txt_with_both_people_and_actions(tmp_path):
    alice = SharedFolderRoomClient(tmp_path, "Alice")
    bob = SharedFolderRoomClient(tmp_path, "Bob")
    room = alice.join("study", "same-volume")
    bob.join("study", "same-volume")
    room_path = alice._room_path
    alice.presence(room["id"], {})
    bob.presence(room["id"], {})
    empty = np.zeros((4, 4, 4), dtype=np.uint8)
    painted = empty.copy()
    painted[1, 1, 1] = 1
    operation = encode_mask_delta(empty, painted)
    operation.update(client_operation_id="study-stroke-0001", segment_id="label-one",
                     segment_name="Mandible", color_hex="#FF0000", base_sequence=0)
    alice.push_operation(room["id"], operation)
    alice.push_operation(room["id"], operation)  # transport retry, not a second action
    bob.send_chat(room["id"], "private text not needed for timing", "study-chat-0001")
    alice.leave(room["id"])
    bob.leave(room["id"])
    assert alice.work_log.flush(5)
    assert bob.work_log.flush(5)
    text = room_path.joinpath("work-log.txt").read_text(encoding="utf-8")
    assert "Alice" in text and "Bob" in text and "Mandible" in text
    assert "SESSION" in text and "closed" in text and "UTC" in text
    assert text.count("segmentation.operation") == 1
    assert "chat.message" in text
    assert "private text not needed" not in text
    assert "payload" not in text
    assert "Person-seconds" in text and "Team room-seconds" in text
    # Independent of voxel compaction and application restarts.
    alice2 = SharedFolderRoomClient(tmp_path, "Alice")
    alice2.join("study", "same-volume")
    alice2.leave(room["id"])
    assert alice2.work_log.flush(5)
    assert "Mandible" in room_path.joinpath("work-log.txt").read_text(encoding="utf-8")


def test_log_module_is_shipped_to_ssh_helper_and_slicer():
    import base64
    import zlib

    from ssh_transport import helper_bundle

    bundle = json.loads(zlib.decompress(base64.b64decode(helper_bundle())))
    assert "work_log" in bundle
    cmake = CORE.parent.joinpath("CMakeLists.txt").read_text()
    assert "LiveSegmentationLib/work_log.py" in cmake


def record(event, epoch, user="Alice", session="a", **details):
    return {"schema": 1, "id": f"{event}-{user}-{session}-{epoch}", "event": event,
            "epoch": epoch, "user": user, "session_id": session, "details": details}


def test_paper_metrics_separate_parallel_person_time_and_unknown_ends():
    records = [record("room.join", 100), record("room.join", 110, "Bob", "b"),
               record("room.join", 120, session="second-window"),
               record("room.leave", 130, session="second-window"),
               record("room.leave", 140), record("room.heartbeat", 150, "Bob", "b")]
    text = render_report(records, generated_at=10000)
    assert "Person-seconds (sum of per-person unions): 80.000" in text
    assert "parallel time counted once): 50.000" in text
    assert "open / end unconfirmed" in text
    assert "end is UNKNOWN" in text
    assert "10000.000 seconds" not in text


def test_bad_journal_tail_is_flagged_and_never_silently_removes_saved_events(tmp_path):
    folder = tmp_path / "work-log-data"
    folder.mkdir()
    path = folder / "interrupted.jsonl"
    item = record("room.join", 100)
    path.write_text(json.dumps(item) + "\n" + json.dumps(item) + '\n{"id":', encoding="utf-8")
    records, issues = read_records(tmp_path)
    assert records == [item]  # duplicate after an uncertain write is idempotent
    assert len(issues) == 1
    assert "Unreadable journal line" in render_report(records, issues)


def test_unobserved_offline_gap_is_not_counted_as_work():
    records = [record("room.join", 100), record("room.heartbeat", 115),
               record("room.heartbeat", 1000), record("room.leave", 1010)]
    text = render_report(records)
    assert "Person-seconds (sum of per-person unions): 25.000" in text
    assert "Unobserved gap in session a" in text


def test_initial_label_with_voxels_counts_as_edit_even_with_metadata_flag():
    records = [record("room.join", 100),
               record("segmentation.operation", 110, changed_voxels=1, metadata_update=True)]
    text = render_report(records)
    assert "First confirmed voxel edit:" in text


def test_txt_refreshes_on_timer_after_journal_flush_without_more_actions(tmp_path, monkeypatch):
    import time

    import work_log

    monkeypatch.setattr(work_log, "FLUSH_SECONDS", 0.01)
    monkeypatch.setattr(work_log, "REPORT_SECONDS", 0.08)
    log = RoomWorkLog(tmp_path)
    log.submit("room.join", "Alice", "timer-session")
    assert log.flush(5)
    log.submit("label.lock", "Alice", "timer-session", {"locked": True})
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if "label.lock" in tmp_path.joinpath("work-log.txt").read_text(encoding="utf-8"):
            break
        time.sleep(0.01)
    assert "label.lock" in tmp_path.joinpath("work-log.txt").read_text(encoding="utf-8")


def test_slow_log_io_never_blocks_submission_or_presence(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    log = RoomWorkLog(tmp_path)
    persist = log._persist

    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return persist(*args, **kwargs)

    monkeypatch.setattr(log, "_persist", slow)
    log.submit("room.join", "Alice", "session")
    assert entered.wait(5)
    # These return before the filesystem worker is released, not a fragile ms limit.
    with ThreadPoolExecutor() as pool:
        future = pool.submit(log.submit, "label.lock", "Alice", "session", {"locked": True})
        future.result(timeout=2)
        pool.submit(log.heartbeat, "Alice", "session").result(timeout=2)
        release.set()
    assert log.flush(5)


def test_failed_txt_replace_keeps_old_txt_and_durable_journal_then_recovers(tmp_path, monkeypatch):
    import work_log

    log = RoomWorkLog(tmp_path)
    log.submit("room.join", "Alice", "s")
    assert log.flush(5)
    before = tmp_path.joinpath("work-log.txt").read_bytes()
    replace = work_log.os.replace

    def fail(*args):
        raise PermissionError("report open in another application")

    monkeypatch.setattr(work_log.os, "replace", fail)
    log.submit("label.lock", "Alice", "s", {"locked": True, "password": "SECRET"})
    assert not log.flush(5)
    assert log.error
    assert tmp_path.joinpath("work-log.txt").read_bytes() == before
    assert any(item["event"] == "label.lock" for item in read_records(tmp_path)[0])
    monkeypatch.setattr(work_log.os, "replace", replace)
    assert log.flush(5)
    text = tmp_path.joinpath("work-log.txt").read_text(encoding="utf-8")
    assert text.count("label.lock") == 1 and "SECRET" not in text


def test_concurrent_writers_publish_all_events_without_a_voxel_lock(tmp_path):
    guard = threading.Lock()

    @contextmanager
    def report_lock():
        with guard:
            yield

    logs = [RoomWorkLog(tmp_path, report_lock) for _ in range(3)]
    for number, log in enumerate(logs):
        log.submit("room.join", f"Person {number}", str(number), epoch=100)
        for index in range(50):
            log.submit("label.review", f"Person {number}", str(number),
                       {"state": "approved"}, event_id=f"{number}-{index}", epoch=101 + index)
        log.submit("room.leave", f"Person {number}", str(number), epoch=160)
    with ThreadPoolExecutor() as pool:
        assert all(pool.map(lambda log: log.flush(10), logs))
    text = tmp_path.joinpath("work-log.txt").read_text(encoding="utf-8")
    assert text.count("label.review") == 150
    assert "Person-seconds (sum of per-person unions): 180.000" in text
    assert "parallel time counted once): 60.000" in text


def test_http_log_is_persistent_and_requires_room_membership(client, headers):
    room = client.post("/api/live/rooms/join", headers=headers,
                       json={"room_name": "study-http", "volume_signature": "v" * 64,
                             "presence_session_id": "alice-http"}).json()
    room_id = room["id"]
    client.post(f"/api/live/rooms/{room_id}/presence", headers=headers,
                json={"presence_session_id": "alice-http"})
    client.post(f"/api/live/rooms/{room_id}/messages", headers=headers,
                json={"text": "Secret chat body", "client_message_id": "study-http-chat"})
    client.delete(f"/api/live/rooms/{room_id}/presence?presence_session_id=alice-http", headers=headers)
    log = client.app.state.work_logs[room_id]
    assert log.flush(5)
    response = client.get(f"/api/live/rooms/{room_id}/work-log", headers=headers)
    assert response.status_code == 200
    assert "closed" in response.text and "chat.message" in response.text
    assert "Secret chat body" not in response.text
    assert log.room_path.joinpath("work-log.txt").is_file()
    assert client.get(f"/api/live/rooms/{room_id}/work-log", headers={"X-LiveSeg-User": "outsider"}).status_code == 403


def test_rolled_back_server_activity_is_not_published(tmp_path):
    from app.db import Database

    db = Database(tmp_path / "study.sqlite3")
    db.initialize()
    observed = []
    try:
        with db.transaction() as connection:
            db.after_commit(connection, lambda: observed.append("must not happen"))
            raise ValueError("failed action")
    except ValueError:
        pass
    assert not observed
    with db.transaction() as connection:
        db.after_commit(connection, lambda: observed.append("confirmed"))
    assert observed == ["confirmed"]


def test_server_logger_loads_in_the_docker_directory_layout(tmp_path):
    import importlib.util
    import shutil

    app_folder = tmp_path / "app"
    app_folder.mkdir()
    source = Path(__file__).resolve().parents[1] / "app" / "study_log.py"
    shutil.copyfile(source, app_folder / "study_log.py")
    library = tmp_path / "LiveSegmentation" / "LiveSegmentationLib"
    library.mkdir(parents=True)
    shutil.copyfile(CORE / "work_log.py", library / "work_log.py")
    spec = importlib.util.spec_from_file_location("docker_study_log_probe", app_folder / "study_log.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.RoomWorkLog)
