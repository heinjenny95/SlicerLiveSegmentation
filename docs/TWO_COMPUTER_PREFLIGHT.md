# Two-computer connection preflight

`Check connection` automates the checks that otherwise make a local test behave
differently from a real two-person session. It runs in a background thread and
never blocks Slicer's interface.

## Procedure

1. Both participants install the same plugin release and load the same source
   dataset.
2. Both enter their own name, the same room name, and the same connection
   destination.
3. Computer A clicks **Check connection**.
4. Computer B clicks **Check connection**.
5. Computer A clicks **Check connection** once more so both sides confirm peer
   visibility.
6. Join only after failed items are resolved. Warnings explain intentional
   limitations, such as unencrypted Direct LAN transport.

The two-minute preflight beacon contains only display name, plugin/protocol
version, and source-volume signature. It is separate from room presence and does
not create a room, segmentation, audit event, chat entry, or live session.

## What is checked

- endpoint or shared-folder response latency;
- authenticated remote-server access or authenticated Direct LAN relay access;
- shared-folder create/atomic replace/read/list permissions;
- plugin minimum version and collaboration protocol compatibility;
- an existing room's source-volume signature without joining it;
- detection of a second computer using the same room, protocol, and dataset;
- clock skew large enough to make file-based presence asymmetric;
- HTTPS and per-user authentication readiness for internet use.

Preflight success proves that the selected route works at check time. It cannot
guarantee future bandwidth, Wi-Fi stability, cloud availability, or firewall
changes. Use the in-room benchmark for repeated live-feed timing after joining.

