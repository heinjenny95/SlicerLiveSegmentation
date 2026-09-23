"""Prepare and verify MRB bytes before touching a user's live Slicer scene."""

import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.controller_harness import collaboration as c
from tests.test_background_backup import setup_backup


def setup_restore(monkeypatch, tmp_path, *, corrupt=False, legacy=False):
    controller, reservation, gui = setup_backup(monkeypatch, tmp_path)
    source = Path(reservation["path"])
    source.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source, "w") as bundle:
        bundle.writestr("Scene/Scene.mrml", "<MRML/>")
    checksum = c.sha256_file(source)
    c._write_json_atomic(source.with_suffix(".mrb.json"), {
        "sha256": None if legacy else checksum, "size_bytes": source.stat().st_size,
    })
    record = controller.client.list_project_backups(controller.room_id)[0]
    if corrupt:
        source.write_bytes(b"damaged transfer")
    controller._selected_backup = lambda: record
    controller.leave = MagicMock()
    import qt
    import slicer
    monkeypatch.setattr(qt, "QMessageBox", SimpleNamespace(
        Yes=1, No=2, question=MagicMock(return_value=1)), raising=False)
    monkeypatch.setattr(slicer.util, "mainWindow", lambda: None, raising=False)
    monkeypatch.setattr(slicer.util, "loadScene", MagicMock(return_value=True), raising=False)
    monkeypatch.setattr(slicer.util, "getNodesByClass", lambda _: [], raising=False)
    monkeypatch.setattr(slicer.util, "errorDisplay", MagicMock(), raising=False)
    return controller, source, gui, slicer.util.loadScene, qt.QMessageBox.question


def finish(controller):
    worker = getattr(controller, "_backup_restore_worker", None)
    if worker:
        worker.join(5)
        assert not worker.is_alive()
    controller._drain_worker_results()


def test_restore_never_reads_the_network_or_loads_scene_on_click(monkeypatch, tmp_path):
    controller, source, gui, load, question = setup_restore(monkeypatch, tmp_path)
    original = c.shutil.copyfile
    started, release = threading.Event(), threading.Event()

    def stalled_copy(src, target):
        assert threading.get_ident() != gui
        started.set()
        assert release.wait(5)
        return original(src, target)

    monkeypatch.setattr(c.shutil, "copyfile", stalled_copy)
    try:
        controller.restore_selected_backup()
        load.assert_not_called()
        controller.leave.assert_not_called()
        question.assert_not_called()
        assert started.wait(5)
    finally:
        release.set()
        finish(controller)
    controller.leave.assert_called_once()
    local_path = Path(load.call_args.args[0])
    assert local_path != source
    assert load.call_args.kwargs == {"properties": {"clear": False}}
    assert not local_path.exists()
    assert source.is_file()


def test_bad_checksum_never_leaves_room_or_loads_scene(monkeypatch, tmp_path):
    controller, _, _, load, _ = setup_restore(monkeypatch, tmp_path, corrupt=True)
    controller.restore_selected_backup()
    finish(controller)
    controller.leave.assert_not_called()
    load.assert_not_called()
    assert "checksum" in str(controller._last_error or controller.backup_status_label.mock_calls).lower()


def test_legacy_backup_requires_explicit_manual_import(monkeypatch, tmp_path):
    controller, _, _, load, _ = setup_restore(monkeypatch, tmp_path, legacy=True)
    controller.restore_selected_backup()
    finish(controller)
    controller.leave.assert_not_called()
    load.assert_not_called()
    assert "checksum" in str(controller.backup_status_label.mock_calls).lower()


def test_finished_download_cannot_replace_a_different_room(monkeypatch, tmp_path):
    controller, _, _, load, question = setup_restore(monkeypatch, tmp_path)
    controller.restore_selected_backup()
    worker = getattr(controller, "_backup_restore_worker", None)
    if worker:
        worker.join(5)
    controller._session_token += 1
    finish(controller)
    controller.leave.assert_not_called()
    load.assert_not_called()
    question.assert_not_called()


@pytest.mark.parametrize("during_question", [False, True])
def test_new_unsent_work_prevents_restore(monkeypatch, tmp_path, during_question):
    controller, _, _, load, question = setup_restore(monkeypatch, tmp_path)
    controller.restore_selected_backup()

    def paint():
        controller.dirty_segments.add("fresh-work")
        return 1

    if during_question:
        question.side_effect = lambda *a: paint()
    else:
        paint()
    finish(controller)
    controller.leave.assert_not_called()
    load.assert_not_called()


def test_declining_restore_keeps_the_room_and_removes_local_copy(monkeypatch, tmp_path):
    controller, _, _, load, question = setup_restore(monkeypatch, tmp_path)
    question.return_value = 2
    controller.restore_selected_backup()
    finish(controller)
    controller.leave.assert_not_called()
    load.assert_not_called()
    assert controller._backup_restore_job is None


def test_cancel_discards_ready_staging(monkeypatch, tmp_path):
    controller, _, _, load, _ = setup_restore(monkeypatch, tmp_path)
    controller.restore_selected_backup()
    controller._backup_restore_worker.join(5)
    staging_path = Path(controller._backup_restore_job["staging"].name)
    controller._cancel_backup_restore()
    finish(controller)
    assert not staging_path.exists()
    load.assert_not_called()


@pytest.mark.parametrize("content", [b"not a zip", b"unsafe", b"no scene"])
def test_even_correctly_hashed_invalid_archives_are_not_loaded(monkeypatch, tmp_path, content):
    controller, source, _, load, _ = setup_restore(monkeypatch, tmp_path)
    if content == b"not a zip":
        source.write_bytes(content)
    else:
        with zipfile.ZipFile(source, "w") as bundle:
            bundle.writestr("../scene.mrml" if content == b"unsafe" else "readme.txt", "test")
    checksum = c.sha256_file(source)
    c._write_json_atomic(source.with_suffix(".mrb.json"), {"sha256": checksum})
    controller._selected_backup()["sha256"] = checksum
    controller.restore_selected_backup()
    finish(controller)
    load.assert_not_called()
    controller.leave.assert_not_called()


def test_cancel_during_blocked_copy_is_nonblocking_and_cleans_staging(monkeypatch, tmp_path):
    controller, source, _, load, _ = setup_restore(monkeypatch, tmp_path)
    original = c.shutil.copyfile
    started, release = threading.Event(), threading.Event()
    destinations = []

    def stalled_copy(src, target):
        destinations.append(Path(target))
        started.set()
        assert release.wait(5)
        return original(src, target)

    monkeypatch.setattr(c.shutil, "copyfile", stalled_copy)
    try:
        controller.restore_selected_backup()
        assert started.wait(5)
        controller._cancel_backup_restore()
        assert controller._backup_restore_worker.is_alive()
        controller.restore_selected_backup()  # No pile-up of blocked SMB workers.
        assert len(destinations) == 1
    finally:
        release.set()
        finish(controller)
    load.assert_not_called()
    assert not destinations[0].parent.exists()
    assert source.exists()


def test_load_failure_never_requests_a_scene_clear(monkeypatch, tmp_path):
    controller, _, _, load, _ = setup_restore(monkeypatch, tmp_path)
    load.return_value = False
    controller.restore_selected_backup()
    finish(controller)
    assert load.call_args.kwargs == {"properties": {"clear": False}}
    assert "partially loaded" in str(controller.backup_status_label.mock_calls)


def test_missing_sidecar_and_spoofed_backup_path_are_not_trusted(monkeypatch, tmp_path):
    controller, source, _, load, _ = setup_restore(monkeypatch, tmp_path)
    source.with_suffix(".mrb.json").unlink()
    controller._selected_backup()["path"] = str(tmp_path / "unrelated.mrb")
    controller.restore_selected_backup()
    finish(controller)
    load.assert_not_called()
    assert "checksum" in str(controller.backup_status_label.mock_calls).lower()


@pytest.mark.parametrize("loaded", [True, False])
def test_imported_replicas_are_detached_from_live_cleanup(monkeypatch, tmp_path, loaded):
    controller, _, _, load, _ = setup_restore(monkeypatch, tmp_path)
    existing, imported = MagicMock(), MagicMock()
    existing.GetID.return_value = "existing"
    imported.GetID.return_value = "new"
    import slicer
    monkeypatch.setattr(slicer.util, "getNodesByClass", MagicMock(side_effect=[[existing], [existing, imported]]))
    load.return_value = loaded
    controller.restore_selected_backup()
    finish(controller)
    existing.SetAttribute.assert_not_called()
    imported.SetAttribute.assert_any_call("LiveSegmentation.SharedReplica", None)
    imported.SetAttribute.assert_any_call("LiveSegmentation.RoomId", None)
    imported.SetAttribute.assert_any_call("LiveSegmentation.BackupImport", "1")
