"""Encrypted folder transport: persistent SSH, no SMB mount or public relay port.

The remote helper is sent to Python in memory. Only normal room data is written
inside the selected folder. Credentials are never included in RPCs or invitations.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import re
import shlex
import threading
import time
import urllib.parse
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

try:
    from .collaboration import LAN_RELAY_METHODS, LanRoomClient, LiveCollaborationError
    from .version import COLLABORATION_PROTOCOL_VERSION, PLUGIN_VERSION
except ImportError:
    from collaboration import LAN_RELAY_METHODS, LanRoomClient, LiveCollaborationError
    from version import COLLABORATION_PROTOCOL_VERSION, PLUGIN_VERSION

MAX_FRAME = 64 * 1024 * 1024


@dataclass(frozen=True)
class SshFolder:
    host: str
    path: str
    port: int = 22
    username: str = ""

    @property
    def location(self):
        host = f"[{self.host}]" if ":" in self.host else self.host
        port = f":{self.port}" if self.port != 22 else ""
        # Usernames intentionally do not travel in shared invitations/history.
        return f"ssh://{host}{port}{urllib.parse.quote(self.path, safe='/')}"

    @property
    def volatile(self):
        return self.path == "/dev/shm" or self.path.startswith("/dev/shm/")


def parse_ssh_folder(value):
    value = str(value or "").strip()
    if not value or "\\" in value or any(ord(c) < 32 for c in value):
        raise ValueError("Enter a Linux folder as host/absolute/path or ssh://host/path")
    if value.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", value):
        raise ValueError("A Linux server folder needs a host name before its absolute path")
    if "://" not in value:
        value = re.sub(r"^([^/]+):/", r"\1/", value)  # host:/path
        value = "ssh://" + value
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "ssh" or parsed.query or parsed.fragment or parsed.password is not None:
        raise ValueError("Use ssh://host/path without a password, query, or fragment")
    host = parsed.hostname or ""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:%_-]*", host) or "%" in host:
        raise ValueError("Invalid SSH host name")
    path = urllib.parse.unquote(parsed.path)
    if not path.startswith("/") or path == "/" or ".." in PurePosixPath(path).parts or "\\" in path or any(ord(c) < 32 for c in path):
        raise ValueError("Select a specific absolute Linux folder without '..' components")
    username = urllib.parse.unquote(parsed.username or "")
    if username and not re.fullmatch(r"[A-Za-z0-9_.@-]+", username):
        raise ValueError("Invalid SSH login name")
    port = parsed.port or 22
    if not 1 <= port <= 65535:
        raise ValueError("SSH port must be between 1 and 65535")
    return SshFolder(host, str(PurePosixPath(path)), port, username)


def looks_like_ssh_folder(value):
    """Avoid interpreting Windows paths as URLs while recognizing pasted hosts."""
    try:
        target = parse_ssh_folder(value)
        return str(value).startswith("ssh://") or any(c in target.host for c in ".-") or bool(target.username)
    except ValueError:
        return False


def key_fingerprint(key):
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")


class SshHostKeyRequired(LiveCollaborationError):
    def __init__(self, hostname, key):
        self.host_key = {"host": hostname, "type": key.get_name(), "key": key.get_base64(), "fingerprint": key_fingerprint(key)}
        super().__init__(f"Verify the SSH server identity for {hostname}: {self.host_key['fingerprint']}")


def trust_host_key(path, details):
    """Called only after the user explicitly accepts the displayed fingerprint."""
    import paramiko

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    hosts = paramiko.HostKeys(str(path)) if path.exists() else paramiko.HostKeys()
    key = paramiko.PKey.from_type_string(details["type"], base64.b64decode(details["key"], validate=True))
    if key_fingerprint(key) != details["fingerprint"]:
        raise ValueError("SSH fingerprint does not match the presented host key")
    if details["host"] in hosts and not hosts.check(details["host"], key):
        raise ValueError("An existing SSH identity differs; refusing to replace it")
    hosts.add(details["host"], key.get_name(), key)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        hosts.save(str(temporary))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def helper_bundle():
    root = Path(__file__).parent
    sources = {name: root.joinpath(name + ".py").read_text(encoding="utf-8-sig") for name in ("features", "version", "collaboration")}
    return base64.b64encode(zlib.compress(json.dumps(sources).encode(), 6)).decode()


# Python 3.10+ and NumPy must be available on the SSH host. There is no daemon,
# disk installation, new listening port, or remote package install in this code.
REMOTE_BOOTSTRAP = r'''
import sys, json, base64, zlib, types, threading, os
from concurrent.futures import ThreadPoolExecutor
MAX_FRAME = 64 * 1024 * 1024
output_lock = threading.Lock()
def send(value):
    data = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8') + b'\n'
    if len(data) > MAX_FRAME:
        data = json.dumps({'id': value.get('id'), 'error': 'SSH response exceeds the safe frame limit'}).encode() + b'\n'
    with output_lock:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
try:
    if sys.version_info < (3, 10):
        raise RuntimeError('The SSH host needs Python 3.10 or newer; select its executable under Remote Python')
    try:
        import numpy
    except ImportError:
        raise RuntimeError('The selected remote Python has no NumPy. Select a Python environment with NumPy under Remote Python. Nothing was installed on the server.')
    send({'hello': True})
    setup_line = sys.stdin.buffer.readline(MAX_FRAME + 1)
    if len(setup_line) > MAX_FRAME:
        raise RuntimeError('SSH setup is too large')
    setup = json.loads(setup_line)
    sources = json.loads(zlib.decompress(base64.b64decode(setup['bundle'])))
    for name in ('features', 'version', 'collaboration'):
        module = types.ModuleType(name)
        module.__file__ = '<LiveSegmentation SSH helper>/' + name + '.py'
        sys.modules[name] = module
        exec(compile(sources[name], module.__file__, 'exec'), module.__dict__)
    from pathlib import Path
    folder = Path(setup['folder']).resolve()
    if not folder.is_dir() or not os.access(folder, os.R_OK | os.W_OK | os.X_OK):
        raise RuntimeError('The selected remote folder must already exist and be readable/writable by your SSH login: ' + str(folder))
    os.umask(0o002)
    core = sys.modules['collaboration']
    client = core.SharedFolderRoomClient(str(folder), setup['user'])
    allowed = core.LAN_RELAY_METHODS
    slots = threading.BoundedSemaphore(16)
    pool = ThreadPoolExecutor(max_workers=8)
    send({'ready': True, 'version': core.PLUGIN_VERSION})
except Exception as exc:
    send({'ready': False, 'error': str(exc)})
    sys.exit(1)
def dispatch(request):
    try:
        method = str(request.get('method', ''))
        if method not in allowed:
            raise RuntimeError('Unsupported SSH collaboration request')
        value = getattr(client, method)(*(request.get('args') or []), **(request.get('kwargs') or {}))
        send({'id': request['id'], 'result': value})
    except Exception as exc:
        send({'id': request.get('id'), 'error': str(exc)})
    finally:
        slots.release()
try:
    while True:
        line = sys.stdin.buffer.readline(MAX_FRAME + 1)
        if not line:
            break
        if len(line) > MAX_FRAME or not line.endswith(b'\n'):
            raise RuntimeError('SSH request exceeds the safe frame limit')
        request = json.loads(line)
        slots.acquire()
        pool.submit(dispatch, request)
finally:
    pool.shutdown(wait=True)
    client._artifact_queue.join()
'''


class SshRoomClient(LanRoomClient):
    transport_kind = "ssh-folder"

    def __init__(self, location, user_name, username, password="", known_hosts=None, remote_python="python3", timeout_seconds=10.0):
        self.target = parse_ssh_folder(location)
        self.location = self.target.location
        self.user_name = str(user_name or "").strip()
        self.username = self.target.username or str(username or "").strip()
        if not self.user_name or not self.username:
            raise ValueError("Enter your display name and SSH login name")
        if not remote_python or any(ord(c) < 32 for c in remote_python):
            raise ValueError("Enter a remote Python executable, not a shell command")
        self.remote_python = str(remote_python)
        self.password = str(password or "")
        self.known_hosts = Path(known_hosts) if known_hosts else Path.home() / ".ssh" / "live_segmentation_known_hosts"
        self.timeout_seconds = float(timeout_seconds)
        self.presence_session_id = uuid.uuid4().hex
        self._ssh = None
        self._channel = None
        self._reader = None
        self._stdout = None
        self._stdin = None
        self._write_lock = threading.Lock()
        self._connect_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending = {}
        self._closed = threading.Event()

    def _ensure_connected(self):
        with self._connect_lock:
            if self._closed.is_set():
                raise LiveCollaborationError("SSH session closed; join the room again")
            if self._channel is not None:
                if self._channel.closed:
                    raise LiveCollaborationError("SSH connection was lost; join the room again")
                return
            try:
                import paramiko
            except ImportError as exc:
                raise LiveCollaborationError("SSH support is not installed. Click Install SSH support, then join again.") from exc

            class VerifyHost(paramiko.MissingHostKeyPolicy):
                def missing_host_key(self, client, hostname, key):
                    raise SshHostKeyRequired(hostname, key)

            ssh = paramiko.SSHClient()
            self._ssh = ssh
            system_hosts = Path.home() / ".ssh" / "known_hosts"
            if system_hosts.is_file():
                ssh.load_system_host_keys(str(system_hosts))
            if self.known_hosts.is_file():
                ssh.load_host_keys(str(self.known_hosts))
            ssh.set_missing_host_key_policy(VerifyHost())
            try:
                ssh.connect(self.target.host, port=self.target.port, username=self.username,
                            password=self.password or None, timeout=5.0, banner_timeout=5.0,
                            auth_timeout=10.0, channel_timeout=10.0, allow_agent=True,
                            look_for_keys=True, compress=False)
                if self._closed.is_set():
                    raise LiveCollaborationError("SSH connection cancelled")
                transport = ssh.get_transport()
                transport.set_keepalive(10)
                command = shlex.quote(self.remote_python) + " -u -c " + shlex.quote(REMOTE_BOOTSTRAP)
                stdin, stdout, stderr = ssh.exec_command(command, timeout=15.0)
                self._stdin = stdin
                self._channel = stdout.channel
                self._stdout = stdout
                threading.Thread(target=self._drain_stderr, args=(stderr,), daemon=True, name="LiveSegmentation-SSH-stderr").start()
                hello_line = stdout.readline(MAX_FRAME + 1)
                hello = json.loads(hello_line) if hello_line else {}
                if not hello.get("hello"):
                    raise LiveCollaborationError(hello.get("error") or "Remote Python did not start; check the Remote Python executable")
                setup = {"bundle": helper_bundle(), "folder": self.target.path, "user": self.user_name}
                stdin.write(json.dumps(setup, separators=(",", ":")) + "\n")
                stdin.flush()
                ready_line = stdout.readline(MAX_FRAME + 1)
                ready = json.loads(ready_line) if ready_line else {}
                if not ready.get("ready"):
                    raise LiveCollaborationError(ready.get("error") or "Remote Python did not start; check Remote Python and SSH shell access")
                if ready.get("version") != PLUGIN_VERSION:
                    raise LiveCollaborationError("SSH helper version mismatch")
                self._channel.settimeout(self.timeout_seconds)
                self._reader = threading.Thread(target=self._read_responses, name="LiveSegmentation-SSH-receive", daemon=True)
                self._reader.start()
            except Exception:
                ssh.close()
                self._channel = None
                raise
            finally:
                self.password = ""

    @staticmethod
    def _drain_stderr(stderr):
        # Drain without retaining login banners or other potentially sensitive
        # remote shell output in diagnostics or an unbounded memory buffer.
        try:
            while stderr.read(4096):
                pass
        except Exception:
            pass

    def _read_responses(self):
        try:
            while not self._closed.is_set():
                line = self._stdout.readline(MAX_FRAME + 1)
                if not line or len(line) > MAX_FRAME:
                    raise LiveCollaborationError("SSH response stream closed or exceeded the frame limit")
                response = json.loads(line)
                with self._pending_lock:
                    pending = self._pending.get(str(response.get("id")))
                if pending is not None:
                    try:
                        pending.put_nowait(response)
                    except queue.Full:
                        pass
        except Exception as exc:
            self._fail_pending(str(exc))
            self.close()

    def _fail_pending(self, message):
        with self._pending_lock:
            pending = list(self._pending.values())
        for result in pending:
            try:
                result.put_nowait({"error": message})
            except queue.Full:
                pass

    def _rpc(self, method, *args, **kwargs):
        if method not in LAN_RELAY_METHODS:
            raise LiveCollaborationError("Unsupported SSH collaboration request")
        self._ensure_connected()
        request_id = uuid.uuid4().hex
        data = (json.dumps({"id": request_id, "method": method, "args": args, "kwargs": kwargs}, separators=(",", ":")) + "\n").encode("utf-8")
        if len(data) > MAX_FRAME:
            raise LiveCollaborationError("SSH request exceeds the safe frame limit")
        result = queue.Queue(maxsize=1)
        with self._pending_lock:
            if self._closed.is_set():
                raise LiveCollaborationError("SSH session is closed")
            self._pending[request_id] = result
        try:
            with self._write_lock:
                self._channel.sendall(data)
            response = result.get(timeout=self.timeout_seconds)
            if "error" in response:
                raise LiveCollaborationError(response["error"])
            return response["result"]
        except queue.Empty as exc:
            raise LiveCollaborationError(f"SSH request '{method}' did not respond within {self.timeout_seconds:g} seconds") from exc
        except OSError as exc:
            raise LiveCollaborationError("SSH connection was interrupted; join the room again") from exc
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def preflight(self, room_name, signature, plugin_version=PLUGIN_VERSION, protocol_version=COLLABORATION_PROTOCOL_VERSION):
        started = time.monotonic()
        report = self._rpc("preflight", room_name, signature, plugin_version, protocol_version)
        report["transport"] = "ssh-folder"
        report["latency_seconds"] = round(time.monotonic() - started, 4)
        report.setdefault("checks", []).insert(0, {"id": "ssh", "status": "pass", "title": "Encrypted SSH connection", "detail": "Authenticated session with verified server identity; no public relay port is used."})
        if self.target.volatile:
            report["checks"].append({"id": "volatile-storage", "status": "warning", "title": "Temporary RAM storage", "detail": "/dev/shm is normally cleared on restart. Save final segmentations and backups to persistent storage."})
            if report.get("status") != "fail":
                report["status"] = "warning"
        return report

    def close(self):
        self._closed.set()
        self.password = ""
        self._fail_pending("SSH session closed")
        if self._ssh is not None:
            self._ssh.close()
