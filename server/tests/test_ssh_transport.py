from __future__ import annotations

import json
import os
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

    def __init__(self, folder, delay_health=False):
        self.folder = folder
        self.delay_health = delay_health
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
                if owner.delay_health:
                    code = code.replace("allowed = core.LAN_RELAY_METHODS", "original_health = client.health_check\n    def delayed_health(*args, **kwargs):\n        import time\n        time.sleep(1.0)\n        return original_health(*args, **kwargs)\n    client.health_check = delayed_health\n    allowed = core.LAN_RELAY_METHODS")
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

    def client(self, user, known_hosts, password="test-password"):
        folder = self.folder.as_posix()
        if os.name == "nt":
            folder = folder[2:]  # same-drive rooted path for the loopback helper
        return SshRoomClient(f"ssh://127.0.0.1:{self.port}{folder}", user, "test-user", password, known_hosts=known_hosts, timeout_seconds=4)

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


def test_two_encrypted_clients_share_voxels_chat_presence_and_history(ssh_server, tmp_path):
    known_hosts = tmp_path / "known_hosts"
    ssh_server.pin(known_hosts)
    alice = ssh_server.client("alice", known_hosts)
    bob = ssh_server.client("bob", known_hosts)
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
        alice.presence(room["id"], {"active_segment": "LiveSeg-test"})
        assert any(item["user"] == "alice" for item in bob.presence(room["id"], {}))
        assert bob.room_history(room["id"])
        assert alice.password == bob.password == ""
        assert alice.preflight("encrypted-room", "a" * 64)["transport"] == "ssh-folder"
        with pytest.raises(LiveCollaborationError, match="Unsupported"):
            alice._rpc("__getattribute__", "password")
    finally:
        alice.close()
        bob.close()


def test_slow_ssh_request_does_not_serialize_other_lanes(tmp_path):
    server = LocalSSHServer(tmp_path, delay_health=True)
    server.pin(tmp_path / "known_hosts")
    client = server.client("alice", tmp_path / "known_hosts")
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
