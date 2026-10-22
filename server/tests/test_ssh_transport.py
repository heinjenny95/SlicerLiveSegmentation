from __future__ import annotations

import json
import os
import queue
import shlex
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import paramiko
import pytest

CORE = Path(__file__).resolve().parents[2] / "LiveSegmentation" / "LiveSegmentationLib"
sys.path.insert(0, str(CORE))
from collaboration import LiveCollaborationError, apply_mask_delta, encode_mask_delta  # noqa: E402
from features import build_invitation, parse_invitation  # noqa: E402
from ssh_transport import (  # noqa: E402
    REMOTE_BOOTSTRAP,
    SshHostKeyRequired,
    SshProcessRoomClient,
    SshRoomClient,
    key_fingerprint,
    looks_like_ssh_folder,
    parse_ssh_folder,
    trust_host_key,
)


@pytest.mark.parametrize("value", ["linux-host/dev/shm/team", "linux-host:/dev/shm/team", "ssh://linux-host/dev/shm/team"])
def test_linux_folder_notation(value):
    parsed = parse_ssh_folder(value)
    assert (parsed.host, parsed.path, parsed.port) == ("linux-host", "/dev/shm/team", 22)
    assert parsed.volatile
    assert looks_like_ssh_folder(value)


@pytest.mark.parametrize("value", ["C:/data/team", r"\\host\share", "/tmp/team", "ssh://user:secret@host/path", "host/../escape", "host/%2e%2e/escape", "host/", "https://host/path", "host/path?secret=x", "host/path\ncmd", "-option/path"])
def test_rejects_ambiguous_or_unsafe_ssh_targets(value):
    with pytest.raises(ValueError):
        parse_ssh_folder(value)


def test_ssh_invitation_has_no_login_or_secret():
    value = build_invitation("ssh-folder", "room", "a" * 64, "ssh://my-user@linux-host:2222/work/team", access_code="must-not-copy")
    assert value["location"] == "ssh://linux-host:2222/work/team"
    assert "access_code" not in value
    assert "my-user" not in json.dumps(value)
    assert parse_invitation(value) == value


class LocalSSHServer:
    """A real encrypted loopback SSH server; only the shipped helper may run."""

    def __init__(self, folder, delay_health=False, delay_snapshot=0.0, delay_first_push=0.0):
        self.folder = folder
        self.delay_health = delay_health
        self.delay_snapshot = float(delay_snapshot)
        self.delay_first_push = float(delay_first_push)
        self.key = paramiko.RSAKey.generate(2048)
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(8)
        self.socket.settimeout(0.1)
        self.port = self.socket.getsockname()[1]
        self.transports = []
        self.processes = []
        self.authenticated = 0
        self.stopped = threading.Event()
        threading.Thread(target=self.accept, daemon=True).start()

    def accept(self):
        while not self.stopped.is_set():
            try:
                sock, _ = self.socket.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self.serve, args=(sock,), daemon=True).start()

    def serve(self, sock):
        owner = self

        class Auth(paramiko.ServerInterface):
            def check_auth_password(self, username, password):
                if username == "test-user" and password == "test-password":
                    owner.authenticated += 1
                    return paramiko.AUTH_SUCCESSFUL
                return paramiko.AUTH_FAILED

            def get_allowed_auths(self, username):
                return "password"

            def check_channel_request(self, kind, channel_id):
                return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

            def check_channel_exec_request(self, channel, command):
                parts = shlex.split(command.decode())
                if parts != ["python3", "-u", "-c", REMOTE_BOOTSTRAP]:
                    return False
                code = REMOTE_BOOTSTRAP
                if owner.delay_first_push:
                    code = code.replace("allowed = core.LAN_RELAY_METHODS",
                        "original_push = client.push_operation\n"
                        "    push_calls = []\n"
                        "    def delayed_push(*args, **kwargs):\n"
                        "        import time\n"
                        "        push_calls.append(1)\n"
                        "        if len(push_calls) == 1:\n"
                        f"            time.sleep({owner.delay_first_push!r})\n"
                        "        return original_push(*args, **kwargs)\n"
                        "    client.push_operation = delayed_push\n"
                        "    allowed = core.LAN_RELAY_METHODS")
                if owner.delay_health:
                    code = code.replace("allowed = core.LAN_RELAY_METHODS", "original_health = client.health_check\n    def delayed_health(*args, **kwargs):\n        import time\n        time.sleep(1.0)\n        return original_health(*args, **kwargs)\n    client.health_check = delayed_health\n    allowed = core.LAN_RELAY_METHODS")
                if owner.delay_snapshot:
                    code = code.replace(
                        "allowed = core.LAN_RELAY_METHODS",
                        "original_snapshot = client.publish_room_snapshot\n"
                        "    def delayed_snapshot(*args, **kwargs):\n"
                        "        import time\n"
                        f"        time.sleep({owner.delay_snapshot!r})\n"
                        "        return original_snapshot(*args, **kwargs)\n"
                        "    client.publish_room_snapshot = delayed_snapshot\n"
                        "    allowed = core.LAN_RELAY_METHODS",
                    )
                process = subprocess.Popen([sys.executable, "-u", "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=owner.folder, **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
                owner.processes.append(process)

                def receive():
                    try:
                        while data := channel.recv(65536):
                            process.stdin.write(data)
                            process.stdin.flush()
                    except (OSError, EOFError):
                        pass
                    finally:
                        try:
                            process.stdin.close()
                        except OSError:
                            pass

                def transmit():
                    try:
                        while data := process.stdout.read1(65536):
                            channel.sendall(data)
                    except (OSError, EOFError):
                        pass
                    finally:
                        channel.close()

                threading.Thread(target=receive, daemon=True).start()
                threading.Thread(target=transmit, daemon=True).start()
                return True

        transport = paramiko.Transport(sock)
        self.transports.append(transport)
        transport.add_server_key(self.key)
        try:
            transport.start_server(server=Auth())
            while transport.is_active() and not self.stopped.wait(0.05):
                pass
        except (OSError, EOFError, paramiko.SSHException):
            pass
        finally:
            transport.close()

    def client(self, user, known_hosts, password="test-password", isolated=False):
        folder = self.folder.as_posix()
        if os.name == "nt":
            folder = folder[2:]  # same-drive rooted path for the loopback helper
        client_type = SshProcessRoomClient if isolated else SshRoomClient
        return client_type(f"ssh://127.0.0.1:{self.port}{folder}", user, "test-user", password, known_hosts=known_hosts, timeout_seconds=4)

    def pin(self, path):
        trust_host_key(path, {"host": f"[127.0.0.1]:{self.port}", "type": self.key.get_name(), "key": self.key.get_base64(), "fingerprint": key_fingerprint(self.key)})

    def close(self):
        self.stopped.set()
        self.socket.close()
        for transport in self.transports:
            transport.close()
        for process in self.processes:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=3)


@pytest.fixture
def ssh_server(tmp_path):
    server = LocalSSHServer(tmp_path)
    yield server
    server.close()


def test_unknown_ssh_identity_is_rejected_before_login(ssh_server, tmp_path):
    client = ssh_server.client("alice", tmp_path / "known_hosts")
    try:
        with pytest.raises(SshHostKeyRequired) as found:
            client.join("room", "a" * 64)
        assert found.value.host_key["fingerprint"] == key_fingerprint(ssh_server.key)
        assert ssh_server.authenticated == 0
        assert not (tmp_path / "known_hosts").exists()
    finally:
        client.close()


def test_changed_ssh_identity_is_not_replaced(ssh_server, tmp_path):
    path = tmp_path / "known_hosts"
    other = paramiko.RSAKey.generate(2048)
    trust_host_key(path, {"host": f"[127.0.0.1]:{ssh_server.port}", "type": other.get_name(), "key": other.get_base64(), "fingerprint": key_fingerprint(other)})
    original = path.read_bytes()
    client = ssh_server.client("alice", path)
    try:
        with pytest.raises(paramiko.BadHostKeyException):
            client.join("room", "a" * 64)
        assert path.read_bytes() == original
        assert ssh_server.authenticated == 0
    finally:
        client.close()


def test_wrong_password_is_not_retained_or_written(ssh_server, tmp_path):
    known_hosts = tmp_path / "known_hosts"
    ssh_server.pin(known_hosts)
    client = ssh_server.client("alice", known_hosts, password="invalid-test-password")
    try:
        with pytest.raises(paramiko.AuthenticationException):
            client.join("rejected-room", "a" * 64)
        assert client.password == ""
        assert not (tmp_path / "LiveSegmentation").exists()
    finally:
        client.close()


def test_missing_remote_folder_is_not_created(ssh_server, tmp_path):
    from dataclasses import replace

    known_hosts = tmp_path / "known_hosts"
    ssh_server.pin(known_hosts)
    client = ssh_server.client("alice", known_hosts)
    client.target = replace(client.target, path=client.target.path + "/missing")
    try:
        with pytest.raises(LiveCollaborationError, match="must already exist"):
            client.join("missing-folder-room", "a" * 64)
        assert not (tmp_path / "missing").exists()
        assert client.password == ""
    finally:
        client.close()


@pytest.mark.parametrize("isolated", [False, True])
def test_two_encrypted_clients_share_voxels_chat_presence_and_history(ssh_server, tmp_path, isolated):
    known_hosts = tmp_path / "known_hosts"
    ssh_server.pin(known_hosts)
    alice = ssh_server.client("alice", known_hosts, isolated=isolated)
    bob = ssh_server.client("bob", known_hosts, isolated=isolated)
    stages = []
    alice.stage_callback = stages.append
    try:
        room = alice.join("encrypted-room", "a" * 64)
        assert bob.join("encrypted-room", "a" * 64)["id"] == room["id"]
        empty = np.zeros((12, 12, 12), dtype=np.uint8)
        painted = empty.copy()
        painted[1:3, 2:4, 3:5] = 1
        operation = {"client_operation_id": "ssh-edit-1", "segment_id": "LiveSeg-test", "segment_name": "Mandibles", "color_hex": "#FF0000", "base_sequence": 0, **encode_mask_delta(empty, painted)}
        pushed = alice.push_operation(room["id"], operation)
        received = bob.operations(room["id"], 0)
        assert np.array_equal(apply_mask_delta(empty, received[-1]), painted)
        assert alice.push_operation(room["id"], operation)["sequence"] == pushed["sequence"]
        alice.send_chat(room["id"], "hello over SSH", "chat-1")
        assert bob.chat_messages(room["id"], 0)[-1]["text"] == "hello over SSH"
        frozen = alice.set_segment_lock(room["id"], "LiveSeg-test", True, 0, "all")
        assert frozen["lock_mode"] == "all"
        with pytest.raises(LiveCollaborationError, match="locked"):
            alice.push_operation(room["id"], dict(operation, client_operation_id="frozen-owner-edit"))
        assert bob.segment_locks(room["id"])[0]["lock_mode"] == "all"
        # Reordering is not an edit to the anchor label, even if it is frozen.
        from collaboration import encode_metadata_update
        order = {"client_operation_id": "ssh-label-order", "segment_id": "LiveSeg-test",
                 "segment_name": "Mandibles", "segment_order": ["LiveSeg-test", "future-label"],
                 **encode_metadata_update(empty.shape)}
        alice.push_operation(room["id"], order)
        assert bob.operations(room["id"], pushed["sequence"])[-1]["segment_order"] == order["segment_order"]
        assert bob.room_history(room["id"])[-1]["changed_voxels"] == 0
        alice.set_segment_lock(room["id"], "LiveSeg-test", False)
        alice.presence(room["id"], {"active_segment": "LiveSeg-test"})
        assert any(item["user"] == "alice" for item in bob.presence(room["id"], {}))
        assert bob.room_history(room["id"])
        work_sample = {"sample_id": "ssh-timing-sample-1", "session_id": alice.presence_session_id,
                       "measurement_version": 1, "category": "review", "start_epoch": 100,
                       "end_epoch": 105, "duration_seconds": 5, "idle_threshold_seconds": 60,
                       "nninteractive_busy": True}
        assert alice.record_work_intervals(room["id"], [work_sample])["accepted_ids"] == [work_sample["sample_id"]]
        alice.record_work_intervals(room["id"], [work_sample])
        assert alice.password == bob.password == ""
        assert alice.preflight("encrypted-room", "a" * 64)["transport"] == "ssh-folder"
        assert "Loading local SSH libraries" in stages
        assert "Starting Python on the SSH server" in stages
        if isolated:
            assert all("test-password" not in arg for arg in alice._process.args)
        with pytest.raises(LiveCollaborationError, match="Unsupported"):
            alice._rpc("__getattribute__", "password")
        old_session = alice.presence_session_id
        alice.leave(room["id"])
        report = bob.presence_report(room["id"], {})
        assert {user["user"] for user in report["users"]} == {"bob"}
        assert report["departed_sessions"][0]["presence_session_id"] == old_session
        alice.join("encrypted-room", "a" * 64)
        assert alice.presence_session_id != old_session
        alice.presence(room["id"], {})
        assert "alice" in {user["user"] for user in bob.presence_report(room["id"], {})["users"]}
        alice.leave(room["id"])
        bob.leave(room["id"])
    finally:
        alice.close()
        bob.close()
    # Graceful helper shutdown flushes the same persistent study log used by
    # Shared Folder. This is encrypted loopback SSH, not a mocked RPC response.
    report_path = tmp_path / "LiveSegmentation" / "rooms" / room["id"] / "work-log.txt"
    from work_log import read_work_report

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        text = read_work_report(report_path) if report_path.is_file() else ""
        if "chat.message" in text and "Mandibles" in text and "room.leave" in text:
            break
        time.sleep(0.05)
    assert "chat.message" in text and "Mandibles" in text and "room.leave" in text
    assert text.count("segmentation.operation") == 2  # one voxel edit + one order-only change
    assert '"segment_order"' in text
    assert "Active work including visual review: 5.000" in text


@pytest.mark.parametrize("isolated", [False, True])
def test_slow_ssh_request_does_not_serialize_other_lanes(tmp_path, isolated):
    server = LocalSSHServer(tmp_path, delay_health=True)
    server.pin(tmp_path / "known_hosts")
    client = server.client("alice", tmp_path / "known_hosts", isolated=isolated)
    try:
        room = client.join("parallel-room", "b" * 64)
        with ThreadPoolExecutor(max_workers=2) as pool:
            slow = pool.submit(client.health_check, room["id"])
            time.sleep(0.1)
            started = time.monotonic()
            client.send_chat(room["id"], "not blocked", "parallel-chat")
            assert time.monotonic() - started < 0.8
            assert not slow.done()
            slow.result(timeout=3)
        started = time.monotonic()
        client.close()
        assert time.monotonic() - started < 0.5
        with pytest.raises(LiveCollaborationError, match="closed"):
            client.join("parallel-room", "b" * 64)
    finally:
        client.close()
        server.close()


def test_isolated_host_verification_works_without_importing_crypto(ssh_server, tmp_path, monkeypatch):
    client = ssh_server.client("alice", tmp_path / "known_hosts", isolated=True)
    try:
        with pytest.raises(LiveCollaborationError) as found:
            client.join("unknown-room", "a" * 64)
        details = found.value.host_key
        assert details["fingerprint"] == key_fingerprint(ssh_server.key)
        assert ssh_server.authenticated == 0
        with monkeypatch.context() as scoped:
            scoped.setitem(sys.modules, "paramiko", None)
            trust_host_key(tmp_path / "known_hosts", details)
        assert (tmp_path / "known_hosts").read_text().startswith(details["host"])
    finally:
        client.close()


def test_stalled_local_import_can_be_killed_without_waiting_for_python(tmp_path, monkeypatch):
    import ssh_transport

    real_popen = subprocess.Popen

    def stalled_child(args, **kwargs):
        # Reproduce a worker stuck before SSH startup, without touching a server.
        if "--local-worker" in args:
            return real_popen([sys.executable, "-u", "-c", "import time; time.sleep(60)"], **kwargs)
        return real_popen(args, **kwargs)

    monkeypatch.setattr(ssh_transport.subprocess, "Popen", stalled_child)
    client = SshProcessRoomClient("linux-host/folder", "alice", "test-user", "test-password", startup_timeout=0.25)
    started = time.monotonic()
    try:
        with pytest.raises(LiveCollaborationError, match="Starting isolated local SSH worker"):
            client.join("stalled", "a" * 64)
        assert time.monotonic() - started < 3
        assert client.password == ""
        client._process.wait(timeout=3)
        assert client._process.poll() is not None
    finally:
        client.close()


def test_explicit_password_does_not_contact_an_ssh_agent(ssh_server, tmp_path, monkeypatch):
    def forbidden_agent():
        raise AssertionError("Password authentication must not contact an agent")

    monkeypatch.setattr(paramiko.client, "Agent", forbidden_agent)
    ssh_server.pin(tmp_path / "known_hosts")
    client = ssh_server.client("alice", tmp_path / "known_hosts")
    try:
        assert client.join("password-room", "a" * 64)["id"]
    finally:
        client.close()


@pytest.mark.parametrize("isolated", [False, True])
def test_idle_ssh_session_survives_read_timeouts(ssh_server, tmp_path, isolated):
    ssh_server.pin(tmp_path / "known_hosts")
    client = ssh_server.client("alice", tmp_path / "known_hosts", isolated=isolated)
    client.timeout_seconds = 0.5
    try:
        room = client.join("idle-room", "a" * 64)
        time.sleep(1.1)  # More than two read deadlines, with no RPC pending.
        assert client.health_check(room["id"])
        assert ssh_server.authenticated == 1, "Idle must not reconnect with cached credentials"
    finally:
        client.close()


def test_partial_ssh_response_survives_idle_timeout():
    client = SshRoomClient("linux-host/folder", "alice", "test-user")
    pieces = iter([b'{"id":"test","result":"caf', TimeoutError(), b'\xc3', TimeoutError(), b'\xa9"}\n', b''])

    class Channel:
        def recv(self, size):
            value = next(pieces)
            if isinstance(value, Exception):
                raise value
            return value

    client._channel = Channel()
    client._pending["test"] = result = queue.Queue(maxsize=1)
    client._read_responses()
    assert result.get_nowait() == {"id": "test", "result": "caf\u00e9"}


@pytest.mark.parametrize("terminated", [False, True])
def test_oversized_ssh_frame_is_rejected(monkeypatch, terminated):
    import ssh_transport

    monkeypatch.setattr(ssh_transport, "MAX_FRAME", 32)
    client = SshRoomClient("linux-host/folder", "alice", "test-user")
    pieces = iter([b"x" * 33 + (b"\n" if terminated else b"")])
    client._receive_chunk = lambda: next(pieces)
    client._pending["test"] = result = queue.Queue(maxsize=1)
    client._read_responses()
    assert "frame limit" in result.get_nowait()["error"]
    assert client._closed.is_set()


def test_large_fragmented_response_is_scanned_only_once(monkeypatch):
    import ssh_transport

    scanned = []

    class CountedBuffer(bytearray):
        def find(self, sub, start=0):
            scanned.append(len(self) - start)
            return super().find(sub, start)

    monkeypatch.setattr(ssh_transport, "bytearray", CountedBuffer, raising=False)
    payload = {"id": "test", "result": "abc" * 20000}
    frame = (json.dumps(payload) + "\n").encode()
    pieces = iter([frame[index:index + 256] for index in range(0, len(frame), 256)] + [b""])
    client = SshRoomClient("linux-host/folder", "alice", "test-user")
    client._receive_chunk = lambda: next(pieces)
    client._pending["test"] = result = queue.Queue(maxsize=1)
    client._read_responses()
    assert result.get_nowait() == payload
    assert sum(scanned) <= len(frame), "Each arriving chunk rescanned the entire accumulated frame"


@pytest.mark.parametrize("isolated", [False, True])
def test_ssh_request_deadline_still_applies(tmp_path, isolated):
    server = LocalSSHServer(tmp_path, delay_health=True)
    server.pin(tmp_path / "known_hosts")
    client = server.client("alice", tmp_path / "known_hosts", isolated=isolated)
    client.timeout_seconds = 0.3
    try:
        room = client.join("deadline-room", "b" * 64)
        started = time.monotonic()
        with pytest.raises(LiveCollaborationError, match="did not respond within"):
            client.health_check(room["id"])
        assert time.monotonic() - started < 0.9
        # A late response must not destroy the connection or satisfy another RPC.
        time.sleep(1.1)
        assert client.send_chat(room["id"], "after timeout", "chat-after-timeout")
    finally:
        client.close()
        server.close()


@pytest.mark.parametrize("client_type,expected", [(SshRoomClient, 120.0), (SshProcessRoomClient, 125.0)])
def test_ssh_budgets_tolerate_slow_operations_but_keep_health_finite(monkeypatch, client_type, expected):
    import ssh_transport

    real_queue = queue.Queue
    deadlines = []

    class ObservedQueue(real_queue):
        def get(self, block=True, timeout=None):
            deadlines.append(timeout)
            return super().get(block=block, timeout=timeout)

    client = client_type("linux-host/folder", "alice", "test-user")
    client._ensure_connected = lambda: None

    class Channel:
        def sendall(self, data):
            request = json.loads(data)
            client._pending[request["id"]].put_nowait({"result": "ok"})

    client._channel = Channel()
    monkeypatch.setattr(ssh_transport.queue, "Queue", ObservedQueue)
    try:
        assert client._rpc("publish_room_snapshot", "room", []) == "ok"
        for method in ("health_check", "send_chat", "presence_report", "operations", "push_operation"):
            assert client._rpc(method, "room") == "ok"
        margin = 5.0 if client_type is SshProcessRoomClient else 0.0
        assert deadlines == [expected, 10.0+margin, *([30.0+margin]*4)]
    finally:
        client.close()


@pytest.mark.parametrize("isolated", [False, True])
def test_slow_checkpoint_survives_normal_deadline_without_blocking_chat_or_presence(tmp_path, isolated):
    server = LocalSSHServer(tmp_path, delay_snapshot=1.2)
    server.pin(tmp_path / "known_hosts")
    client = server.client("alice", tmp_path / "known_hosts", isolated=isolated)
    client.timeout_seconds = 0.5
    client.snapshot_timeout_seconds = 3.0
    try:
        room = client.join("slow-checkpoint", "b" * 64)
        mask = np.zeros((4, 4, 4), dtype=np.uint8)
        mask[1, 2, 3] = 1
        operation = {"segment_id": "stable-id", **encode_mask_delta(np.zeros_like(mask), mask)}
        with ThreadPoolExecutor(max_workers=2) as pool:
            slow = pool.submit(client.publish_room_snapshot, room["id"], [operation], expected_sequence=0)
            time.sleep(0.15)
            started = time.monotonic()
            assert client.send_chat(room["id"], "during checkpoint", "parallel-snapshot-chat")
            assert client.presence_report(room["id"], {})["users"]
            assert time.monotonic() - started < 0.8
            assert not slow.done(), "Checkpoint wait serialized other SSH lanes"
            assert slow.result(timeout=4)["last_sequence"] == 1
        received = client.operations(room["id"], 0)
        assert np.array_equal(apply_mask_delta(np.zeros_like(mask), received[-1]), mask)
        assert client.health_check(room["id"])
    finally:
        client.close()
        server.close()


@pytest.mark.parametrize("isolated", [False, True])
def test_checkpoint_deadline_is_finite_and_late_reply_keeps_its_identity(tmp_path, isolated):
    server = LocalSSHServer(tmp_path, delay_snapshot=1.2)
    server.pin(tmp_path / "known_hosts")
    client = server.client("alice", tmp_path / "known_hosts", isolated=isolated)
    client.timeout_seconds = 0.4
    client.snapshot_timeout_seconds = 0.6
    try:
        room = client.join("checkpoint-deadline", "b" * 64)
        started = time.monotonic()
        with pytest.raises(LiveCollaborationError, match="publish_room_snapshot.*within 0.6 seconds"):
            client.publish_room_snapshot(room["id"], [])
        assert time.monotonic() - started < 1.1
        # A timeout is not a cancellation of already accepted remote work.
        # Ignore its late reply; never feed it to a chat/health request.
        time.sleep(1.0)
        assert client.send_chat(room["id"], "after checkpoint timeout", "late-snapshot-chat")
        assert client.health_check(room["id"])
    finally:
        client.close()
        server.close()


@pytest.mark.parametrize("isolated", [False, True])
def test_closing_ssh_does_not_wait_for_long_checkpoint_budget(tmp_path, isolated):
    server = LocalSSHServer(tmp_path, delay_snapshot=1.2)
    server.pin(tmp_path / "known_hosts")
    client = server.client("alice", tmp_path / "known_hosts", isolated=isolated)
    try:
        room = client.join("checkpoint-close", "b" * 64)
        with ThreadPoolExecutor(max_workers=1) as pool:
            slow = pool.submit(client.publish_room_snapshot, room["id"], [])
            time.sleep(0.15)
            started = time.monotonic()
            client.close()
            with pytest.raises(LiveCollaborationError, match="closed"):
                slow.result(timeout=0.8)
            assert time.monotonic() - started < 0.8
    finally:
        client.close()
        server.close()


@pytest.mark.parametrize('isolated', [False, True])
def test_timed_out_push_retry_is_idempotent_and_other_lanes_continue(tmp_path, isolated):
    server = LocalSSHServer(tmp_path, delay_first_push=1.2)
    server.pin(tmp_path / 'known_hosts')
    client = server.client('alice', tmp_path / 'known_hosts', isolated=isolated)
    try:
        client.timeout_seconds = .3
        room = client.join('delayed-once', 'b'*64)
        before = np.zeros((4,)*3, np.uint8)
        after = before.copy()
        after[1, 2, 3] = 1
        op = {'segment_id': 'A', 'client_operation_id': 'same-identity', **encode_mask_delta(before, after)}
        with pytest.raises(LiveCollaborationError, match='push_operation.*within 0.3 seconds'):
            client.push_operation(room['id'], op)
        assert client.send_chat(room['id'], 'not blocked', 'independent-chat')
        assert client.push_operation(room['id'], op)
        time.sleep(1.3)  # Allow the first accepted request and its late reply to finish.
        operations = client.operations(room['id'], 0)
        assert len(operations) == 1
        np.testing.assert_array_equal(apply_mask_delta(before, operations[0]), after)
        assert client.health_check(room['id'])
    finally:
        client.close()
        server.close()
