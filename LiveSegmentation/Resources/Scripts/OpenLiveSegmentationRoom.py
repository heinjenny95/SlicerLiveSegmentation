"""Open a .livesegroom invitation after Slicer has initialized the module."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import qt
import slicer

invitation_path = Path(sys.argv[-1]).resolve() if len(sys.argv) > 1 else None
attempt = 0


def open_invitation():
    global attempt
    attempt += 1
    try:
        if invitation_path is None or invitation_path.suffix.lower() not in {
            ".livesegroom",
            ".liveseg",
        }:
            raise ValueError("No Live Segmentation invitation was supplied")
        slicer.util.selectModule("LiveSegmentation")
        module = slicer.app.moduleManager().module("LiveSegmentation")
        if module is None:
            raise RuntimeError("Live Segmentation is not registered yet")
        controller = module.widgetRepresentation().self().live_collaboration
        controller.import_invitation_path(str(invitation_path))
        slicer.util.showStatusMessage(
            "Live Segmentation invitation loaded — select the matching volume and join",
            6000,
        )
        probe_output = os.environ.get("LIVE_SEGMENTATION_INVITATION_PROBE_OUTPUT")
        if probe_output:
            Path(probe_output).write_text(
                json.dumps(
                    {
                        "ok": True,
                        "room": controller._text(controller.room_edit),
                        "transport": controller._transport_mode(),
                        "lan_url": controller._text(controller.lan_url_edit),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            slicer.app.exit(0)
    except Exception as exc:
        if attempt < 20:
            qt.QTimer.singleShot(250, open_invitation)
            return
        probe_output = os.environ.get("LIVE_SEGMENTATION_INVITATION_PROBE_OUTPUT")
        if probe_output:
            Path(probe_output).write_text(
                json.dumps({"ok": False, "error": str(exc)}, indent=2),
                encoding="utf-8",
            )
            slicer.app.exit(1)
            return
        slicer.util.errorDisplay(f"Could not open Live Segmentation invitation: {exc}")


qt.QTimer.singleShot(0, open_invitation)
