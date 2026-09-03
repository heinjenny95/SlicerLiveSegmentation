# Live Segmentation 0.12.1

Version 0.12.1 fixes the module panel occupying an excessive fraction of the
screen on narrow and lower-resolution displays.

## Responsive module panel

- Wide one-line action bars for connection, presence, locks, review, chat,
  backups, history, conflict resolution, diagnostics, quality checks, and
  invitations now use compact multi-row layouts.
- Long functionality remains available without horizontal clipping; the normal
  Slicer module panel continues to scroll vertically.
- The crash-recovery checkbox uses a shorter visible label and keeps its full
  explanation as a tooltip.
- A real Slicer 5.12.3 UI-width smoke test measures a minimum module-content
  width of 288 pixels, reduced from 741 pixels in version 0.12.0.
- All collaboration behavior and room formats remain unchanged.

---

# Previous release: Live Segmentation 0.12.0

Version 0.12.0 turns the existing low-latency collaboration core into a more
complete biological segmentation workspace. Room templates were deliberately
left unchanged in this release.

## Faster connection choices and visible performance

- **Direct LAN + shared-folder fallback** starts a dependency-free relay inside
  Slicer, protects it with a temporary session code, prefers the LAN path, and
  falls back to the identical persistent shared-folder room.
- The performance line separates publish, receive, local apply/render, chat,
  and full edit-roundtrip latency. A non-destructive five-sample benchmark
  reports minimum, median, p95, maximum, errors, and an overall rating.
- The direct relay and every existing live lane remain background operations;
  an unavailable LAN host or shared folder does not block Slicer's GUI thread.

## Collaboration, review, and recovery

- Persistent spatial comments attach a discussion to a label and Slicer
  location, support jump-to-position and resolution, and reuse the permanent
  room-chat log.
- A persistent activity dock keeps remote label updates visible while Segment
  Editor or another module is open; the existing voxel highlight remains.
- Collaborative undo publishes an append-only inverse operation and reverts
  only voxels that still contain the value written by the selected edit, so
  later collaborator changes are preserved.
- Unacknowledged edits are written atomically to a local, context-bound crash
  journal and may be replayed idempotently after joining the same room and
  dataset. Explicitly leaving clears the journal.
- Review states are displayed as a priority queue. Historical revisions can be
  compared with the current segmentation in a separate green/red Added/Removed
  overlay without changing shared data.

## Quality, invitations, and research measurements

- Read-only QA checks detect empty labels, disconnected and small components,
  and overlapping label voxels.
- The Windows installer registers `.livesegroom`; double-clicking an invitation
  opens Slicer, selects Live Segmentation, and populates the connection. Direct
  invitations contain the temporary session code and must be shared privately.
- An anonymized session export contains latency distributions, byte/count
  totals, fallback counts, and aggregate QA counts while omitting user names,
  room names, URLs, paths, images, and segmentation payloads.

## Verification

- 54 automated tests pass, including real localhost two-client LAN relay,
  invalid-code rejection, LAN-outage fallback, crash journal, invitation,
  metrics, QA, server schema, and all prior protocol behavior.
- Real Slicer 5.12.3 shared-folder and direct-LAN smoke tests pass. The extended
  direct-LAN run published a small edit in about 0.2 seconds and completed
  spatial comment, QA, benchmark, review queue, revision-overlay, collaborative
  undo, and end-to-end metrics checks.
- Ruff, compileall, installer packaging, archive validation, normal launch, Safe
  Start, and double-click invitation checks are part of the release process.

---

# Previous release: Live Segmentation 0.11.6

Version 0.11.6 restores convenient access to previously used collaboration
locations without reintroducing network-dependent Slicer startup.

## Safe recent-folder history

- **Shared folder** is now an editable dropdown containing up to eight paths
  whose connection and room health check completed successfully.
- The active path still starts empty after every normal Slicer launch. Building
  and opening the dropdown reads only local settings; it does not resolve,
  enumerate, or probe any saved network location.
- Selecting an entry only copies its text into the field. Access to the shared
  folder begins after the user clicks **Join live room** and remains cancellable
  on a background lane.
- Failed or timed-out paths are never added. Reusing a successful path moves it
  to the top, Windows paths are de-duplicated case-insensitively, and **Clear
  list** removes the local history.
- Safe Start continues to use isolated settings, so it intentionally does not
  import the normal profile's recent-folder list.

## Verification

- 47 automated tests cover the bounded and de-duplicated history helpers as
  well as the existing live/slow/offline, transport, deletion, backup, and
  collaboration behavior.
- A real Slicer 5.12.3 smoke test verifies blank startup state, dropdown
  selection, successful-path promotion, and persistence without startup path
  access. Ruff, Python compilation, release validation, normal launch, and Safe
  Start regression checks remain part of the release process.

---

# Previous release: Live Segmentation 0.11.5

Version 0.11.5 fixes a false active-session disconnect that could still occur
when a reachable shared folder or a large local segmentation update took more
than ten seconds to complete.

## Two-stage active-session watchdog

- Ten seconds without a completed shared-folder request now changes the status
  to an orange **responding slowly** warning and requests a dedicated health
  check. The room, shared labels, chat, and locks remain loaded.
- A local reset now requires 30 seconds of complete transport silence or an
  explicit failed read/write health check. A genuine returned access error can
  therefore still disconnect promptly, while a temporary SMB backlog does not.
- The timestamp used by the network watchdog is refreshed after a received
  operation has finished applying to Slicer's MRML scene. CPU, voxel conversion,
  and rendering time on a large dataset no longer masquerade as network delay.
- The 15-second, cancellable, non-blocking initial connection watchdog from
  version 0.11.4 remains unchanged.

## Startup recovery

- Legacy room, transport, shared-folder, and server targets are removed during
  scripted-module import, before the Live Segmentation widget is constructed.
- The Windows installer now creates **Live Segmentation Safe Start** alongside
  the normal shortcut. Safe Start uses Slicer's supported `--disable-settings`
  and `--ignore-slicerrc` switches, bypassing stale normal-profile and startup-
  script state without deleting that profile or changing separately installed
  extensions.

## Verification

- 45 automated tests cover the live/slow/offline boundaries in addition to the
  existing transport, deletion, backup, and collaboration suite.
- Ruff, Python compilation, release validation, normal-profile and isolated-
  settings Safe Start tests, and real Slicer smoke tests
  confirm that slow warnings preserve the room while hard failures still reset
  locally without blocking the interface or shutdown.

---

# Previous release: Live Segmentation 0.11.4

Version 0.11.4 prevents slow but reachable institutional SMB shares from being
reported as offline during connection setup or a brief live-session stall.

## Network-latency tolerance

- The non-blocking connection watchdog now allows 15 seconds instead of four.
  The status line displays elapsed time while the background attempt is pending,
  and **Cancel connection** remains available throughout.
- Active shared-folder sessions now allow 10 seconds for a completed live-sync
  request or read/write validation instead of disconnecting after three seconds.
- All waits remain confined to daemon background lanes. The longer tolerance
  therefore does not reintroduce the Slicer startup, folder-switching, shutdown,
  or interface freezes fixed in version 0.11.3.
- Timeout messages are generated from the configured watchdog values so the UI
  cannot show stale hard-coded durations after future tuning.

## Verification

- Automated tests, Ruff, Python compilation, release-archive validation, and a
  real Slicer connection-watchdog smoke test cover the longer deadlines while
  retaining immediate local cancellation and shutdown.

---

# Previous release: Live Segmentation 0.11.3

Version 0.11.3 prevents an unavailable SMB/UNC collaboration folder from
freezing Slicer's interface, folder switching, or application shutdown.

## Non-blocking connection lifecycle

- Shared-folder and server joins now run on a daemon background lane. The Join
  button returns immediately and becomes **Cancel connection** while the request
  is pending.
- A collaboration location that does not answer within four seconds is cancelled
  locally. No blocked Windows filesystem call can hold Slicer's GUI thread.
- Leaving a room clears the shared MRML replica, fields, chat, locks, and UI state
  immediately. The optional remote presence/audit cleanup runs best-effort in a
  daemon thread, so even an indefinitely blocked UNC call cannot prevent Slicer
  from closing.
- QFileSystemWatcher is no longer armed on shared folders. Its UNC registration
  and removal methods could synchronously enter Windows networking code; the
  existing 75 ms timer and independent background pull lanes retain live latency
  without that risk.

## Fast failure and clean restart

- An active shared-folder room with no completed live request for three seconds
  is declared unavailable and reset locally. A pending read/write validation
  also has a three-second deadline even if another cached read still succeeds.
- Room name, transport, shared-folder path, and server target are session-only.
  Setup removes connection values saved by earlier releases and starts with blank
  room/shared-folder fields plus the neutral local-server default, so Slicer never
  probes or reconnects to the previous network path during startup. User name and
  backup preferences remain saved.
- The folder browser always opens from a local default instead of receiving the
  previous UNC path as its initial directory.
- Newer room schemas now explain the required minimum plugin version and state
  explicitly that every participating computer must update. A 0.11.1 client will
  still show its older generic text for a schema-2 room; installing 0.11.2 or
  newer resolves that expected compatibility rejection.

## Verification

- 43 automated tests, Ruff, and Python compilation pass, including a new
  future-room compatibility-message regression.
- The complete Slicer 5.12.3 resilience run verifies startup removal of a stale
  UNC setting, an immediately returning blocked join, cancellation by watchdog,
  fast outage reset, manual rejoin, chat, locks, history, backups, deletion in
  both directions, 24/24 rapid components, and exact 50/50-voxel leave/rejoin.
- A dedicated shutdown run held both a join call and a leave call in simulated
  60-second network waits. Join returned immediately, local leave completed in
  0.015 seconds, the Slicer process exited after producing the test result, and
  no new Slicer process remained.

---

# Previous release: Live Segmentation 0.11.2

Version 0.11.2 synchronizes deletion of complete Slicer labels. Removing a
segment on one participant now removes that MRML segment for every participant
instead of leaving stale labels on peers.

## Ordered label-deletion tombstones

- Slicer's `SegmentRemoved` event is converted into a small ordered tombstone
  carrying the deleted segment ID, name, color, author, and room sequence.
- The receiver removes the complete segment and clears its local baseline,
  verification, lock, and review state. An empty but intentionally retained
  label remains distinct from a deleted label.
- Tombstones are supported by both shared-folder and optional server transports,
  remain visible as `label deleted` entries in the permanent activity timeline,
  and participate in semantic concurrent-edit conflict detection.
- Historical reconstruction tracks label lifetime: a deleted label is absent at
  later revisions, while a deliberately recreated label with the same ID can
  appear again as a later ordered operation.
- Shared-folder rooms migrate from protocol schema 1 to schema 2 on first join.
  The room records 0.11.2 as its minimum plugin version so older clients stop
  clearly rather than silently displaying a label they cannot delete.

## Verification

- Four deletion and migration regressions increase the automated suite to 42
  tests; Ruff and Python compilation also pass.
- A real Slicer 5.12.3 smoke test confirms local removal publication, application
  of a peer deletion to the MRML scene, deletion-aware historical state, and
  continued absence after a clean leave/rejoin. The same run retained 24/24
  rapid component voxels and restored the remaining label exactly at 51/51
  voxels with a 0.172-second initial edit publication.

---

# Previous release: Live Segmentation 0.11.1

Version 0.11.1 prevents a recoverable Windows/SMB read gap from briefly flashing
an otherwise healthy shared-folder room offline.

## Stable shared-folder health reporting

- Shared JSON reads retry short access-denied, visibility, and incomplete-read
  windows for up to 0.42 seconds before reporting a failure.
- One exhausted live-lane read no longer changes the connection indicator by
  itself. The affected feeds are retried immediately and a dedicated room health
  check decides whether the shared location is actually unavailable.
- Real outages remain visible: the health probe still marks the room offline and
  manual refresh confirms recovery when the shared location returns.
- Diagnostics retain the most recent transient transport warning, including the
  lane and UTC timestamp, so a message does not need to be captured by screenshot.

## Verification

- Two retry-focused regression tests increase the automated suite to 38 tests.
- The complete Slicer 5.12.3 advanced smoke test confirms that one simulated SMB
  read gap stays online, a removed room is detected as offline, and the room
  recovers after restoration. Rapid components, exact 50/50-voxel leave/rejoin,
  chat, locks, history, snapshots, diagnostics, and a valid MRB backup also pass.

---

# Previous release: Live Segmentation 0.11.0

Version 0.11.0 makes live collaboration practical on very large biological
image volumes without retaining one source-volume-sized mask copy per label.

## Sparse chunked label baselines

- Confirmed label state is stored in independently allocated 64×64×64 `uint8`
  chunks. Empty space consumes no baseline memory, and a chunk is released again
  when all of its voxels are erased.
- A virtual 4096³ geometry with three small components in distant parts of the
  volume allocates only three chunks (less than 0.8 MiB) instead of a theoretical
  64 GiB dense mask per label.
- Ordinary outgoing and incoming edits compare and update only their affected
  regions. Diagnostics report chunk count and allocated baseline bytes.

## Bounded checkpoints and reliable rejoin

- A complete sparse checkpoint is encoded as one backwards-compatible clear
  snapshot followed by small chunk patches. Components at opposite ends of a
  volume no longer force a temporary dense bounding-box allocation during
  history compaction.
- Shared-folder and optional server transports preserve the snapshot/patch group
  semantics, while older operation-only clients can still replay the same
  ordered log.
- Initial synchronization remains armed until the join watermark is reached,
  handling the short SMB visibility window around a newly compacted checkpoint.
- A chunk-coordinate regression that could prevent a checkpoint from applying
  after leave/rejoin is fixed. The lifecycle smoke test now reconstructs the
  expected room mask and requires exact voxel equality instead of checking only
  that one replica exists.

## Measured verification

- A real Slicer 5.12.3 run synchronized a sparse edit on a 512×512×512 source
  volume in 0.28 seconds and restored 27/27 voxels after a clean rejoin.
- Two simultaneous Slicer processes on a 256×256×256 volume exchanged a return
  edit and converged on 35/35 voxels. In the same parallel run, all three rapid
  disconnected components reached both clients (24/24 added voxels).
- All 36 automated tests, Ruff, Python compilation, and the complete advanced
  Slicer smoke test pass, including exact 50/50-voxel rejoin after chat, locks,
  history compaction, conflicts, backup creation, and connection recovery.

---

# Previous release: Live Segmentation 0.10.4

Version 0.10.4 guarantees that rapid consecutive edits of the same label are
queued and delivered without losing a component.

## Lossless rapid-stroke queue

- A label can now have multiple local patches in flight. Every new patch is
  compared with the confirmed server baseline plus all queued and published
  patches that have not yet returned through the ordered room feed.
- Queued operation crops are overlaid sparsely; the plugin does not copy the
  complete reference volume to preserve rapid strokes.
- Sender echoes are tracked across independent push and pull lanes, including
  the race where the receiver observes its own operation before the push worker
  reports completion.

## Reliable Segment Editor selection and settling

- Slicer 5.12 uses `currentSegmentID`; the plugin now uses this API while retaining
  compatibility with versions that expose `selectedSegmentID`. The previous
  silent fallback to the first label could make the trailing verifier inspect the
  wrong label.
- Interactive labels receive sparse trailing state comparisons for two seconds.
  This catches the Slicer event ordering in which a Paint notification arrives
  just before the final voxel write.

## Measured verification

- The original failure was confirmed from the live room: only two yellow patch
  operations existed for three visible local components.
- Two real Slicer 5.12.3 processes on the LSDF share then synchronized three
  rapid, disconnected additions to the same existing label. All 24 of 24 new
  voxels were visible in the second Slicer; the initial edit published in 0.42
  seconds and was received in 0.29 seconds.
- All 32 automated tests, Ruff, Python compilation, and the complete advanced
  Slicer smoke test pass, including chat, locks, history, backups, conflicts,
  connection recovery, clean rejoin, and the early-event rapid-stroke probe.

---

# Previous release: Live Segmentation 0.10.3

Version 0.10.3 removes the remaining manual Segment Editor and SMB maintenance
delays observed for a large edit confined to one microscopy slice.

## Event-driven interactive edits

- The currently selected label now has a constant-time binary-labelmap revision
  probe. Segment Editor effects that update their VTK representation before the
  normal high-level segmentation callback are queued without waiting for that
  delayed callback.
- Shared-folder rooms use filesystem change notifications to wake the receiver
  immediately when the atomic hot feed changes. A 100 ms poll remains as a safe
  fallback for network filesystems that do not provide notifications.
- Receiver updates, including additions and removals, remain cropped to the
  operation bounding box and do not copy the complete microscopy volume.

## Foreground-first shared-folder scheduling

- Hot-feed publication and receiver reads now get a short uncontended SMB window
  before operation archives, retry indexes, label-owner metadata, audit records,
  and conflict analysis start.
- The live UI returns after the atomic edit is published; conflict detection then
  runs independently and remains available through the permanent conflict panel.
- Mixed hot-feed/archive reads now merge correctly during the archive grace
  period, while explicit history compaction waits for pending durable archives.

## Measured verification

- With two real Slicer 5.12.3 processes, the real LSDF share, and a
  479×247×313 volume, a 49,729-voxel single-slice edit was visible on the receiver
  after 0.23 seconds.
- A second patch to the same label published in 0.17 seconds and was fully visible
  on the receiver after 0.27 seconds.
- All 31 automated tests, Ruff, Python compilation, and the complete advanced
  Slicer smoke test pass, including chat, locks, history, backups, conflict
  detection, diagnostics, connection recovery, and clean rejoin.

---

# Previous release: Live Segmentation 0.10.2

Version 0.10.2 removes the serial shared-folder metadata round trips that could
still make a small remote edit appear roughly ten seconds after it was drawn.

## One-read LSDF hot feed

- Each atomic sequence-state update now carries a bounded, self-contained set of
  the newest complete voxel operations. A receiver reads the state once and can
  immediately apply the edit instead of opening a second operation file.
- The hot feed acts as a write-ahead journal. Append-only operation files,
  idempotency indexes, and label-owner metadata are derived in a bounded
  background lane; an inline record is archived synchronously before eviction.
- Unchanged compact label-owner metadata is no longer rewritten for every brush
  stroke. Ephemeral sequence-lock ownership also avoids an unnecessary durable
  flush.
- Operation history, snapshots, reconnection, idempotent retry, global ordering,
  and old shared rooms remain compatible with the existing format.

## Faster Slicer-side detection and display

- Slicer-provided segment IDs restrict a normal Segment Editor event to the one
  label that changed. The compatibility fallback checks all labels only when a
  third-party editor cannot identify its active segment.
- Label-management widgets are no longer rebuilt for representation-only paint
  events.
- The actual segment is refreshed before the optional remote-change highlight is
  generated, improving perceived display latency.

## Measured verification

- The direct new-operation feed was visible to a second client on the real LSDF
  share after 0.047 seconds in the transport probe.
- Two real Slicer 5.12.3 processes communicating through LSDF converged
  bidirectionally on 35 voxels. Sender publication took 0.343 seconds and the
  receiver applied the remote operation after 0.448 seconds.
- 31 automated tests, Ruff, Python compilation, and the complete advanced Slicer
  smoke test pass.

---

# Previous release: Live Segmentation 0.10.1

Version 0.10.1 replaces full-volume synchronization with incremental Slicer
labelmap updates. It directly addresses the 40-second delay observed with large
biological image volumes.

## True region-based live edits

- Local changes are read from each segment's effective internal labelmap extent;
  the plugin no longer exports a temporary reference-sized label volume for every
  Segment Editor event.
- First edits, empty-label announcements, and checkpoints use compact cropped
  snapshots instead of marking every source-volume voxel as changed.
- Incoming patches mutate only their changed voxel bounding box through Slicer's
  native binary-labelmap merge operations. The complete 3D labelmap is no longer
  re-imported for a small brush stroke.
- Incoming changes update the ordered baseline in place and preserve concurrent
  local work only inside the affected crop, avoiding several full-volume copies.
- Remote-change highlights are also cropped, eliminating a second hidden
  full-volume import for every edit.
- Reverting an unauthorized edit to a locked label now updates only the label's
  affected extent instead of re-importing the complete mask.
- Linear parent transforms are handled in segmentation coordinates; uncommon
  non-linear geometries retain the general resampling fallback for correctness.

## Measured verification

- 30 automated transport/server tests pass, including cropped delta, compact
  non-empty snapshot, and zero-voxel empty-label snapshot regressions.
- Two actual Slicer 5.12.3 processes synchronized a 27-voxel edit on a
  512×512×512 reference volume in about 0.55 seconds end-to-end on the test host.
- Bidirectional two-process convergence, Ruff, and Python compilation pass.

---

# Previous release: Live Segmentation 0.10.0

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
