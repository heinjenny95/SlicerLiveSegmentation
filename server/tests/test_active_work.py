from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

CORE = Path(__file__).resolve().parents[2] / "LiveSegmentation" / "LiveSegmentationLib"
sys.path.insert(0, str(CORE))
from collaboration import SharedFolderRoomClient  # noqa: E402
from work_log import render_report  # noqa: E402


def sample(category="work", start=100, end=105, identifier="sample-0001"):
    return {"sample_id": identifier, "session_id": "study-session", "measurement_version": 1,
            "category": category, "start_epoch": start, "end_epoch": end,
            "duration_seconds": end - start, "idle_threshold_seconds": 60,
            "nninteractive_busy": False}


def test_local_work_samples_are_persistent_and_retry_idempotent(tmp_path):
    client = SharedFolderRoomClient(tmp_path, "Researcher")
    room = client.join("active-study", "v" * 64, presence_session_id="study-session")
    item = sample()
    assert client.record_work_intervals(room["id"], [item])["accepted_ids"] == [item["sample_id"]]
    client.record_work_intervals(room["id"], [item])
    client.leave(room["id"])
    assert client.work_log.flush(5)
    text = client.work_log.room_path.joinpath("work-log.txt").read_text(encoding="utf-8")
    assert "ACTIVE WORK" in text
    assert "Active work including visual review: 5.000" in text
    assert text.count('"sample_id": "sample-0001"') == 1


def test_manual_visual_review_is_work_but_pause_and_wait_are_not():
    items = [sample("work", 100, 105), sample("review", 105, 110, "sample-0002"),
             sample("manual_pause", 110, 115, "sample-0003"),
             sample("technical_wait", 115, 120, "sample-0004")]
    records = [{"schema": 1, "id": s["sample_id"], "event": "work.interval", "epoch": 1000,
                "user": "Researcher", "session_id": "study-session", "details": s} for s in items]
    text = render_report(records)
    assert "Active work including visual review: 10.000" in text
    assert "Technical waiting: 5.000" in text
    assert "Manual pause: 5.000" in text


def test_active_clock_exists_without_qt_or_nninteractive():
    from active_work import ActiveWorkClock

    assert callable(ActiveWorkClock)


def clock(**kwargs):
    from active_work import ActiveWorkClock

    saved = []
    return ActiveWorkClock("study-session", saved.append, monotonic=0, epoch=1000,
                           max_gap_seconds=float("inf"), **kwargs), saved


def test_idle_threshold_is_exact_and_meaningful_input_resumes():
    timer, saved = clock()
    timer.tick(70)
    assert timer.totals["work"] == 60 and timer.totals["idle"] == 10
    timer.note_input(70)
    timer.tick(75)
    timer.finish(75)
    assert timer.active_seconds == 65
    assert saved[-1]["category"] == "session_end"
    assert saved[-1]["expected_samples"] == len(saved) - 1


def test_silent_visual_review_counts_beyond_idle_and_manual_pause_wins():
    timer, saved = clock()
    timer.set_review(True, 0)
    timer.tick(120)
    assert timer.totals["review"] == 120 and timer.totals["idle"] == 0
    timer.set_paused(True, 120)
    timer.note_input(125)
    timer.tick(130)
    assert timer.totals["manual_pause"] == 10
    assert not timer.review
    timer.set_paused(False, 130)
    timer.tick(135)
    assert timer.totals["work"] == 5


def test_nninteractive_wait_does_not_subtract_simultaneous_review_or_editing():
    timer, _ = clock()
    timer.tick(0, busy=True)
    timer.tick(5)
    assert timer.totals["technical_wait"] == 5
    timer.note_input(5)
    timer.tick(8)
    assert timer.totals["work"] == 2
    assert timer.totals["technical_wait"] == 6
    timer.set_review(True, 8)
    timer.tick(18)
    assert timer.totals["review"] == 10
    assert timer.totals["technical_wait"] == 6


def test_optional_ir_probe_ignores_heartbeat_and_never_constructs_widget():
    from active_work import nninteractive_busy

    assert not nninteractive_busy(SimpleNamespace())
    widget = SimpleNamespace(_prediction_in_flight=False, _heartbeat_request=object())
    modules = SimpleNamespace(SlicerNNInteractiveIRWidget=widget)
    assert not nninteractive_busy(modules)
    widget._prediction_in_flight = True
    assert nninteractive_busy(modules)
    widget._cleaned_up = True
    assert not nninteractive_busy(modules)


def test_project_context_does_not_call_mutating_ir_target_getters():
    from active_work import module_matches_project

    widget = SimpleNamespace(_last_selected_segmentation_node_id="shared")
    def unsafe_getter():
        raise AssertionError("Must not call a target getter that can create/mutate nodes")
    widget.get_selected_segmentation_node_and_segment_id = unsafe_getter
    modules = SimpleNamespace(SlicerNNInteractiveIRWidget=widget)
    assert module_matches_project(modules, "SlicerNNInteractiveIR", "shared")
    assert not module_matches_project(modules, "SlicerNNInteractiveIR", "other")


def test_foreground_chat_other_module_and_manual_wait_are_not_work():
    timer, _ = clock()
    timer.tick(0, foreground=False)
    timer.tick(10, foreground=True, chat=True)
    timer.tick(20, chat=False, relevant=False)
    timer.tick(30, relevant=True)
    timer.set_manual_wait(True, 30)
    timer.tick(40)
    assert timer.active_seconds == 0
    assert timer.totals["away"] == timer.totals["communication"] == 10
    assert timer.totals["other_context"] == timer.totals["technical_wait"] == 10


def test_unobserved_gui_freeze_is_unknown_and_wall_clock_does_not_set_duration():
    from active_work import ActiveWorkClock

    saved = []
    timer = ActiveWorkClock("session", saved.append, monotonic=10, epoch=1000)
    timer.tick(30)
    assert timer.totals["unknown"] == 20 and timer.active_seconds == 0
    assert saved[0]["start_epoch"] == 1000 and saved[0]["end_epoch"] == 1020
    timer.note_input(30)
    timer.tick(35)
    assert timer.active_seconds == 5


def test_configured_idle_policy_and_missing_samples_remain_auditable():
    timer, saved = clock()
    timer.set_idle_seconds(15, 0)
    timer.tick(20)
    timer.finish(20)
    records = [{"schema": 1, "id": s["sample_id"], "event": "work.interval", "epoch": 1000,
                "user": "Researcher", "session_id": s["session_id"], "details": s} for s in saved]
    assert "Configured idle thresholds (seconds): 15" in render_report(records)
    assert "Missing local timing samples" not in render_report(records)
    assert "Missing local timing samples" in render_report(records[1:])


@pytest.mark.parametrize("field,value", [("category", []), ("duration_seconds", float("inf")),
                                        ("session_id", None), ("measurement_version", True),
                                        ("end_epoch", 2000), ("idle_threshold_seconds", 0)])
def test_bad_work_sample_is_rejected_before_persistence(tmp_path, field, value):
    client = SharedFolderRoomClient(tmp_path, "Researcher")
    room = client.join("invalid-work", "v" * 64)
    item = sample()
    item[field] = value
    with pytest.raises((ValueError, TypeError)):
        client.record_work_intervals(room["id"], [item])


def test_http_work_time_uses_authenticated_author_and_room_membership(client, headers):
    room = client.post("/api/live/rooms/join", headers=headers,
                       json={"room_name": "active-http", "volume_signature": "v" * 64}).json()
    url = f"/api/live/rooms/{room['id']}/work-intervals"
    assert client.post(url, headers={"X-LiveSeg-User": "outsider"}, json={"samples": [sample()]}).status_code == 403
    assert client.post(url, headers=headers, json={"samples": [sample()]}).status_code == 200
    assert client.post(url, headers=headers, json={"samples": [sample()]}).status_code == 200
    assert client.app.state.work_logs[room["id"]].flush(5)
    text = client.get(f"/api/live/rooms/{room['id']}/work-log", headers=headers).text
    assert 'ACTIVE PERSON "alice"' in text
    assert "Active work including visual review: 5.000" in text
    assert client.post(url, headers=headers, json={"samples": [dict(sample(), author="forged")]}).status_code == 422


def test_work_time_transfer_failure_never_disconnects_voxel_session(monkeypatch):
    import queue

    from tests.controller_harness import Peer, install_fake_slicer

    scene, _ = install_fake_slicer(monkeypatch)
    peer = Peer("Researcher", scene)
    controller = peer.controller
    refreshed = []
    controller.work_time = SimpleNamespace(error="", acknowledge=lambda ids: None,
                                           refresh=lambda: refreshed.append(True))
    class OfflineClient:
        def record_work_intervals(self, *args):
            raise OSError("Timing storage unavailable")
    local = SimpleNamespace(flush=lambda *args, **kwargs: True)
    controller._worker_results = queue.Queue()
    controller._work_time_lane(controller._session_token, OfflineClient(), "room", [sample()], local)
    controller._drain_worker_results()
    assert controller.connected
    assert "Timing storage unavailable" in controller.work_time.error
    assert not peer.errors and refreshed


def test_txt_reader_retries_a_transient_atomic_replace_race(tmp_path, monkeypatch):
    from work_log import read_work_report

    path = tmp_path / "work-log.txt"
    path.write_text("durable report", encoding="utf-8")
    original = Path.read_text
    attempts = []
    def read(target, *args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise PermissionError("brief Windows replacement window")
        return original(target, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    assert read_work_report(path) == "durable report"
    assert len(attempts) == 2


def test_older_unmeasured_work_is_unavailable_not_zero_seconds():
    text = render_report([])
    assert "Active work including visual review: UNAVAILABLE" in text
    assert "Active work including visual review: 0.000" not in text
