"""Exercise the smoke script's final guards without importing or starting Slicer."""

from __future__ import annotations

import ast
import queue
from pathlib import Path
from types import SimpleNamespace

import pytest


def run_rejoin_guard(received, expected, *, deleted_label_present=False):
    source = Path(__file__).resolve().parents[2] / "scripts/slicer_live_segmentation_smoke_test.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    lifecycle = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and "LIVE_SEGMENTATION_SMOKE_TEST_LIFECYCLE" in ast.unparse(node.test)
    )
    start = next(
        index for index, node in enumerate(lifecycle.body)
        if isinstance(node, ast.If)
        and ast.unparse(node.test) == "rejoined_voxels != expected_rejoin_voxels"
    )
    end = next(
        index for index, node in enumerate(lifecycle.body[start:], start)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "replicas" for target in node.targets)
    )
    block = ast.Module(body=lifecycle.body[start:end], type_ignores=[])
    controller = SimpleNamespace(
        client=SimpleNamespace(operations=lambda *_: []), room_id="room",
        timer=SimpleNamespace(isActive=lambda: True), _worker_results=queue.Queue(),
        initial_sync_complete=False, last_sequence=0, initial_sequence=2,
        connected=True, _last_error="",
    )
    context = {
        "controller": controller, "rejoined_voxels": received,
        "expected_rejoin_voxels": expected, "deletion_sync": True,
        "deleted_probe_ids": ["deleted"],
        "rejoined": SimpleNamespace(GetSegmentation=lambda: SimpleNamespace(
            GetSegment=lambda _sid: object() if deleted_label_present else None)),
    }
    exec(compile(block, str(source), "exec"), context)


@pytest.mark.parametrize("received", [0, 27, 29])
def test_rejoin_smoke_fails_when_voxels_are_missing_or_added(received):
    with pytest.raises(RuntimeError, match="Rejoining did not restore"):
        run_rejoin_guard(received, 28)


def test_rejoin_smoke_accepts_complete_restore():
    run_rejoin_guard(28, 28)


def test_rejoin_smoke_checks_deleted_labels_independently():
    with pytest.raises(RuntimeError, match="deleted label reappeared"):
        run_rejoin_guard(28, 28, deleted_label_present=True)
