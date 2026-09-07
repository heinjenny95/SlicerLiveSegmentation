# Linux folder collaboration over SSH

Live Segmentation 0.15.0 can run its folder operations on an existing Linux
server through an encrypted SSH session. It does not interpret a Linux path as
an HTTP endpoint or a Windows network share.

## Connect

1. Install the module ZIP on every participant's computer and restart Slicer
   after saving current work. nnInteractive remains independently installed.
2. Choose **Linux server folder (SSH)**. Enter `linux-host/dev/shm/team`,
   `linux-host:/absolute/folder`, or `ssh://linux-host:22/absolute/folder`.
3. Enter your own **SSH login** and **Password**. These are the same credentials
   you use for an SSH terminal. Leave the password empty only if a supported
   SSH agent or default private key already provides authentication.
4. Keep **Remote Python** at `python3`, or enter the absolute executable path
   of an existing environment with Python 3.10+ and NumPy. This is an executable,
   not a shell command such as `conda activate ...`.
5. Click **Install SSH support** once if needed. This installs Paramiko locally
   into Slicer's Python environment in the background; it does not install
   anything on the Linux server.
6. Load the same source volume locally on both computers. Use different display
   names but the same folder and room name. Click **Join live room**.
7. At first use, compare the displayed SHA256 server fingerprint with the one
   obtained through your administrator or an already trusted connection. Only
   trust a matching fingerprint, then click Join again. A changed known key is
   rejected, never automatically replaced.

The folder must already exist and be readable, writable and traversable by
both SSH accounts. Have its owner/administrator configure shared group or ACL
permissions, including inheritance for newly created files. The plugin uses a
group-friendly umask but cannot grant access that your account does not have.
Do not solve permission errors by making sensitive data world-writable.

## Storage and lifecycle

Since 0.15.1, SSH and cryptography run inside a dedicated local Python process,
not inside Slicer's Python interpreter. The worker uses Slicer's existing Python
runtime, a local module working directory, no user-site packages and no inherited
PYTHONPATH. Credentials pass through an anonymous pipe, never process arguments,
environment variables or a temporary file. Cancelling a connection terminates
only that worker (including Python launcher children on Windows).

Connection progress identifies library loading, server login, remote Python
startup and folder preparation. A timeout reports the last setup step; it does
not automatically diagnose the server as offline. An entered password bypasses
agent/key discovery; leave it empty to use a supported SSH agent/default key.

Room data lives at `<selected folder>/LiveSegmentation/rooms/...`, using the
same ordered operations, stable label identities, locks and history as shared
folder mode. A small Python helper runs in memory for each SSH connection and
ends after disconnect and pending work. No daemon or additional public TCP
listener is installed. Concurrent RPCs allow chat and presence to proceed while
another request is waiting, subject to server resources and SSH bandwidth.

**`/dev/shm` is normally temporary RAM-backed storage. Its contents may disappear
on restart or unmount and are limited by RAM/swap and filesystem capacity.**
Use a persistent server folder for durable room history, or save final
segmentations/projects separately to persistent storage before ending work.
Automatic full `.mrb` project upload is not implemented for SSH mode; its backup
controls are hidden and the UI explicitly directs you to Slicer's **Save**.
Room operation history is not a substitute for a complete project backup.

Password fields are not persisted in settings, invitations, room files or
diagnostics. The in-memory client clears its password after connection setup;
the UI clears it after joining and on cleanup. Previously used locations are
remembered without a username or password, but startup remains disconnected
with an empty active location. Leave cancels local workers and closes the SSH
connection without waiting for remote disk operations on the GUI thread.

## Security and compatibility

- SSH host identities use the user's normal `~/.ssh/known_hosts` plus
  `~/.ssh/live_segmentation_known_hosts`; only public keys are stored there.
- Usernames in a pasted `ssh://user@host/path` are accepted, but never exported
  in invitations. Passwords in URLs and parent-directory traversal are rejected.
- Password authentication, default keys and SSH agents are supported. MobaXterm
  saved sessions, jump-host configuration, arbitrary SSH config directives and
  interactive MFA dialogs are not imported. Such deployments may require
  administrator configuration or the HTTPS transport instead.
- The server runs the helper under your existing SSH account. SSH access is not
  permission to connect to hosts or folders outside your authorized project.
- Every participant needs version 0.15.0 or newer to use this mode. Protocol 3
  and existing room data remain unchanged. Neither inference nor source-image
  loading is transferred to the other participant's computer.
- Host reachability, network latency, remote filesystem stalls, limited memory
  and local Slicer rendering still affect response times; this is not a hard
  real-time system. Connection setup and network waits run outside the GUI.

## Verification

Automated tests start an actual encrypted loopback SSH server and separate
Python helper processes. They cover two-client voxels/chat/presence/history,
idempotency, parallel requests, unknown and changed keys, failed authentication,
missing folders and clean cancellation. A Slicer 5.12.3 test verifies path
auto-detection, joining, incoming paint, outgoing erase, chat and an exact
7-voxel rejoin. Loopback results are not measurements of an institutional server.
