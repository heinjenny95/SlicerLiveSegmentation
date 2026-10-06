"""Owner-only protection and universal freezes have distinct edit semantics."""

import pytest

from tests.controller_harness import Peer, install_fake_slicer
from tests.controller_harness import collaboration as c
from tests.test_protected_claims import operation


@pytest.mark.parametrize("mode", ["others", "all"])
def test_folder_lock_modes_protect_target_and_siblings(tmp_path, mode):
    alice, bob = [c.SharedFolderRoomClient(tmp_path, name) for name in ("alice", "bob")]
    room = alice.join("locks", "a" * 64)["id"]
    bob.join("locks", "a" * 64)
    alice.push_operation(room, operation("A", "seed", (5, 5, 5)))
    alice._artifact_queue.join()
    state = alice.set_segment_lock(room, "A", True, lock_mode=mode)
    assert state["lock_mode"] == mode
    assert alice.segment_locks(room)[0]["lock_mode"] == mode
    for peer in (alice, bob):
        protected = peer is bob or mode == "all"
        if protected:
            with pytest.raises(c.LiveCollaborationError, match="locked"):
                peer.push_operation(room, operation("A", peer.user_name + "edit"))
        else:
            peer.push_operation(room, operation("A", "owner-edit", (5, 5, 5)))
        peer.push_operation(room, operation("B", peer.user_name + "claim", (5, 5, 5)))
        assert ("A" in peer.operations(room, 0)[-1]["protected_segment_ids"]) == protected
    with pytest.raises(c.LiveCollaborationError, match="Only alice"):
        bob.set_segment_lock(room, "A", False)
    alice.set_segment_lock(room, "A", False)
    alice.push_operation(room, operation("A", "unfrozen"))


@pytest.mark.parametrize("mode", ["others", "all"])
def test_http_lock_modes(client, headers, mode):
    room = client.post("/api/live/rooms/join", headers=headers,
                       json={"room_name": "locks", "volume_signature": "a" * 64}).json()["id"]
    base = f"/api/live/rooms/{room}"
    assert client.post(base + "/operations", headers=headers, json=operation("A", "seed")).status_code == 201
    reply = client.put(base + "/locks/A", headers=headers, json={"locked": True, "lock_mode": mode})
    assert reply.status_code == 200
    assert reply.json()["lock_mode"] == mode
    assert client.get(base + "/locks", headers=headers).json()[0]["lock_mode"] == mode
    edit = client.post(base + "/operations", headers=headers, json=operation("A", "edit"))
    assert edit.status_code == (423 if mode == "all" else 201)
    client.post(base + "/operations", headers=headers, json=operation("B", "claim"))
    assert ("A" in client.get(base + "/operations", headers=headers).json()[-1]["protected_segment_ids"]) == (mode == "all")


def test_controller_universal_lock_blocks_owner_and_pending_freeze(monkeypatch):
    scene, _ = install_fake_slicer(monkeypatch)
    controller = Peer("alice", scene).controller
    controller.segment_locks_state["A"] = {"locked": True, "owner": "alice", "lock_mode": "others"}
    assert not controller._locked_by_other("A")
    controller.segment_locks_state["A"]["lock_mode"] = "all"
    assert controller._locked_by_other("A")
    controller.segment_locks_state.clear()
    controller.pending_lock_changes["A"] = {"locked": True, "lock_mode": "all"}
    assert controller._locked_by_other("A")


def test_http_client_refuses_silent_universal_downgrade():
    client = c.LiveRoomClient.__new__(c.LiveRoomClient)
    client._request = lambda *args: {"locked": True, "owner": "alice"}
    with pytest.raises(c.LiveCollaborationError, match="upgrade"):
        client.set_segment_lock("room", "A", True, lock_mode="all")


def test_universal_expiry_and_invalid_mode_leave_ownership_intact(tmp_path, monkeypatch):
    alice = c.SharedFolderRoomClient(tmp_path, "alice")
    room = alice.join("expiry", "a" * 64)["id"]
    alice.push_operation(room, operation("A", "seed"))
    alice._artifact_queue.join()
    state = alice.set_segment_lock(room, "A", True, 1, "all")
    with pytest.raises(c.LiveCollaborationError, match="mode"):
        alice.set_segment_lock(room, "A", True, lock_mode="typo")
    assert alice.segment_locks(room)[0]["lock_mode"] == "all"
    monkeypatch.setattr(c.time, "time", lambda: state["expires_epoch"] + 1)
    assert not alice.segment_locks(room)[0]["locked"]
    alice.push_operation(room, operation("A", "expired"))


def test_lan_relay_preserves_universal_lock_mode(tmp_path):
    relay = c.LanRelayServer(tmp_path, access_code="test-code", host="127.0.0.1", port=0)
    relay.start()
    client = c.LanRoomClient(f"http://127.0.0.1:{relay._httpd.server_address[1]}", "alice", "test-code")
    try:
        room = client.join("freeze", "a" * 64)["id"]
        client.push_operation(room, operation("A", "seed"))
        assert client.set_segment_lock(room, "A", True, 0, "all")["lock_mode"] == "all"
        with pytest.raises(c.LiveCollaborationError, match="locked"):
            client.push_operation(room, operation("A", "frozen"))
        client.set_segment_lock(room, "A", False)
        client.push_operation(room, operation("A", "unfrozen"))
    finally:
        relay.stop()


def test_database_migrates_existing_locks_without_changing_them(tmp_path):
    from app.db import Database

    database = Database(tmp_path / "legacy.db")
    database.initialize()
    with database.connect() as connection:
        connection.execute("ALTER TABLE live_segment_locks DROP COLUMN lock_mode")
        connection.execute("INSERT INTO live_rooms(id,name,volume_signature,created_by,created_at) VALUES ('r','room','v','alice','now')")
        connection.execute("INSERT INTO live_segment_locks(room_id,segment_id,owner,locked,updated_by,updated_at) VALUES ('r','A','alice',1,'alice','now')")
    database.initialize()
    database.initialize()
    with database.connect() as connection:
        state = dict(connection.execute("SELECT * FROM live_segment_locks").fetchone())
    assert (state["owner"], state["locked"], state["lock_mode"]) == ("alice", 1, "others")
