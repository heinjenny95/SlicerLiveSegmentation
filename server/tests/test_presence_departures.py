"""Explicit departures must not look like a temporarily delayed heartbeat."""

import threading
from unittest.mock import MagicMock

import pytest

from tests.controller_harness import Peer, collaboration, install_fake_slicer


def presence_controller(monkeypatch):
    scene, _ = install_fake_slicer(monkeypatch)
    controller = Peer("JennyA", scene).controller
    for name in ("collaborator_combo", "users_label", "jump_to_user_button", "follow_checkbox"):
        setattr(controller, name, MagicMock())
    controller.collaborator_combo.currentText = ""
    controller.follow_checkbox.checked = False
    return controller


def test_normal_presence_poll_removes_explicitly_departed_peer(tmp_path, monkeypatch):
    alice = collaboration.SharedFolderRoomClient(tmp_path, "JennyA")
    bob = collaboration.SharedFolderRoomClient(tmp_path, "JennyB")
    room = alice.join("departure", "1" * 64)
    bob.join("departure", "1" * 64)
    bob.presence(room["id"], {})
    controller = presence_controller(monkeypatch)
    controller.client = alice
    controller.room_id = room["id"]
    controller._update_presence(alice.presence(room["id"], {}))
    assert "JennyB" in controller.presence_by_user

    bob.leave(room["id"])
    controller._presence_lane(controller._session_token, alice, room["id"], {})
    controller._drain_worker_results()

    assert "JennyB" not in controller.presence_by_user
    controller.users_label.setText.assert_called_with("Nobody else is connected")


def test_inflight_heartbeat_cannot_resurrect_departed_session(tmp_path, monkeypatch):
    alice = collaboration.SharedFolderRoomClient(tmp_path, "JennyA")
    bob = collaboration.SharedFolderRoomClient(tmp_path, "JennyB")
    room = alice.join("late heartbeat", "2" * 64)
    bob.join("late heartbeat", "2" * 64)
    bob.presence(room["id"], {})
    bob_path = bob._presence_path(bob._room_path)
    entered, resume = threading.Event(), threading.Event()
    original_write = collaboration._write_presence_json
    errors = []

    def delayed_write(path, data):
        if path == bob_path:
            entered.set()
            assert resume.wait(5), "test did not release the delayed heartbeat"
        return original_write(path, data)

    def heartbeat():
        try:
            bob.presence(room["id"], {})
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(collaboration, "_write_presence_json", delayed_write)
    worker = threading.Thread(target=heartbeat)
    worker.start()
    try:
        assert entered.wait(5), "heartbeat did not reach its write"
        bob.leave(room["id"])
    finally:
        resume.set()
        worker.join(5)
    assert not worker.is_alive()
    assert not errors
    assert {user["user"] for user in alice.presence(room["id"], {})} == {"JennyA"}


@pytest.mark.parametrize("missing", [False, True])
def test_departure_is_confirmed_even_while_heartbeat_is_unreadable(tmp_path, monkeypatch, missing):
    alice = collaboration.SharedFolderRoomClient(tmp_path, "JennyA")
    bob = collaboration.SharedFolderRoomClient(tmp_path, "JennyB")
    room = alice.join("unreadable heartbeat", "3" * 64)
    bob.join("unreadable heartbeat", "3" * 64)
    bob.presence(room["id"], {})
    controller = presence_controller(monkeypatch)
    controller._update_presence(alice.presence(room["id"], {}))
    bob_path = bob._presence_path(bob._room_path)
    bob.leave(room["id"])
    if missing:
        bob_path.unlink()
    else:
        bob_path.write_text("{partial", encoding="utf-8")

    report = alice.presence_report(room["id"], {})
    controller._update_presence(report["users"], report["departed_sessions"])
    assert "JennyB" not in controller.presence_by_user


def test_unconfirmed_missing_heartbeat_retains_network_grace(tmp_path, monkeypatch):
    alice = collaboration.SharedFolderRoomClient(tmp_path, "JennyA")
    bob = collaboration.SharedFolderRoomClient(tmp_path, "JennyB")
    room = alice.join("network gap", "4" * 64)
    bob.join("network gap", "4" * 64)
    bob.presence(room["id"], {})
    controller = presence_controller(monkeypatch)
    controller._update_presence(alice.presence(room["id"], {}))
    bob._presence_path(bob._room_path).write_text("{partial", encoding="utf-8")

    report = alice.presence_report(room["id"], {})
    controller._update_presence(report["users"], report["departed_sessions"])
    assert controller.presence_by_user["JennyB"]["_presence_delayed"]
    controller._presence_last_observed["JennyB"] -= collaboration.PRESENCE_DISPLAY_GRACE_SECONDS + 1
    controller._update_presence(report["users"], report["departed_sessions"])
    assert "JennyB" not in controller.presence_by_user


def test_rejoin_with_same_client_uses_fresh_session_and_ignores_old_departure(tmp_path, monkeypatch):
    alice = collaboration.SharedFolderRoomClient(tmp_path, "JennyA")
    bob = collaboration.SharedFolderRoomClient(tmp_path, "JennyB")
    room = alice.join("rejoin", "5" * 64)
    bob.join("rejoin", "5" * 64)
    bob.presence(room["id"], {})
    alice.presence(room["id"], {})
    old_session = bob.presence_session_id
    bob.leave(room["id"])
    old_departures = alice.presence_report(room["id"], {})["departed_sessions"]
    bob.join("rejoin", "5" * 64)
    assert bob.presence_session_id != old_session
    bob.presence(room["id"], {})
    report = alice.presence_report(room["id"], {})
    controller = presence_controller(monkeypatch)
    controller._update_presence(report["users"], old_departures)
    assert controller.presence_by_user["JennyB"]["presence_session_id"] == bob.presence_session_id
    # Even a cached old departure plus an ambiguous read must not close the
    # newer session already displayed by the controller.
    controller._update_presence([], old_departures)
    assert controller.presence_by_user["JennyB"]["_presence_delayed"]


def test_departure_is_published_before_slow_optional_audit(tmp_path, monkeypatch):
    alice = collaboration.SharedFolderRoomClient(tmp_path, "JennyA")
    bob = collaboration.SharedFolderRoomClient(tmp_path, "JennyB")
    room = alice.join("slow leave audit", "6" * 64)
    bob.join("slow leave audit", "6" * 64)
    bob.presence(room["id"], {})
    entered, resume = threading.Event(), threading.Event()

    def delayed_audit(*args):
        entered.set()
        assert resume.wait(5)

    monkeypatch.setattr(bob, "_append_audit", delayed_audit)
    worker = threading.Thread(target=bob.leave, args=(room["id"],))
    worker.start()
    try:
        assert entered.wait(5)
        assert {user["user"] for user in alice.presence(room["id"], {})} == {"JennyA"}
    finally:
        resume.set()
        worker.join(5)
    assert not worker.is_alive()


def test_lan_presence_report_transports_session_departure_and_rejoin(tmp_path):
    relay = collaboration.LanRelayServer(tmp_path, "test-code", host="127.0.0.1")
    relay.start()
    url = f"http://127.0.0.1:{relay._httpd.server_address[1]}"
    alice = collaboration.LanRoomClient(url, "JennyA", "test-code")
    bob = collaboration.LanRoomClient(url, "JennyB", "test-code")
    try:
        room = alice.join("lan departure", "7" * 64)
        bob.join("lan departure", "7" * 64)
        bob.presence(room["id"], {})
        assert "JennyB" in {user["user"] for user in alice.presence_report(room["id"], {})["users"]}
        old_session = bob.presence_session_id
        bob.leave(room["id"])
        report = alice.presence_report(room["id"], {})
        assert {user["user"] for user in report["users"]} == {"JennyA"}
        assert report["departed_sessions"][0]["presence_session_id"] == old_session
        bob.join("lan departure", "7" * 64)
        assert bob.presence_session_id != old_session
        bob.presence(room["id"], {})
        assert "JennyB" in {user["user"] for user in alice.presence_report(room["id"], {})["users"]}
    finally:
        relay.stop()


def test_presence_lane_still_supports_legacy_list_transport(monkeypatch):
    controller = presence_controller(monkeypatch)

    class LegacyClient:
        def presence(self, room_id, details):
            return [{"user": "JennyB", "presence_session_id": "legacy-session"}]

    controller._presence_lane(controller._session_token, LegacyClient(), "room", {})
    result = controller._worker_results.get_nowait()
    assert result["users"][0]["user"] == "JennyB"
    assert result["departed_sessions"] == []


@pytest.mark.parametrize("prefix", ["", "Direct LAN request failed: "])
def test_new_lan_client_can_still_poll_an_older_relay(monkeypatch, prefix):
    client = collaboration.LanRoomClient("http://127.0.0.1:8000", "JennyA", "code")
    calls = []

    def old_rpc(method, *args):
        calls.append(method)
        if method == "presence_report":
            raise collaboration.LiveCollaborationError(prefix + "Unsupported LAN collaboration request")
        return [{"user": "JennyB"}]

    monkeypatch.setattr(client, "_rpc", old_rpc)
    for _ in range(2):
        assert client.presence_report("room", {})["users"] == [{"user": "JennyB"}]
    assert calls == ["presence_report", "presence", "presence"]


def test_delayed_lan_leave_does_not_reset_newer_connection(tmp_path):
    relay = collaboration.LanRelayServer(tmp_path, "test-code", host="127.0.0.1")
    relay.start()
    url = f"http://127.0.0.1:{relay._httpd.server_address[1]}"
    old = collaboration.LanRoomClient(url, "JennyB", "test-code")
    new = collaboration.LanRoomClient(url, "JennyB", "test-code")
    try:
        room = old.join("delayed lan leave", "8" * 64)
        old.presence(room["id"], {})
        new.join("delayed lan leave", "8" * 64)
        new.presence(room["id"], {})
        old.leave(room["id"])
        users = new.presence_report(room["id"], {})["users"]
        assert users[0]["presence_session_id"] == new.presence_session_id
    finally:
        relay.stop()


def test_departure_marker_name_is_bounded_for_slicers_windows_runtime(tmp_path):
    marker = collaboration.SharedFolderRoomClient._presence_departure_path
    first = marker(tmp_path, "JennyB", "first-session")
    second = marker(tmp_path, "JennyB", "next-session")
    # Slicer's embedded Python may lack Windows long-path support. The marker
    # must not be longer than the room's usual heartbeat/operation filenames.
    assert len(first.name) <= 32
    assert first != second
    assert first.suffix == ".left"
