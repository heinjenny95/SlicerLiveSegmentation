# Changelog

All notable changes to Slicer Live Segmentation are recorded here, newest first.
Entries up to 0.15.1 are in [RELEASE_NOTES.md](RELEASE_NOTES.md); that file
remains the long-form description that ships with each release.

Identifiers such as `L-19` refer to findings of the September 2026 code review.

## Unreleased

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
