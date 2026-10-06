"""Chat must not wait for an SMB directory/cache generation to refresh."""

from pathlib import Path

from tests.controller_harness import collaboration as c


def test_chat_arrives_with_stale_hot_cache_and_stale_directory(tmp_path, monkeypatch):
    alice, bob = [c.SharedFolderRoomClient(tmp_path, name) for name in ("alice", "bob")]
    room = alice.join("chat", "c" * 64)["id"]
    bob.join("chat", "c" * 64)
    assert bob.chat_messages(room, 0) == []
    alice.send_chat(room, "First", "message-first")
    alice.send_chat(room, "Second", "message-second")
    original_feed, original_glob = c.SharedFolderRoomClient._recent_feed_state, Path.glob

    def stale_feed(path, *args):
        if path.name == "chat-state.json":
            return 0, []
        return original_feed(path, *args)

    def stale_directory(path, pattern):
        if path.name == "chat":
            return iter(())
        return original_glob(path, pattern)

    monkeypatch.setattr(c.SharedFolderRoomClient, "_recent_feed_state", staticmethod(stale_feed))
    monkeypatch.setattr(Path, "glob", stale_directory)
    assert [item["text"] for item in bob.chat_messages(room, 0)] == ["First", "Second"]
    assert [item["text"] for item in bob.chat_messages(room, 1, limit=1)] == ["Second"]
    assert bob.chat_messages(room, 2) == []


def test_chat_fast_feed_does_not_duplicate_retries_or_skip_legacy_messages(tmp_path):
    alice = c.SharedFolderRoomClient(tmp_path, "alice")
    room = alice.join("legacy", "c" * 64)["id"]
    first = alice.send_chat(room, "Legacy", "message-legacy")
    fast_path = alice._room_path / "chat-live" / f"{first['sequence']:020d}.json"
    fast_path.unlink(missing_ok=True)  # Room written by a previous plugin.
    alice.send_chat(room, "Modern", "message-modern")
    alice.send_chat(room, "Modern", "message-modern")
    assert [item["text"] for item in alice.chat_messages(room, 0)] == ["Legacy", "Modern"]
    assert [item["text"] for item in alice.chat_messages(room, 1)] == ["Modern"]
