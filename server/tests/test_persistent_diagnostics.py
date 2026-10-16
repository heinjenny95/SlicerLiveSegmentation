import json
import sys
from pathlib import Path

LIVE_CORE = Path(__file__).resolve().parents[2] / "LiveSegmentation" / "LiveSegmentationLib"
sys.path.insert(0, str(LIVE_CORE))

import types  # noqa: E402

from diagnostics import DiagnosticLog, ResultQueue, safe_text  # noqa: E402

from tests import controller_harness as harness  # noqa: E402


def test_log_is_persistent_rotated_and_does_not_store_secrets(tmp_path):
    log = DiagnosticLog(tmp_path, max_bytes=700, backups=2)
    for index in range(20):
        log.event("operation.received", sequence=index, password="DO-NOT-STORE",
                  message="password=HIDDEN api_key=HIDDEN Authorization: Bearer HIDDEN")
    log.close()
    files = sorted(tmp_path.glob("*.jsonl*"))
    assert 1 <= len(files) <= 3
    text = "".join(p.read_text(encoding="utf-8") for p in files)
    assert "HIDDEN" not in text and "DO-NOT-STORE" not in text
    records = [json.loads(line) for line in text.splitlines()]
    assert all("at_utc" in r and "monotonic_s" in r for r in records)
    assert any(r.get("sequence") == 19 for r in records)


def test_secret_bearing_error_urls_are_redacted():
    text = safe_text("https://user:pw@host/path?token=verysecret api_key='other secret with spaces'")
    assert "verysecret" not in text and "other secret" not in text and "spaces" not in text and ":pw@" not in text


def test_worker_result_keeps_completion_time_and_sanitized_traceback():
    results = ResultQueue()
    try:
        raise ValueError("password='private value' transport failed")
    except ValueError as exc:
        results.put({"error": str(exc), "lane": "edit-pull"})
    result = results.get_nowait()
    assert result["_worker_finished_monotonic"] > 0
    assert "ValueError" in result["_traceback"]
    assert "private value" not in result["_traceback"]


def test_writer_failure_does_not_block_or_silently_accumulate(tmp_path):
    obstruction = tmp_path / "not-a-directory"
    obstruction.write_text("fixture", encoding="utf-8")
    log = DiagnosticLog(obstruction / "logs")
    log._thread.join(timeout=2)
    assert log.error is not None
    for _ in range(100):
        log.event("must-not-accumulate")
    assert log._queue.qsize() <= 1
    log.close()


def test_error_popup_and_history_survive_successful_health_check(monkeypatch, caplog):
    scene, _ = harness.install_fake_slicer(monkeypatch)
    class Dialog:
        Critical, Ok = 3, 1
        def __init__(self, *unused): self.closed = False
        def setWindowTitle(self, *unused): pass
        def setIcon(self, *unused): pass
        def setStandardButtons(self, *unused): pass
        def setModal(self, *unused): pass
        def setText(self, *unused): pass
        def show(self): pass
        def close(self): self.closed = True
    sys.modules["qt"].QMessageBox = Dialog
    sys.modules["slicer"].util.mainWindow = lambda: None
    peer = harness.Peer("receiver", scene)
    c = peer.controller
    c._show_error = types.MethodType(harness.collaboration.LiveCollaborationController._show_error, c)
    c._worker_results.put({"lane": "edit-pull", "session_token": c._session_token,
                          "duration": .01, "error": "A persistent diagnostic error"})
    c._drain_worker_results()
    dialog = c._connection_error_dialog
    assert dialog is not None
    c._worker_results.put({"lane": "maintenance", "session_token": c._session_token,
                          "duration": .01, "health_checked": True, "health_ok": True})
    c._drain_worker_results()
    assert c.connection_healthy
    assert not dialog.closed
    assert c.error_history[-1]["message"] == "A persistent diagnostic error"
    assert c._last_error == "A persistent diagnostic error"
    assert "A persistent diagnostic error" in caplog.text
