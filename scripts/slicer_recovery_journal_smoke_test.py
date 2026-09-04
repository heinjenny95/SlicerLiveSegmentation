from __future__ import annotations

import json
import os
import tempfile
import traceback
from pathlib import Path

import slicer
from LiveSegmentationLib.features import PendingOperationJournal


def run_probe():
    real_replace = os.replace
    try:
        with tempfile.TemporaryDirectory(prefix="live-seg-recovery-") as root:
            journal = PendingOperationJournal(Path(root) / "pending.json")
            journal._REPLACE_RETRY_DELAYS = (0.0, 0.0, 0.0)
            attempts = []

            def transient_lock(source, destination):
                attempts.append((str(source), str(destination)))
                if len(attempts) < 3:
                    raise PermissionError(5, "simulated Windows file lock", str(destination))
                real_replace(source, destination)

            os.replace = transient_lock
            operation = {"client_operation_id": "slicer-retry-op"}
            written = journal.write({"room_name": "slicer-smoke"}, [operation])
            recovered = journal.read({"room_name": "slicer-smoke"})
            if not written or recovered != [operation] or len(attempts) != 3:
                raise RuntimeError("Recovery journal did not survive transient file lock")

            os.replace = lambda source, destination: (_ for _ in ()).throw(
                PermissionError(5, "persistent Windows file lock", str(destination))
            )
            journal._REPLACE_RETRY_DELAYS = (0.0, 0.0)
            nonfatal = journal.write(
                {"room_name": "slicer-smoke"},
                [{"client_operation_id": "newer-op"}],
            )
            if nonfatal is not False:
                raise RuntimeError("Persistent journal lock was not downgraded")
            if journal.read({"room_name": "slicer-smoke"}) != [operation]:
                raise RuntimeError("Last valid journal was not preserved")
            if list(Path(root).glob(".*.tmp")):
                raise RuntimeError("Temporary recovery files were not cleaned up")
            return {
                "ok": True,
                "plugin_version": __import__(
                    "LiveSegmentationLib.version", fromlist=["PLUGIN_VERSION"]
                ).PLUGIN_VERSION,
                "retry_attempts": len(attempts),
                "persistent_lock_nonfatal": True,
                "previous_journal_preserved": True,
            }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "traceback": traceback.format_exc()}
    finally:
        os.replace = real_replace


output_path = os.environ.get("LIVE_SEGMENTATION_RECOVERY_PROBE_OUTPUT")
if not output_path:
    raise RuntimeError("LIVE_SEGMENTATION_RECOVERY_PROBE_OUTPUT is required")

result = run_probe()
Path(output_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
slicer.app.exit(0 if result["ok"] else 1)
