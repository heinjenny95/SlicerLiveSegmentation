# Live Segmentation 0.10.0

Version 0.10.0 removes the remaining shared-folder polling barriers and keeps
communication visible while users work in Segment Editor.

## Low-latency live lanes

- Edit upload and download, chat send and receive, presence heartbeat, lock set
  and read, health checks, and maintenance now run in fully independent lanes.
  A slow network operation can no longer hold unrelated live results.
- Compact recent-operation and recent-chat feeds avoid relisting complete SMB
  directories every 200 milliseconds. Deterministic per-message indexes also
  make retries constant-time.
- A compact label-owner index makes the normal owner lock path a single cached
  lookup. Non-critical audit writes no longer delay chat, label locks, or edits.
- Windows/SMB atomic replacement retries short reader-sharing windows without
  weakening atomic publication.

## Communication and activity UI

- Room chat is available in a persistent Slicer dock that remains visible while
  Segment Editor or another module is active.
- Dock messages can attach the current Slicer position as a spatial comment and
  retain the existing jump-to-location behavior.
- The timeline is now a live activity feed. New ordered operations immediately
  produce human-readable entries such as “Jenny created label ‘Label 1’” without
  requiring the full-history refresh button.
- Full history refreshes merge with already received activity, avoiding a race
  that could hide a just-arrived event.

## Verification

- 27 automated transport/server tests pass, including regressions proving that
  normal operation and chat polling do not relist their network directories.
- Ruff, compilation, and the complete Slicer 5.12.3 UI smoke suite pass.
- Two simultaneous Slicer processes exchanged bidirectional edits and converged
  on the same 35-voxel label.

---

# Previous release: Live Segmentation 0.9.1

Version 0.9.1 adds bounded parallel processing for independent work on shared
folders and the optional collaboration server, while preserving deterministic
ordering wherever edits can affect the same label.

## Parallel collaboration pipeline

- Outgoing voxel edits are grouped by label. Each label keeps its local edit
  order, while different labels may synchronize concurrently.
- Presence updates, permanent chat publication, and label-lock changes execute
  as independent realtime tasks instead of waiting for one another.
- Lock changes for different labels run concurrently.
- Incoming operation, presence, chat, label-owner, lock, audit, role, review,
  access-request, snapshot, conflict, history-archive, and backup metadata files
  are read through a bounded pool of up to eight I/O workers.
- The advanced history/review/diagnostics refresh fetches its independent data
  sets concurrently.
- Global voxel ordering and edits inside the same label remain serialized to
  preserve deterministic convergence and avoid corrupting shared state.

## Verification

- 25 automated tests pass. The new regression test proves that independent
  operation files are read concurrently while their returned sequence order is
  preserved.
- Ruff and Python compilation pass.

---

# Previous release: Live Segmentation 0.9.0

Version 0.9.0 replaces the monolithic shared-folder polling loop with independent
realtime, edit, and maintenance lanes after a two-user network-share trial.

## Realtime collaboration engine

- Presence, permanent chat, and label locks no longer wait for voxel operations,
  version-history scans, project backups, diagnostics, or snapshots.
- Shared-folder chat uses its own short sequence lock; label locks and backup
  reservations no longer contend for the voxel-operation sequence lock.
- Own chat messages render immediately and are reconciled with their permanent
  room record without a duplicate line.
- Presence heartbeat expiry now tolerates slow SMB/network shares while an
  explicit leave still disappears immediately.
- A compact label-owner index eliminates repeated full operation-history scans
  during lock polling.
- Voxel sequence allocation uses a compact counter, and expensive overlap/conflict
  analysis runs after releasing the publishing lock.
- Ephemeral high-frequency JSON records avoid synchronous disk flushes that are
  disproportionately slow on SMB shares; atomic publication remains intact.

## Clear controls and working backups

- **Sync now** refreshes only live edits, participants, chat, locks, and health;
  history and backup lists retain their own explicit refresh actions.
- **Label to manage** makes lock, ownership, access, and review selection visible
  and also activates that label in Slicer's Segment Editor.
- Automatic backup enablement, interval, and retention remain editable while a
  shared-folder room is active, and **Back up now** creates an immediate MRB.
- Maintenance failures no longer report a successful live edit or chat action as
  a room disconnect.

## Verification

- 24 automated shared-folder/server protocol tests pass, including a regression
  that holds the voxel sequence lock while chat, presence, and a label lock
  complete in under 0.5 seconds.
- Ruff and Python compilation pass.
- Real Slicer 5.12.3 UI smoke tests pass optimistic chat, explicit label selection,
  editable backups, lock round-trips, and manual live sync.
- Two simultaneous Slicer 5.12.3 processes exchanged bidirectional edits and
  converged on the same 35-voxel label.

---

# Previous release: Live Segmentation 0.8.1

Version 0.8.1 contains the complete collaboration, review, recovery, diagnostics,
and publication-readiness roadmap selected after the 0.7.0 two-user test. The
time-series/4D proposal is intentionally deferred.

## 0.8.1 Windows and SMB hotfix

- Shared-folder sequence locking now retries the transient Windows access-denied
  race that can occur while another client removes the lock directory.
- The concurrency regression suite now covers this Windows/SMB behavior directly.
- Presence-expiry timing tests use a cross-platform-safe margin.

## Google-Docs-like spatial collaboration

- Rich presence now synchronizes active label, Segment Editor effect, crosshair,
  slice offsets, stable user color, and room role.
- **Jump to user** and **Follow view** align local navigation without modifying
  shared voxels.
- Remote changes receive a short-lived colored spatial outline in a separate,
  display-only MRML Segmentation node.
- Chat messages can carry a permanent spatial anchor and jump back to that view.

## History, snapshots, and conflicts

- Added a searchable operation timeline and append-only restoration of any
  selected revision.
- Full segment checkpoint groups use normal snapshot operations for compatibility.
- Older loose operation JSON files are compressed into ZIP archives while all
  history remains available for audit and restoration.
- Concurrent overlapping voxel operations create persistent conflict records.
- Conflict actions support latest, mine, other revision, and binary union.

## Governance and review

- Added viewer, editor, reviewer, and administrator roles.
- Added draft, in-progress, ready-for-review, changes-requested, and approved
  label states. Approval locks the reviewed label.
- Label locks support expiry, edit requests, ownership transfer, and administrator
  recovery of abandoned locks.
- Shared-folder and server transports record chat, edit, role, lock, review,
  conflict, template, backup, join, and leave audit events.

## Backups, diagnostics, templates, and invitations

- Backup manager lists MRB bundles, stores SHA-256 sidecars, verifies integrity,
  pins milestones, enforces configurable retention, and restores after confirmation.
- Diagnostics report latency, read/write health, free space, pending changes,
  operation/archive/snapshot counts, and backups; sanitized exports omit identity
  and location fields.
- Room material templates standardize segment IDs, names, colors, and optional
  Slicer/DICOM terminology without replacing voxel data.
- Secret-free `.liveseg` invitations configure a room and verify the source-volume
  signature. API keys are never exported.

## Server and publication preparation

- Added optional bearer-token-to-user identity binding and optional HTTPS
  enforcement behind a TLS reverse proxy.
- Added SQLite persistence and API parity for roles, reviews, access requests,
  conflicts, templates, richer presence, anchored chat, and audit events.
- Added Apache-2.0 licensing, citation and contributing metadata, cross-platform
  GitHub Actions CI, secure deployment guidance, and Extensions Index checklist.
- Published the sanitized source, issue templates, security policy, and release
  artifacts at https://github.com/heinjenny95/SlicerLiveSegmentation.

## Verification

- Ruff and Python compilation pass.
- 21 automated tests pass.
- Real Slicer 5.12.3 advanced test passed presence, spatial chat, templates,
  review/access requests, snapshot compaction, conflict detection, diagnostics,
  valid MRB backup, connection loss/recovery, and clean leave/rejoin.

---

## Previous release: 0.7.0

Version 0.7.0 adds the collaboration controls requested after the first two-user
shared-folder trial.

## Connection status and refresh

- Active server and shared-folder rooms are continuously health-checked.
- Failed reads or writes immediately replace the green live state with a red
  connection error and a visible error dialog instead of leaving a false online
  indication.
- Slow successful synchronization cycles of 2.5 seconds or more are shown in
  amber with their measured round-trip duration.
- **Refresh now** checks the connection, participants, edits, chat, and locks
  immediately without leaving the room. Slicer's developer **Reload** action is
  no longer needed for this purpose.

## Permanent room chat

- Added a built-in chat panel with Enter-to-send support.
- Messages are stored permanently as ordered, idempotent room records.
- Both the shared-folder and optional HTTP-server transports provide the same
  history and replay behavior.

## Label ownership and locking

- The author of a label's first synchronized operation is recorded as its owner.
- Owners can lock and unlock the currently selected label.
- Non-owners cannot change the lock. Their edits to a locked label are reverted
  locally and rejected by both transports.
- Ownership and lock state persist across restarts and rejoining.

## Automatic backups

- Shared-folder rooms periodically save complete Slicer `.mrb` project bundles.
- The interval is configurable from 1 to 1440 minutes and defaults to 5 minutes.
- Backups are coordinated room-wide, published atomically, and all older bundles
  remain in the room's `backups` directory.

## Clean room lifecycle

- Every join now creates exactly one dedicated local room replica.
- A selected pre-existing segmentation used to seed a new room is copied rather
  than converted into disposable live state.
- Leave detaches Segment Editor, clears the shared-segmentation field, removes
  all room labels from the MRML scene, and discards delayed results from older
  synchronization threads.
- Repeated leave/rejoin cycles therefore cannot stack duplicate labels.

## Validation

- Ruff and Python compilation passed.
- 14 automated transport, API, chat, lock, health, backup-reservation, delta, and
  SMB compatibility tests passed.
- Two simultaneous Slicer 5.12.3 processes converged from 27 to 35 voxels.
- Slicer tests passed for connection-loss detection and recovery, permanent chat,
  lock/unlock, manual refresh, clean leave/rejoin, and a valid complete-project
  `.mrb` backup.
