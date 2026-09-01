from __future__ import annotations

import argparse
import shutil
import sys
import time
import uuid
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "LiveSegmentation" / "LiveSegmentationLib"))

from collaboration import SharedFolderRoomClient, encode_mask_delta  # noqa: E402


def main():
    parser = argparse.ArgumentParser(
        description="Measure the shared-folder live-operation hot path."
    )
    parser.add_argument("shared_folder", type=Path)
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()

    room_name = f"latency-probe-{uuid.uuid4().hex[:12]}"
    sender = SharedFolderRoomClient(args.shared_folder, "latency-probe-sender")
    receiver = SharedFolderRoomClient(args.shared_folder, "latency-probe-receiver")
    room = sender.join(room_name, "f" * 64)
    receiver.join(room_name, "f" * 64)
    room_path = sender._room_path.resolve()
    rooms_root = sender.rooms_root.resolve()
    try:
        empty = np.zeros((4, 4, 4), dtype=np.uint8)
        changed = empty.copy()
        changed[1:3, 1:3, 1:3] = 1
        operation = {
            "client_operation_id": uuid.uuid4().hex,
            "segment_id": "LatencyProbe",
            "segment_name": "Latency probe",
            "color_hex": "#37E8B8",
            "base_sequence": 0,
            **encode_mask_delta(empty, changed),
        }
        started = time.monotonic()
        pushed = sender.push_operation(room["id"], operation)
        push_return_seconds = time.monotonic() - started
        receiver_feed_seconds = None
        reads = 0
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            reads += 1
            if receiver.operations(room["id"], 0):
                receiver_feed_seconds = time.monotonic() - started
                break
            time.sleep(0.05)
        sender._artifact_queue.join()
        print(
            {
                "push_return_seconds": round(push_return_seconds, 3),
                "receiver_feed_seconds": (
                    None
                    if receiver_feed_seconds is None
                    else round(receiver_feed_seconds, 3)
                ),
                "poll_reads": reads,
                "sequence": int(pushed["sequence"]),
            }
        )
        if receiver_feed_seconds is None:
            raise RuntimeError("Receiver did not observe the operation before timeout")
    finally:
        try:
            sender.leave(room["id"])
            receiver.leave(room["id"])
        finally:
            if rooms_root not in room_path.parents:
                raise RuntimeError(f"Refusing to remove unexpected probe path: {room_path}")
            shutil.rmtree(room_path)


if __name__ == "__main__":
    main()
