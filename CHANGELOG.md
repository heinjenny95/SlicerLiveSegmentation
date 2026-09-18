# Changelog

All notable changes to Slicer Live Segmentation are recorded here, newest first.
Entries up to 0.15.1 are in [RELEASE_NOTES.md](RELEASE_NOTES.md); that file
remains the long-form description that ships with each release.

Identifiers such as `L-19` refer to findings of the September 2026 code review.

## Unreleased

### Fixed

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
