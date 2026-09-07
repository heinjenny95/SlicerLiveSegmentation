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
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

if __name__ == "__main__" and sys.argv[1:] == ["--local-worker"]:
    # Use this installed module's directory, never a source-data folder.
    sys.path.insert(0, str(Path(__file__).resolve().parent))

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
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # The GUI must not import SSH/crypto libraries, even for the trust dialog.
    host, kind, encoded = (str(details[name]) for name in ("host", "type", "key"))
    if not re.fullmatch(r"[A-Za-z0-9.:[\]_-]+", host):
        raise ValueError("Invalid SSH host identity")
    blob = base64.b64decode(encoded, validate=True)
    size = int.from_bytes(blob[:4], "big")
    if not 0 < size < 128 or len(blob) > 16384 or blob[4:4 + size].decode("ascii") != kind:
        raise ValueError("Invalid SSH public key")
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    if fingerprint != details["fingerprint"]:
        raise ValueError("SSH fingerprint does not match the presented host key")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for line in lines:
        parts = line.split()
        if len(parts) >= 3 and host in parts[0].split(","):
            if parts[1:3] != [kind, encoded]:
                raise ValueError("An existing SSH identity differs; refusing to replace it")
            return
    lines.append(f"{host} {kind} {encoded}")
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
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
        self.connection_stage = "Preparing SSH connection"
        self.stage_callback = None

    def _set_stage(self, stage):
        self.connection_stage = stage
        if self.stage_callback:
            self.stage_callback(stage)

    def _ensure_connected(self):
        with self._connect_lock:
            if self._closed.is_set():
                raise LiveCollaborationError("SSH session closed; join the room again")
            if self._channel is not None:
                if self._channel.closed:
                    raise LiveCollaborationError("SSH connection was lost; join the room again")
                return
            self._set_stage("Loading local SSH libraries")
            try:
                import paramiko
            except ImportError as exc:
                raise LiveCollaborationError("SSH support is not installed. Click Install SSH support, then join again.") from exc

            class VerifyHost(paramiko.MissingHostKeyPolicy):
                def missing_host_key(self, client, hostname, key):
                    raise SshHostKeyRequired(hostname, key)

            ssh = paramiko.SSHClient()
            self._ssh = ssh
            self._set_stage("Reading verified server identities")
            system_hosts = Path.home() / ".ssh" / "known_hosts"
            if system_hosts.is_file():
                ssh.load_system_host_keys(str(system_hosts))
            if self.known_hosts.is_file():
                ssh.load_host_keys(str(self.known_hosts))
            ssh.set_missing_host_key_policy(VerifyHost())
            try:
                self._set_stage("Connecting to SSH server and signing in")
                ssh.connect(self.target.host, port=self.target.port, username=self.username,
                            password=self.password or None, timeout=5.0, banner_timeout=5.0,
                            auth_timeout=10.0, channel_timeout=10.0, allow_agent=not bool(self.password),
                            look_for_keys=not bool(self.password), compress=False)
                if self._closed.is_set():
                    raise LiveCollaborationError("SSH connection cancelled")
                transport = ssh.get_transport()
                transport.set_keepalive(10)
                command = shlex.quote(self.remote_python) + " -u -c " + shlex.quote(REMOTE_BOOTSTRAP)
                self._set_stage("Starting Python on the SSH server")
                stdin, stdout, stderr = ssh.exec_command(command, timeout=15.0)
                self._stdin = stdin
                self._channel = stdout.channel
                self._stdout = stdout
                threading.Thread(target=self._drain_stderr, args=(stderr,), daemon=True, name="LiveSegmentation-SSH-stderr").start()
                hello_line = stdout.readline(MAX_FRAME + 1)
                hello = json.loads(hello_line) if hello_line else {}
                if not hello.get("hello"):
                    raise LiveCollaborationError(hello.get("error") or "Remote Python did not start; check the Remote Python executable")
                self._set_stage("Preparing remote helper and checking folder access")
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
                self._set_stage("Opening collaboration room")
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
                if "stage" in response:
                    self.connection_stage = str(response["stage"])
                    if self.stage_callback:
                        self.stage_callback(self.connection_stage)
                    continue
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


class _ProcessInput:
    def __init__(self, process):
        self.process = process

    @property
    def closed(self):
        return self.process.poll() is not None

    def sendall(self, data):
        self.process.stdin.write(data)
        self.process.stdin.flush()


class SshProcessRoomClient(SshRoomClient):
    """Isolate optional crypto imports and SSH inside a cancellable local worker.

    Credentials cross an anonymous stdin pipe, never a command line, environment
    variable, network listener or temporary configuration file.
    """

    def __init__(self, *args, python_executable=None, startup_timeout=40.0, **kwargs):
        super().__init__(*args, **kwargs)
        self.python_executable = str(python_executable or sys.executable)
        self.startup_timeout = float(startup_timeout)
        self._process = None
        self._reap_started = False

    def _ensure_connected(self):
        with self._connect_lock:
            if self._closed.is_set():
                raise LiveCollaborationError("SSH session closed; join the room again")
            if self._channel is not None:
                if self._channel.closed:
                    raise LiveCollaborationError("Local SSH worker stopped; join the room again")
                return
            self._set_stage("Starting isolated local SSH worker")
            ready = queue.Queue(maxsize=1)
            with self._pending_lock:
                self._pending["__setup__"] = ready
            try:
                script = Path(__file__).resolve()
                environment = os.environ.copy()
                environment.pop("PYTHONPATH", None)
                environment["PYTHONNOUSERSITE"] = "1"
                process = subprocess.Popen(
                    [self.python_executable, "-u", "-s", str(script), "--local-worker"],
                    cwd=str(script.parent), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, env=environment,
                    **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
                )
                self._process = process
                if self._closed.is_set():
                    self.close()
                    raise LiveCollaborationError("SSH setup cancelled")
                self._channel = _ProcessInput(process)
                self._stdout = process.stdout
                self._reader = threading.Thread(target=self._read_responses, daemon=True, name="LiveSegmentation-local-SSH-receive")
                self._reader.start()
                threading.Thread(target=self._drain_stderr, args=(process.stderr,), daemon=True, name="LiveSegmentation-local-SSH-stderr").start()
                setup = dict(location=self.location, user_name=self.user_name, username=self.username,
                             password=self.password, known_hosts=str(self.known_hosts),
                             remote_python=self.remote_python, timeout_seconds=self.timeout_seconds,
                             presence_session_id=self.presence_session_id)
                try:
                    self._channel.sendall((json.dumps(setup) + "\n").encode("utf-8"))
                finally:
                    setup.clear()
                    self.password = ""
                response = ready.get(timeout=self.startup_timeout)
                if "error" in response:
                    error = LiveCollaborationError(f"{response['error']}\nSetup step: {self.connection_stage}")
                    if response.get("host_key"):
                        error.host_key = response["host_key"]
                    raise error
                if response.get("result") != PLUGIN_VERSION:
                    raise LiveCollaborationError("Local SSH worker version mismatch")
            except queue.Empty as exc:
                stage = self.connection_stage
                self.close()
                raise LiveCollaborationError(f"SSH setup timed out during: {stage}. The local worker was stopped; this does not by itself mean the server is offline.") from exc
            except Exception:
                self.close()
                raise
            finally:
                self.password = ""
                with self._pending_lock:
                    self._pending.pop("__setup__", None)

    def preflight(self, room_name, signature, plugin_version=PLUGIN_VERSION, protocol_version=COLLABORATION_PROTOCOL_VERSION):
        return self._rpc("preflight", room_name, signature, plugin_version, protocol_version)

    def close(self):
        super().close()
        process = self._process
        with self._pending_lock:
            if process is None or self._reap_started:
                return
            self._reap_started = True

        def reap():
            try:
                if process.poll() is None:
                    if os.name == "nt":
                        # Include children of Python/venv launchers; only this
                        # specific worker's subtree, never the owning Slicer.
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=5)
                    else:
                        process.terminate()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                if process.poll() is None:
                    process.kill()
                process.wait()

        threading.Thread(target=reap, daemon=True, name="LiveSegmentation-SSH-reap").start()


def run_local_worker():
    from concurrent.futures import ThreadPoolExecutor

    output_lock = threading.Lock()

    def send(value):
        data = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(data) > MAX_FRAME:
            data = (json.dumps({"id": value.get("id"), "error": "Local SSH response exceeds frame limit"}) + "\n").encode()
        with output_lock:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    client = None
    setup = {}
    try:
        setup = json.loads(sys.stdin.buffer.readline(MAX_FRAME + 1))
        session_id = setup.pop("presence_session_id")
        client = SshRoomClient(**setup)
        setup.clear()
        client.presence_session_id = session_id
        client.stage_callback = lambda stage: send({"stage": stage})
        client._ensure_connected()
        send({"id": "__setup__", "result": PLUGIN_VERSION})
    except Exception as exc:
        stage = client.connection_stage if client else "starting local SSH worker"
        send({"id": "__setup__", "error": f"SSH setup failed during {stage}: {exc}", "host_key": getattr(exc, "host_key", None)})
        if client:
            client.close()
        return
    finally:
        setup.clear()

    slots = threading.BoundedSemaphore(16)
    pool = ThreadPoolExecutor(max_workers=8)

    def dispatch(request):
        try:
            method = str(request.get("method", ""))
            if method not in LAN_RELAY_METHODS:
                raise LiveCollaborationError("Unsupported local SSH request")
            value = getattr(client, method)(*(request.get("args") or []), **(request.get("kwargs") or {}))
            send({"id": request["id"], "result": value})
        except Exception as exc:
            send({"id": request.get("id"), "error": str(exc)})
        finally:
            slots.release()

    try:
        while True:
            line = sys.stdin.buffer.readline(MAX_FRAME + 1)
            if not line:
                break
            if len(line) > MAX_FRAME or not line.endswith(b"\n"):
                raise LiveCollaborationError("Local SSH request exceeds frame limit")
            request = json.loads(line)
            slots.acquire()
            pool.submit(dispatch, request)
    finally:
        client.close()
        pool.shutdown(wait=True)


if __name__ == "__main__" and sys.argv[1:] == ["--local-worker"]:
    run_local_worker()
