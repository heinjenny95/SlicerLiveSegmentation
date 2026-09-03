# Manual two-user acceptance test

## Safe recent shared-folder dropdown

1. In a normal Slicer profile, join a room through a reachable shared folder
   and then leave the room.
2. Restart Slicer normally. Verify the active **Shared folder** field is empty
   and startup does not wait for or contact the previous location.
3. Open the dropdown and verify the successfully used path is listed. Select it
   and verify it is copied into the active field without starting a connection.
4. Click **Join live room** and verify only then does the extension access the
   location. Reusing a path must move it to the top of the list.
5. Attempt a failed or timed-out connection and verify that location is not
   added to the dropdown.
6. Verify at most eight unique entries are kept and **Clear list** removes them.
7. Launch **Live Segmentation Safe Start** and verify its isolated settings do
   not expose or probe the normal profile's recent-folder history.

## Slow first network access

1. Disconnect an existing Windows connection to the test share or use a UNC
   path that has not yet been opened in the current Windows session.
2. Start joining and verify that the interface remains responsive,
   **Cancel connection** remains available, and the status line shows elapsed
   seconds.
3. A response after more than four but less than 15 seconds must still open the
   room. Only 15 seconds without a response may cancel the local attempt.

## Temporary active-session stall

1. While joined, delay or suspend responses from the test share for more than
   ten but less than 30 seconds.
2. Verify that the status turns orange and reports a slow shared folder while
   the shared segmentation and current room remain loaded.
3. Restore responses and verify that live status returns automatically.
4. Repeat with more than 30 seconds of complete silence and verify that the
   session is reset locally without freezing Slicer.

## Safe Start recovery

1. Install the package and verify both **Live Segmentation** and **Live
   Segmentation Safe Start** desktop shortcuts exist.
2. Put an intentionally unusable value into an older Live Segmentation
   connection setting or use a disposable Slicer profile containing stale
   state.
3. Launch **Live Segmentation Safe Start** and verify Slicer reaches the module
   without opening or probing the stale network location.
4. Verify the ordinary Slicer profile and separately installed extensions were
   not deleted or modified. The shortcut arguments must contain
   `--disable-settings --ignore-slicerrc`.

## Direct LAN, invitation, and fallback

1. On participant A choose **Direct LAN + shared-folder fallback**, select a
   writable shared folder, enable local hosting, and click **Start LAN host**.
2. Export a `.livesegroom` invitation and open it by double-click on participant
   B. Verify Slicer opens Live Segmentation and fills room, relay URL, session
   code, and fallback folder without automatically joining.
3. Load the matching source volume on B and join. Paint in both directions and
   verify the status says direct LAN and edits remain sub-second on a suitable
   local network.
4. Stop the LAN host while both clients remain able to access the shared folder.
   Verify subsequent requests use the fallback and the activity/metrics counters
   record it without freezing Slicer.
5. Repeat with an incorrect session code and verify the join is rejected. Never
   expose the unencrypted relay outside a trusted LAN or approved VPN.

## Narrow module panel

1. Open **Live Segmentation** and drag the left module panel toward its minimum
   width. Confirm that it can shrink to approximately 288 pixels rather than
   occupying a large fraction of the display.
2. Expand **Room chat**, **Live activity, version history, and conflicts**, and
   **Diagnostics, templates, and invitations**. Confirm that action controls are
   arranged over multiple rows, remain clickable, and do not force the panel
   wider.
3. Run `scripts/slicer_ui_width_smoke_test.py` with the source module path and
   confirm `root_minimum_hint` is at most 360 pixels.

## Version 0.12 collaboration enhancements

1. Send an ordinary message and a spatial comment. Verify both appear
   permanently, the comment jumps to its stored label/view, and resolution is
   visible to both clients after reconnecting.
2. Keep Segment Editor open and verify remote edits appear in the activity dock
   and as temporary colored voxel highlights.
3. Run **Benchmark** and verify health/live-feed minimum, median, p95 and maximum
   are displayed without creating an edit. Confirm live timing lists publish,
   receive, apply/render, chat, and edit roundtrip separately.
4. Make an edit, let the other user edit a different voxel in the same label,
   and run **Undo my last shared edit**. Verify the first edit is inverted as a
   new history sequence while the later collaborator voxel remains.
5. Select an earlier history row and **Compare with current**. Verify a separate
   display-only segmentation shows green Added and red Removed voxels and does
   not enter the shared history.
6. Set labels to ready-for-review, changes-requested, and approved; verify the
   review queue order, notes, and approved-label lock.
7. Run segmentation quality checks on synthetic empty, disconnected, tiny, and
   overlapping labels. Verify every issue is reported and no voxel changes.
8. Disconnect before a queued edit is acknowledged, close Slicer, rejoin the
   exact same context, and recover it. Verify replay is idempotent. An explicit
   **Leave live room** must clear the journal.
9. Export research metrics and verify the JSON contains aggregate timings and
   counts but no user, room, URL, path, image, or segmentation payload.

## Preparation

1. Create an empty folder in a shared location writable by both test users.
2. Open two Slicer instances and load the same non-sensitive source volume.
3. Open `Live Segmentation` and select the source volume in both instances. Do
   not create or select a Segmentation node.
4. Select `Shared/network folder`, use the same shared folder and room name, and
   enter different user names.
5. Join the room in both instances.
6. Verify that each instance automatically shows a room-managed node under
   `Shared segmentation`.

## Standard Segment Editor

1. In instance A click `Open Segment Editor`.
2. Verify that Segment Editor displays the selected source volume and the
   automatically selected room Segmentation node.
3. Add a segment and paint a small area.
4. Verify that the segment and voxel edit appear in instance B without manual
   save or pull.
5. In B add a non-overlapping area to the same segment and verify both areas in A.
6. Erase part of the segment in B and verify the removal in A.

## Rapid same-label strokes

1. Select a single label in instance A and paint three disconnected components
   in quick succession without waiting for the peer between strokes.
2. Verify instance B receives all three components, in order, without pressing
   **Sync now**.
3. Repeat while the first component is still being transmitted over a slower
   network share. Verify no component is lost or assigned to a different label.
4. Select a different label and repeat. Confirm the Segment Editor selection,
   live history, and the received label identity all agree.

## Large-volume and sparse-component scaling

1. Repeat the two-user test with a representative high-resolution biological
   volume (at least 512×512×512 voxels when hardware permits).
2. Create several small labels and paint sparse components at widely separated
   locations. Confirm normal brush edits remain region-based and do not allocate
   one dense source-volume mask per label.
3. Run room diagnostics and record `baseline_storage`: mode must be
   `sparse-64-cubed-chunks`, allocated bytes must track occupied chunks, and
   deleting a component must allow its empty chunk to be released.
4. Create a checkpoint, leave, and rejoin. Verify every component is restored
   exactly once and the voxel count equals the state reconstructed directly from
   the room log.
5. Confirm the checkpoint group begins with a snapshot and uses small patch
   operations for further occupied chunks instead of one bounding box spanning
   distant components.

## Separately installed tools

1. Keep the live room connected.
2. Open any separately installed segmentation tool and configure it to edit the
   same Segmentation node.
3. Apply one edit and verify that it appears in the other Slicer instance.

## Parallel shared-folder load

1. Create at least four labels and distribute them between the two users.
2. Paint different labels simultaneously while sending chat messages and
   changing a lock on another label.
3. Verify edits inside each label retain their local order while edits to
   different labels, chat, presence, and locks continue without waiting for one
   another.
4. Join a third disposable client after at least 25 operations. Verify the
   operation files are loaded in sequence order and the reconstructed masks are
   identical to both active clients.
5. Open history, conflicts, reviews, roles, and snapshots together with
   **Refresh advanced data**. Verify the independent datasets load without
   pausing live edits or chat.

## Conflicts and protection

1. Edit two non-overlapping areas simultaneously and verify both remain.
2. Assign different values to the same voxel and verify both clients converge on
   the later ordered operation.
3. Attempt to join the room with a different source volume and verify rejection.
4. Lock A's selected label and verify B cannot keep or publish an edit to it.
5. Unlock the label and verify B can edit it again.
6. Use **Label to manage** to switch labels. Confirm the lock status and Segment
   Editor selection follow the visible choice.

## Connection, chat, backups, and lifecycle

1. Interrupt shared-folder or server access and verify the green online state is
   replaced by a red connection error and a visible dialog.
2. Restore access and click **Sync now**. Verify the room remains joined and
   returns to green without using Slicer's developer **Reload** action.
3. Send a chat message and verify it appears immediately for its sender and in
   the other instance without pressing **Sync now**. Leave and rejoin and verify
   the full chat remains.
4. Verify both participants appear online within two seconds in both instances.
5. While both users paint, send chat and lock/unlock a label. Verify none of
   these actions disconnects either participant or waits for the voxel sync.
6. In shared-folder mode change backup enablement, interval, and retention while
   connected. Use **Back up now** and verify a versioned `.mrb` appears under the
   room's `backups` directory while older bundles remain.
7. Leave the room and verify the shared-segmentation selector, Segment Editor
   label table, and room node are cleared.
8. Rejoin repeatedly and verify exactly one room segmentation is present.

## Advanced collaboration

Before joining, run the publication preflight on computer A, then computer B,
then computer A again. Confirm both computers report the other participant and
the same plugin/protocol generation and source dataset. Repeat once with a
different source volume and confirm joining is prevented. For Remote HTTPS
server mode, verify a public `https://` endpoint and individual token pass;
verify a non-loopback `http://` endpoint is rejected unless the session-only
local-test override is checked.

1. Move Alice's crosshair and slice views. On Thomas, select Alice and test
   **Jump to user** and **Follow view**. Confirm no voxel data changes.
2. Paint on Alice and confirm Thomas sees a short-lived colored outline exactly
   over the changed area.
3. Send an anchored chat message and verify **Jump to message location** restores
   the saved view after reconnecting.
4. Create overlapping edits from the same starting revision. Verify a conflict
   reports label and voxel count; test latest, mine, other, and union resolution
   on disposable synthetic data.
5. Set Thomas to viewer and confirm edits are reverted and rejected. Set reviewer
   and confirm approval locks the label.
6. Request access to a locked label, transfer ownership, use a temporary lock,
   and confirm an administrator can resolve an abandoned lock.
7. Publish a material template with names, colors, and terminology. Join from a
   clean scene and verify missing labels appear without replacing voxel data.
8. Export and import a `.livesegroom` invitation. Verify it contains no API key,
   treats a direct-LAN session code as sensitive, and rejects a different source
   volume. Legacy `.liveseg` import remains compatible.
9. Create a checkpoint and confirm older loose operations move into a ZIP
   archive. A fresh client must reconstruct the same segmentation, and the
   timeline must restore a pre-checkpoint revision.
10. Run diagnostics online and offline. Verify the exported report omits users,
    room names, server URLs, and shared-folder paths.
11. Pin, verify, and restore a disposable MRB backup. Confirm retention removes
    only the oldest unpinned backups.

## Automated Slicer probe

`scripts/slicer_live_segmentation_smoke_test.py` runs in two simultaneous Slicer
processes. The consumer starts without a Segmentation node and must receive the
room node plus the producer's 27 voxels. It then adds 8 voxels to the same
segment; both processes must converge on 35 voxels without any artificial
generic `Modified()` call. With `LIVE_SEGMENTATION_SMOKE_RAPID_COMPONENTS=1`,
the producer also adds three disconnected components to the same existing label
at 120 ms intervals. The third notification intentionally precedes its voxel
write; the peer must still receive all 24 new voxels.

The lifecycle probe reconstructs the authoritative room mask before leaving and
requires exact voxel equality after rejoin. The release validation also runs a
512×512×512 single-client lifecycle case and a simultaneous 256×256×256
bidirectional rapid-stroke case.
