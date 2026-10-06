"""A completed speed test must not immediately disappear behind old diagnostics."""

import json
from unittest.mock import MagicMock

from tests.controller_harness import Peer, install_fake_slicer


def test_benchmark_result_survives_old_diagnostic_report(monkeypatch):
    scene, _ = install_fake_slicer(monkeypatch)
    controller = Peer("alice", scene).controller
    for name in ("benchmark_button", "benchmark_result_label", "diagnostics_text"):
        setattr(controller, name, MagicMock())
    controller.last_diagnostics = {"previous": "diagnostic report"}
    report = {"rating": "good", "health": {"median_ms": 17.5}, "live_feed": {"median_ms": 25.0}, "error_count": 0}
    controller._handle_action_results([{"action": "benchmark", "value": report}])
    displayed = json.loads(controller.diagnostics_text.setPlainText.call_args.args[0])
    assert displayed == report
    text = controller.benchmark_result_label.setText.call_args.args[0]
    assert "17.5" in text and "25" in text and "not" in text.lower()
