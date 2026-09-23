"""Network backup work must not execute on Slicer's GUI thread (L-15)."""

import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from tests.controller_harness import Peer, collaboration, install_fake_slicer


def setup_backup(monkeypatch, tmp_path):
    scene, _ = install_fake_slicer(monkeypatch)
    peer = Peer("alice", scene)
    controller = peer.controller
    controller.backup_retention_spin = SimpleNamespace(value=10)
    controller.backup_status_label = MagicMock()
    controller._queue_action = MagicMock()
    client = collaboration.SharedFolderRoomClient(tmp_path / "share", "alice")
    controller.room_id = client.join("room", "s" * 64)["id"]
    controller.client = client
    reservation = client.reserve_project_backup(controller.room_id, 60)
    gui_thread = threading.get_ident()

    def save_scene(path):
        assert threading.get_ident() == gui_thread
        Path(path).write_bytes(b"synthetic-scene-bundle")
        return True

    import slicer
    monkeypatch.setattr(slicer.util, "saveScene", save_scene, raising=False)
    return controller, reservation, gui_thread


def test_network_copy_and_hash_never_run_on_gui_thread(monkeypatch, tmp_path):
    controller, reservation, gui_thread = setup_backup(monkeypatch, tmp_path)
    copied = threading.Event()
    release = threading.Event()
    original = collaboration.shutil.copyfile
    original_hash = collaboration.sha256_file
    worker_threads = []

    def copy(source, destination):
        worker_threads.append(threading.get_ident())
        assert threading.get_ident() != gui_thread, "network copy blocks the GUI"
        copied.set()
        assert release.wait(5)
        return original(source, destination)

    def hash_file(path):
        assert threading.get_ident() != gui_thread, "hashing blocks the GUI"
        return original_hash(path)

    monkeypatch.setattr(collaboration.shutil, "copyfile", copy)
    monkeypatch.setattr(collaboration, "sha256_file", hash_file)
    try:
        assert controller._create_project_backup(reservation)
        assert copied.wait(5)
        assert not Path(reservation["path"]).exists()
        # Return to the GUI before releasing the deliberately stalled network.
        assert controller._backup_worker.is_alive()
        assert "completed" not in str(controller.backup_status_label.mock_calls).lower()
    finally:
        release.set()
        worker = getattr(controller, "_backup_worker", None)
        if worker:
            worker.join(5)
    assert len(set(worker_threads)) == 1
    controller._drain_worker_results()
    assert Path(reservation["path"]).read_bytes() == b"synthetic-scene-bundle"
    monkeypatch.setattr(collaboration, "sha256_file", original_hash)
    assert controller.client.verify_project_backup(
        controller.room_id, Path(reservation["path"]).name)["valid"]


def test_corrupted_transfer_is_not_published_as_a_valid_backup(monkeypatch, tmp_path):
    controller, reservation, _ = setup_backup(monkeypatch, tmp_path)
    monkeypatch.setattr(collaboration.shutil, "copyfile",
                        lambda source, target: Path(target).write_bytes(b"corrupted"))
    controller._create_project_backup(reservation)
    worker = getattr(controller, "_backup_worker", None)
    if worker:
        worker.join(5)
    controller._drain_worker_results()
    assert not Path(reservation["path"]).exists()
    assert "checksum" in str(controller.backup_status_label.mock_calls).lower()


def test_slow_backup_completion_does_not_touch_a_new_room(monkeypatch, tmp_path):
    controller, reservation, _ = setup_backup(monkeypatch, tmp_path)
    started, release = threading.Event(), threading.Event()
    original = collaboration.shutil.copyfile
    old_client, old_room = controller.client, controller.room_id
    prune = MagicMock()
    monkeypatch.setattr(old_client, "prune_project_backups", prune)

    def copy(source, target):
        started.set()
        assert release.wait(5)
        return original(source, target)

    monkeypatch.setattr(collaboration.shutil, "copyfile", copy)
    try:
        controller._create_project_backup(reservation)
        assert started.wait(5)
        controller._session_token += 1  # Leave/rejoin while the worker is blocked.
        controller.room_id = "another-room"
        controller.client = MagicMock()
        controller.backup_status_label.reset_mock()
        assert controller._create_project_backup(reservation) is False
    finally:
        release.set()
        controller._backup_worker.join(5)
    controller._drain_worker_results()
    prune.assert_called_once_with(old_room, 10)
    controller.backup_status_label.setText.assert_not_called()
    controller.client.prune_project_backups.assert_not_called()
    assert Path(reservation["path"]).is_file()


def test_local_save_failure_cleans_reservation_off_thread(monkeypatch, tmp_path):
    controller, reservation, gui_thread = setup_backup(monkeypatch, tmp_path)
    import slicer
    monkeypatch.setattr(slicer.util, "saveScene", lambda path: False)
    original = collaboration._write_json_atomic

    def write(path, *args, **kwargs):
        assert threading.get_ident() != gui_thread
        return original(path, *args, **kwargs)

    monkeypatch.setattr(collaboration, "_write_json_atomic", write)
    assert controller._create_project_backup(reservation) is False
    controller._backup_worker.join(5)
    controller._drain_worker_results()
    state = collaboration._read_json_file(Path(reservation["state_path"]))
    assert state["last_backup_epoch"] == 0
    assert not Path(reservation["path"]).exists()


def test_gui_event_during_scene_save_does_not_start_another_backup(monkeypatch, tmp_path):
    controller, reservation, _ = setup_backup(monkeypatch, tmp_path)
    import slicer
    save = slicer.util.saveScene

    def reentrant_save(path):
        assert controller._create_project_backup(reservation) is False
        return save(path)

    monkeypatch.setattr(slicer.util, "saveScene", reentrant_save)
    assert controller._create_project_backup(reservation)
    controller._backup_worker.join(5)
    assert not controller._backup_in_progress()
    assert Path(reservation["path"]).exists()


def test_room_change_during_local_save_aborts_upload(monkeypatch, tmp_path):
    controller, reservation, _ = setup_backup(monkeypatch, tmp_path)
    import slicer
    save = slicer.util.saveScene

    def switch_room(path):
        controller._session_token += 1
        controller.client = MagicMock()
        controller.room_id = "another-room"
        controller.backup_status_label.reset_mock()
        return save(path)

    monkeypatch.setattr(slicer.util, "saveScene", switch_room)
    assert controller._create_project_backup(reservation) is False
    controller._backup_worker.join(5)
    controller._drain_worker_results()
    assert not Path(reservation["path"]).exists()
    controller.backup_status_label.setText.assert_not_called()
    assert "Room changed" in collaboration._read_json_file(
        Path(reservation["state_path"]))["error"]
