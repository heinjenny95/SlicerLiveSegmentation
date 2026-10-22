# Changelog

All notable changes to Slicer Live Segmentation are recorded here, newest first.
Entries up to 0.15.1 are in [RELEASE_NOTES.md](RELEASE_NOTES.md); that file
remains the long-form description that ships with each release.

Identifiers such as `L-19` refer to findings of the September 2026 code review.

## Unreleased

- Personal label visibility uses its own persistent settings file even when
  Slicer is launched with --disable-settings by the safe desktop/NAS starters.

- Do not rewrite label-creator aggregates for every checkpoint tile in the
  background archive lane either; checkpoint publication already preserves
  original owners. This avoids hundreds of redundant shared-storage round trips.

- Exercise checkpoint contention with an 867-tile batch while a second writer
  joins/paints and a competing checkpoint skips; require stale staging to leave
  no published checkpoint or lost peer edit.

- Remember personal per-label visibility by stable ID across room rejoins and
  restarts. Coalesce settings writes, restore labels arriving during initial
  sync, and never persist fast-preview's temporary global surface hiding.

- Extend integer-source range acceleration to the separately validated Windows
  x64 Slicer 5.12.0 r34621 / VTK 9.6.2 profile (also used by standalone IR).

- Coordinate checkpoint preparation with a separate room lease; durably stage
  tile payloads before the live sequence lock, then recheck the exact room head.
  Skip competing/stale checkpoints, avoid repeated per-tile owner-index reads,
  and retry transient shared-lock contention without an immediate offline popup.

- Rebuild existing native segment-table eye/color/status controls after received
  room-wide reorder operations, retaining each participant's selection and scroll.

- Keep actual own label creation/rename/color actions visible once in the coarse
  Activity timeline, independently of suppressed synchronization ACK messages.

- Allow ordinary SSH requests 30 seconds (checkpoints 120; health probes remain
  10 seconds). Give the isolated forwarding worker a distinct outer deadline.
  Retry timeouts with bounded per-lane backoff and stable operation/message IDs;
  show an amber banner first, escalating only after repeated failures lasting a
  minute, with a five-minute popup cooldown. Retain full error diagnostics and
  immediate handling of authentication/permissions failures.

- Keep the visible Activity timeline coarse: label creation, deletion, property
  changes and reordering, without voxel/sequence/acknowledgement spam. Detailed
  operation diagnostics and the collaborative history remain intact.

- Publish live-room checkpoints without compressing history under the hot
  sequence lock. Production NAS/LAN/SSH clients archive on a separate maintenance
  queue; snapshot validation/counting and history preparation happen before the
  short ordered commit. Recheck the sequence before publishing. Packed counting
  avoids full dense mask expansion; atomic archive publication precedes removal,
  and an archive failure retains complete loose history with an audit record.

- Do not preallocate obsolete native labelmap extents for a known, already
  visible own echo. No-op acknowledgements must not change content timestamps
  and invalidate AI work; real corrective writes still allocate as needed.

- Coalesce overlapping 3D-preview dirty cells instead of scanning repeated ROI
  hints for each packet. Cancel obsolete pooling jobs between bounded slabs and
  revalidate native extent/shape before reusing a pooled cache. Cancellation is
  diagnostic, not a connection error; authoritative labelmaps are unchanged.
  The native large-layer stress probe keeps its window hidden so validation
  does not disturb an ongoing annotation session.

- Preserve the latest local rename/color across delayed metadata echoes,
  including rapid consecutive renames and changes not encoded yet. Metadata
  reconciliation uses operation identities/order rather than display names.

- Compose incoming voxel state against visible pending operations, not only the
  last acknowledged baseline. Delayed own paint echoes no longer resurrect a
  subsequent local erase or needlessly invalidate an in-flight AI request.
  Unencoded edits, deferred room actions and protected voxels remain preserved.

- Apply acknowledged lock states immediately, not only after the next poll.
  An older or incomplete lock poll cannot reopen a newer confirmed freeze;
  explicit newer unlocks and same-revision expiry states remain effective.
  This closes the acknowledgement-to-refresh protection gap found by the native
  IR row-lock integration probe.

- Expose a stable-ID, permission-checked asynchronous lock request for the IR
  label-row controls. Both lock modes use the existing room transport and do
  not change either user's selected material or perform network I/O in the UI.

- Synchronize label ordering across room participants as an ordered metadata-only
  operation. Native drag reordering preserves each editor's selected stable ID,
  label names/colors and all voxel data. Concurrent requests resolve in room
  sequence order; unmentioned new labels are retained and missing labels are
  never recreated. Preserve order through checkpoints, compaction, historical
  restore and rejoin. Validate/persist the optional order list in folder/SSH/LAN
  and HTTP transports and record it in diagnostics, activity and work logs.

- Skip empty native labelmaps that still own a zero-length scalar allocation in
  the asynchronous 3D preview; they no longer abort all populated-label meshes.
  Keep a local preview entry in the editor's 3D button row instead of leaving
  that entry missing. Outside live rooms the original full-resolution button
  is restored. Native regressions include empty and populated materials together.

### SSH checkpoint response deadline

- Allow full-room checkpoint publication up to 120 seconds for its SSH response
  instead of the 10-second small-request deadline. The isolated local worker's
  outer wait includes five seconds for forwarding the inner outcome. Chat,
  presence, health and ordinary edits retain their short independent deadlines;
  closing the session still interrupts pending waits immediately. A timed-out
  request may still finish remotely; late replies remain isolated by request ID.
- Regressions use real encrypted loopback SSH and both client implementations:
  slow checkpoints with concurrent chat/presence, finite expiry, late responses
  and closing during a pending checkpoint.

- Exercise real folder transport and controller replay together under seeded
  interleaved labels, lost acknowledgements/retries, lagged delivery, compaction
  with offline local work, explicit leave/rejoin and a late participant.

- Incrementally repool live 3D previews for proven bounded in-place room writes.
  Recompute entire affected pooling cells (including erasures) from unchanged
  geometry and a continuous revision chain; unknown/native edits, geometry or
  representation replacements retain the full background scan. Cache is local,
  bounded and never changes labels; diagnostics report scanned voxels and mode.

### Activity describes effects, not maintenance traffic

- Do not call unchanged state snapshots or metadata echoes a manual label edit.
  Accumulate real baseline changes across rendering tiles; confirmations,
  automatic checkpoints and state synchronization have distinct descriptions.
  Raw ordered operation/study journals remain unchanged for auditability.

### Asynchronous approximate live 3D preview

- Add a clearly labeled local 3D preview with bounded occupancy pooling and
  private worker-owned mesh conversion. Authoritative labelmaps and label
  exports stay at full resolution. Revisions reject stale worker results;
  scene close, leave, visibility, colors and parent transforms are respected.
- While in a live room, replace native editors' synchronous Show 3D entry with
  the global preview action. Derived full-resolution surfaces are not retained
  during preview, avoiding automatic reconversion during native ROI writes.
  Preview models are excluded from scene saves and never synchronized.

### Persistent diagnostic history

- Add process-owned asynchronous rotating JSONL logs for operation receipt/
  application, transport durations, checkpoint preparation/commit, slow GUI
  callbacks and errors. Records carry UTC and monotonic times, IDs and counts,
  not masks, raw credentials or chat content. Log-write failures are exposed.
- Keep connection errors readable until acknowledged, independently of transport
  recovery. A global Errors button exposes recent errors and the local log folder.
  Recovery no longer automatically closes the error box or clears its text.

### Consistent room checkpoints

- Assemble complete system-checkpoint groups privately across feed pages before
  touching visible materials. Compare sparse baseline chunks cooperatively and
  skip unchanged masks entirely, avoiding repeated clear/repaint and native
  labelmap work for routine history compaction. Keep pending local edits visible.
- Changed masks are staged in bounded GUI slices, revision-checked and committed
  together; malformed/incomplete groups retain the old replica and report an
  error instead of displaying a partial checkpoint. Existing wire format stays
  compatible. Tests check intermediate visibility, not just final convergence.

### Fast native integer source-range preparation

- Avoid VTK 9.6.2's per-point image-range scan when opening the standard editor
  with ordinary integer, single-component images without cell scalars/ghosts.
  Prime the same image-owned cache using VTK's exported optimized base-class
  implementation; no pixel copies/substitutions, geometry changes, DLL patches,
  private-field writes or background MRML/VTK access.
- Explicit source binding, manual module/volume selection and subsequent image
  revisions are guarded before native editor callbacks. Unchanged source tokens
  do not rescan; scene-reset parameter nodes are re-observed; cleanup removes only
  owned observers. The earlier inactive-Threshold optimization remains separate.
- The protected C++ ABI bridge is restricted to Windows x64, Slicer 5.12.3 r34627,
  VTK 9.6.2, an application-local library and a mandatory range/revision self-test.
  Unknown profiles and unsupported image layouts retain native behavior, with a
  one-time diagnostic for large sources. This is not a universal VTK ABI promise.

### Lazy standard-editor Threshold preparation

- Do not align/copy the complete source merely to notify inactive built-in
  Threshold when the normal Segment Editor opens. A reversible accessor adapter
  affects only that editor's built-in Threshold instance. Activating Threshold
  uses the native aligned image, parameters, preview and application unchanged.
- Keep original image buffers, geometry, nodes and intensity masks untouched.
  Native probes check a 1-GiB volume, optional real BigTIFF input, repeated module
  navigation, exact signed-intensity Threshold results and cleanup. This avoids
  one full-volume pass, but Slicer's core source-range scan remains synchronous;
  this change does not promise a freeze-free first open on arbitrary large data.

### Avoid hidden Segment Editor preprocessing

- Defer source/segmentation binding until the standard editor is actually used.
  Joining from Live Segmentation or nnInteractive must not initialize Threshold's
  full-volume source preprocessing in a hidden editor. Manual module navigation
  and the explicit editor button still attach the authoritative room replica.
- Skip redundant bindings when both target nodes already match. This does not
  bypass Slicer's own first-use preprocessing when the user opens that editor.

### Bounded ROI geometry in live synchronization

- Read shifted ROI origins and affine label geometry in the requested reference
  region instead of exporting, copying and scanning complete reference volumes
  on the GUI thread. Capture only the label footprint and yield between slabs.
- Keep packed label values separate, clip transformed footprints to the source,
  verify geometry/revisions during capture and reject unsupported relative
  non-linear transforms explicitly instead of silently starting a full export.
- Add ROI-origin and affine sampling regressions plus a native large-data probe.

### Active local work time, including visual review

- Add compact global Work/Review/Pause/Resume controls. Purposeful local view
  and editor input resumes estimated work; chat, bare pointer motion, remote
  paint and optional IR background heartbeats do not. Default auto-pause is
  60 seconds (configurable 15–900); explicit Review counts silent inspection.
- Measure monotonic local intervals, not network arrival delays. Separate work,
  confirmed review, manual pause, idle, away/other context, communication,
  detected technical waits and unobserved GUI gaps. Inspect an already-created
  nnInteractive IR worker flag without loading, initializing or retargeting it.
  Concurrent review/editing is not deducted as AI waiting.
- Transfer validated, bounded, idempotent samples on an independent lane to the
  permanent TXT/journals, with a local timing backup and final sample-count
  markers. Missing coverage and uninstrumented older sessions are explicit.
  Timing/storage failures never disconnect the voxel collaboration session.

### Study timing and permanent activity log

- Automatically maintain `work-log.txt` in the room data directory, with UTC
  joins/leaves and acknowledged label, lock, chat and review actions per person.
  Count transport retries once; keep journals across restarts and compaction.
- Separate per-person attendance, summed person-seconds, parallel team room-time
  and calendar span. Never present attendance as exact active segmentation time;
  flag unknown ends, damaged records and observation gaps longer than 60 seconds.
- Flush small append-only journals on an independent background lane (5-second
  target), sample heartbeats at 15 seconds, and refresh the atomic TXT report at
  30 seconds and on join/leave. Logging does not hold the voxel sequence lock.
  Failed TXT replacement keeps both the previous TXT and recoverable journals.
- Include the logger in Slicer, encrypted SSH helpers and the optional HTTP
  server. Server activity is queued only after its database transaction commits;
  the authenticated room-member TXT download excludes chat bodies and secrets.

### Local workflow

- Add an optional nnInteractive IR toolbar button and Ctrl+Alt+I shortcut next
  to Live Segmentation. Both navigation actions stay available across modules;
  switching does not reconnect or initialize inference. The IR action is hidden
  if the separately installed module is absent, preserving the standalone
  collaboration extension. Include its icon in the module resources.

- Add a main-window Live Segmentation button, Ctrl+Alt+L return action, and
  Chat action that remain available in other modules. Navigation does not
  reconnect, reload data or alter selected labels. Setup is idempotent and
  cleanup removes the toolbar and shortcut; a native regression checks it.

- Give the module a consistent 12-pixel content inset and left-align form
  captions, keeping the compact side-by-side fields while avoiding text
  pressed against the panel edge. Native width/layout QA checks the inset.

- Keep successful SSH/NAS locations in a separate local preference file, so
  the recent-location dropdown survives safe/test starters using disabled
  Slicer settings. Startup remains disconnected with an empty active path.
  Migrate old history, strip URL credentials/query parameters, and clear both
  stores when requested. A two-process native regression covers restart recall.
  Use the generic local user-data root rather than Slicer's application-specific
  Slicer-tmp directory, so normal and safe starters see the same history.

### Connection

- Allow 30 seconds (instead of 15) to join shared-folder, LAN and HTTPS rooms.
  Joining remains asynchronous and cancellable; SSH retains its 45-second
  setup allowance. This does not change the established-session watchdog.

### Chat

- Publish a bounded, immutable, sequence-addressed chat fast feed. Receivers
  open the next message directly, without a preliminary existence check,
  hot-cache refresh or directory scan. Legacy permanent chat remains readable
  and retries remain idempotent. This reduces cache-induced NAS delays but
  does not guarantee a latency bound on an unresponsive network share.

### Label protection

- Add owner-editable locks and universal freezes, including the owner's own
  edits, deletion and metadata changes. Frozen sibling voxels also resist
  cross-label overwrite. Folder/SSH/LAN and HTTP use the same semantics; the
  HTTP database migrates old locks to owner-editable mode. A pending local
  freeze already guards the owner's edits. Unknown modes fail closed; an old
  HTTP server cannot silently claim to support a universal freeze. All peers
  must use this updated build (mixed folder writers are not supported).

- Track the lock holder separately from label ownership: an owner-editable
  lock grants editing to the person who set it, even when a room admin locks
  somebody else's label. Explicit ownership transfer hands over this edit
  privilege; old locks without a holder retain their original owner semantics.

- Extend the native Slicer collaboration smoke test with both lock modes and
  restoration of an attempted owner edit on a universally frozen label.
  Keep mode and holder when ingesting polled locks into the controller, so
  the native editor does not silently treat a received freeze as owner-editable.

### Interface

- Group the everyday workflow into Data, Room and Label protection. Keep the
  persistent chat button directly accessible; collapse follow-view, permissions,
  inline comments, history, backups and diagnostics until needed. Move long
  setup guidance into tooltips, hide empty preflight/SSH-status areas and put
  path-history clearing and the renamed Connection speed test in diagnostics.

- Keep the connection speed test result visible instead of immediately
  overwriting it with the previous diagnostics report (L-44). A separate
  readable summary shows median health/feed response times, errors and the
  distinction from end-to-end edit latency; detailed statistics stay available.

### Presence

- Explicitly leaving a shared-folder room now publishes a session-specific
  departure before optional audit work. The next presence poll removes that
  collaborator immediately instead of retaining them for the network grace
  period. A heartbeat already in flight cannot resurrect the closed session;
  an old departure cannot disconnect a newer session of the same user.
  The departure report is also carried over LAN and SSH. Missing/partial
  heartbeats without a confirmed departure still use the existing network
  grace period. Rejoining starts a fresh presence session.

### Security

- HTTPS server: a valid token alone no longer gives access to every room
  (L-03). Room endpoints now require that the user has joined the room, which
  needs the signature of its source volume; everything else gets `403`. The
  connection check no longer returns other participants' volume signatures,
  which had let any token holder join any room a colleague was checking. It
  reports only whether the source volumes match; 0.15.1 clients keep working.
  The database gains a `live_room_members` table automatically. After a server
  upgrade, participants of existing rooms become members on their next join.
  See `docs/SECURITY.md` for what this does not cover.

- HTTPS server: one small request can no longer exhaust memory or stall the
  server for everyone (L-04). The upload limit only applied to the compressed
  payload, so 0.7 MB could inflate to about 1 GB; decompression is now bounded
  by the size the operation's bounds imply, with a cap on the bounds. The
  conflict check built a Python set with one tuple per voxel of each bounding
  box, inside the database write transaction and on the event loop, so a
  700-byte request over a 128-cubed box blocked every request for seconds. It
  now compares bit rows, looks at no more than the 64 most recent concurrent
  operations, and the endpoint runs in the thread pool. A payload that does
  not match its bounds is answered with `422` instead of being stored (or
  causing a `500`), so it cannot stop the room's readers later.

### Fixed

- Project backup restore now downloads to local staging and verifies the stored
  checksum and MRB archive in a background worker (L-15). A failed or stale
  preparation leaves the room/scene untouched. Opening requires a fresh explicit
  confirmation with no unsettled local edits, imports offline without clearing
  other scene data, and never rolls back the shared room. Legacy backups without
  stored checksums require manual inspection/import. Local MRML import still runs
  on Slicer's GUI thread and may pause for a large scene.

- Restoring a room revision now also queues deletion of labels that did not
  exist in it, including an entirely empty revision (L-17), and restores names
  and colors. It refuses to queue anything if local work or the applied room
  sequence changed while fetching history; existing local edits remain intact.
  This is a series of ordinary protected operations, not an atomic room-wide
  rollback: locks may refuse individual operations and subsequent concurrent
  edits still participate in the global order.

- Historical reconstruction now uses the same ordered cross-label ownership
  and immutable protection decisions as live replay (L-17). Deleted/recreated
  labels also retain the correct new metadata. It keeps sparse 64-cubed
  baselines and emits a snapshot followed by bounded patches for widely spread
  components, not a full-volume allocation per label and edit. Restore, undo,
  comparison and conflict-choice consumers accept all chunks instead of only
  the first one; the comparison display and union/undo still have dense GUI
  work and are not claimed to be fully memory-bounded. A guarded 2000-cubed
  sparse test refuses any attempted full-volume allocation.

- Cross-label voxel claims now respect foreign label locks (L-17). The folder
  transport captures the protected IDs under the room sequence lock; HTTP
  captures them in the write transaction and migrates a JSON field in its
  database. Receivers use this immutable decision and the ordered baseline,
  not their potentially stale UI lock state: protected voxels are clipped from
  the incoming stroke while the rest is applied. The sender's optimistic
  overwrite is corrected too. Folder lock/approval/ownership changes serialize
  with publication and invalidate a small derivative lock index before writing;
  warm brush strokes do not scan all label-lock files. Approval clears stale
  expiry. All participants and the host/server must run the corrected code;
  older readers cannot enforce this new operation field. No release or protocol
  version change is made by this development commit.

- Project-backup network copying, hashing, metadata and retention run in a
  dedicated background worker (L-15), not Slicer's GUI thread. The upload is
  checked against the local source checksum before publication. Completion is
  reported only after verification; stale completions cannot update a newly
  joined room. Slicer's local `saveScene` still runs synchronously on its GUI
  thread and may pause a large scene. Backup restore is not changed here.

- Shared-folder/SSH and HTTP publishers now reject edits whose base precedes
  the label's last deletion (L-17). The deletion barrier survives restores,
  compaction, process restarts and a missing polling cache. An informed restore
  remains possible. Clients handle the refusal without reconnecting, awaiting
  an impossible echo, or restoring a refused obsolete deletion. All folder
  publishers must use the updated code; existing wrongly accepted historical
  operations are not rewritten or claimed to be repaired retroactively.

- A locally pending label deletion no longer bypasses ordered voxel ownership
  (L-17). Incoming edits to that hidden label still clear their claims from
  siblings before the deletion takes effect, matching other peers and a fresh
  join. Retained baselines also follow incoming claims while their labels are
  hidden, so a refused deletion cannot restore outdated overlap.

- Shared-folder/SSH retries remain idempotent after history compaction (L-16).
  A lost acknowledgement or replayed crash-journal operation now returns its
  original sequence even after the loose payload was archived. Compaction
  persists missing retry receipts before removing loose files, including those
  from a different publisher whose background writer has not finished.

- Leaving a room no longer clears unacknowledged edits from the optional crash
  journal (L-08). Unsettled local work, including edits still being captured or
  encoded, keeps a hidden, detached OFFLINE RECOVERY segmentation in the scene.
  The standard editor treats that copy as read-only; new joins do not erase it.
  An explicit warning and Save action make clear that this is not a disk backup:
  save it before closing Slicer. Clean, settled replicas are still removed.

- Native Segment Editor undo/redo history is cleared and disabled during live
  collaboration (L-05), so restoring an old whole-segmentation state cannot
  erase colleagues' labels. Use the collaborative Undo action instead. The
  previous native undo-history limit is restored on leaving the room. Verified
  with the real Slicer undo stack, including offline undo after leaving.

- SSH sessions no longer close after an idle read timeout (L-14). The reader
  retains partial UTF-8/JSON frames across pauses, including in the isolated
  SSH worker, while per-request deadlines, stream limits, and explicit close
  remain in force. The incremental reader scans each received byte only once
  for frame boundaries, avoiding repeated full-prefix scans on large edits.

- Collaborative undo, revision restore, conflict-resolution snapshots, and crash
  recovery no longer generate an inverse edit while waiting for their server
  echo (L-06). Verification distinguishes already-visible local strokes from
  queued room actions; drawing can continue while those actions are pending.

- Joining or rejoining a compacted Shared Folder/SSH/Direct LAN room no longer
  stays connected with an empty segmentation. Small initial-sync pages were
  bounded from the old cursor instead of the first available system snapshot;
  when archived history was larger than the page, every response stayed empty.
  The page limit now counts operations from that snapshot, without weakening
  the rule against skipping gaps to ordinary per-label replacements.

- The Slicer lifecycle smoke test now fails when rejoining restores fewer or
  more voxels than expected. Its failure branch was accidentally nested after
  an unconditional raise in the deleted-label check and could never run;
  an `ok=true` result could therefore include zero restored voxels. Regression
  tests execute the actual guard with missing, extra, and correctly restored
  voxels, as well as a deleted label that reappears.

- An edit the room refused because the label had just been locked by someone
  else was still treated as sent and waited for an echo that never came
  (L-07). For the rest of the session, painting the same voxels again was never
  published, automatic snapshots were blocked, and the refused edit stayed in
  the recovery journal. A refused deletion made the label vanish for that
  participant only, without any message. Refused operations are now dropped
  from the pending state, and a refused deletion restores the label from the
  room state with an Activity entry.

- One damaged operation no longer stops a room for every participant, forever
  (L-09). An operation with an undecodable payload, bounds outside the volume,
  or a file truncated by a NAS power loss made every pull fail at its sequence
  number and was reported as a connection problem. Such an operation is now
  refused before it gets a sequence number in the Shared Folder, Direct LAN,
  and SSH folder transports. When one is met while reading (written by an
  older client or damaged on disk), it is skipped with an entry in the
  Activity log; because every participant skips it, the room stays
  consistent. A file that cannot be read is only given up on after 30 seconds,
  so a file that is still being written is not skipped by mistake.
- Decoding an operation never inflates more data than its bounds allow, so a
  few kilobytes can no longer expand to gigabytes inside Slicer or in the
  Direct LAN host's Slicer process (L-09).
- A room snapshot could erase a collaborator's latest edits for every
  participant, including the author (L-02). The snapshot was built from the
  publisher's last known sequence but appended, with replace semantics, after
  everything that had arrived in the meantime, and compaction then archived
  those operations. The publisher now passes the sequence the snapshot was
  built from; the shared folder checks it under the sequence lock and skips
  the snapshot when the room has moved on (the HTTPS server has no atomic
  check yet, so the client checks immediately before publishing). A skipped
  automatic snapshot is simply retried later, a requested one is rebuilt.
- A participant that was behind the compaction point never learned that a
  label had been deleted and kept a ghost label (L-02). Compaction now repeats
  such deletions at the start of the snapshot; they are not shown as new
  activity.
- A reader that briefly could not see the next operation (SMB directory-cache
  lag) resumed at any later operation of kind `snapshot`, which includes an
  ordinary replace of one label, and skipped other labels' operations for good
  (L-02). It now resumes only at the first operation of a room snapshot.
- Every idle participant published its own full snapshot shortly after
  another one had done so, because only one's own snapshots were counted
  (L-02). Snapshots received from the room now count as well.
- Shared/Network Folder: the sequence lock could be held by two computers at
  once (L-01), which produced two operations with the same sequence number,
  silently dropped one of them for readers, and made the room refuse every new
  participant. Three causes are fixed:
  - Two waiting clients that judged the same abandoned lock stale could both
    break it; the second one renamed the first one's fresh lock away. A lock is
    now inspected again after the rename and put back if it is not the
    abandoned lock that was observed.
  - A holder that needed longer than the 60-second stale limit (a snapshot on a
    slow NAS) lost its lock while it was still working. The holder now
    refreshes its lock while it holds it.
  - Lock age compared the local clock with the file server's timestamps, so a
    PC running a minute ahead of the NAS broke every foreign lock at once. Age
    is now measured against the file server's own clock.
  A holder also verifies that it still owns the lock immediately before it
  writes an operation and refuses to write otherwise. The on-disk format is
  unchanged and 0.15.1 clients can share a room with this version; they still
  have the old behavior themselves. Mutual exclusion on SMB remains
  best-effort, and the duplicate-sequence check at join stays in place.
- The module self-test no longer fails on every run: it asserted the literal
  version `0.14.6` in 0.14.7, 0.15.0, and 0.15.1 (L-19). A new test compares
  `VERSION`, `version.py`, `server/app/main.py`, `CITATION.cff`, and
  `CMakeLists.txt`, and `scripts/build_release.py` refuses to build when they
  disagree or, on a tag build, when the tag is not `v<VERSION>`.
- The release manifest no longer reports `ruff: passed`, `automated_tests: 124`,
  or Slicer smoke-test results that the build script never ran (L-19). It now
  separates `measured_by_this_script` from `maintainer_asserted_validation`.
- `scripts/open-live-segmentation.ps1` no longer contains a maintainer's
  personal Windows profile path, which exposed an account name and only worked
  on one computer (L-20). It finds the newest per-user Slicer like the
  installer does and accepts `-SlicerPath`. A test now rejects personal
  profile paths in tracked files. The path remains in the Git history and in
  the published 0.15.1 source archive.
