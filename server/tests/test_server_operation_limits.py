"""One request must not exhaust or stall the HTTPS server (review finding L-04)."""

from __future__ import annotations

import base64
import inspect
import sys
import time
import zlib
from pathlib import Path

import numpy as np
import pytest

LIVE_CORE = Path(__file__).resolve().parents[2] / "LiveSegmentation" / "LiveSegmentationLib"
sys.path.insert(0, str(LIVE_CORE))

from app.main import changed_voxel_bits, changed_voxel_count, operation_overlap  # noqa: E402
from collaboration import encode_mask_delta  # noqa: E402

SIGNATURE = "c" * 64
ALICE = {"X-LiveSeg-User": "alice"}
BOB = {"X-LiveSeg-User": "bob"}


def join(client, headers):
    response = client.post(
        "/api/live/rooms/join",
        headers=headers,
        json={"room_name": "limits", "volume_signature": SIGNATURE},
    )
    assert response.status_code == 200, response.text
    return response.json()


def operation(previous, current, operation_id, **extra):
    return {
        "client_operation_id": operation_id,
        "segment_id": "S1",
        "segment_name": "S1",
        "color_hex": "#37E8B8",
        **encode_mask_delta(previous, current),
        **extra,
    }


def test_small_payload_cannot_inflate_beyond_its_bounds(client):
    room = join(client, ALICE)
    empty = np.zeros((4, 4, 4), np.uint8)
    painted = empty.copy()
    painted[0, 0, 0] = 1
    bomb = operation(empty, painted, "bomb-0001")
    bomb["payload"] = base64.b64encode(zlib.compress(b"\x00" * (256 * 1024 * 1024), 9)).decode()
    assert len(bomb["payload"]) < 1024 * 1024  # passes the upload limit of this test server

    started = time.perf_counter()
    response = client.post(f"/api/live/rooms/{room['id']}/operations", headers=ALICE, json=bomb)
    assert response.status_code == 422
    assert "payload length" in response.text
    assert time.perf_counter() - started < 5.0
    assert client.get(f"/api/live/rooms/{room['id']}/operations", headers=ALICE).json() == []


@pytest.mark.parametrize(
    "payload",
    [
        base64.b64encode(b"not zlib at all").decode(),
        base64.b64encode(zlib.compress(b"\x01")).decode(),
    ],
)
def test_undecodable_payload_is_refused_instead_of_stored(client, payload):
    room = join(client, ALICE)
    empty = np.zeros((4, 4, 4), np.uint8)
    painted = empty.copy()
    painted[1, 1, 1] = 1
    damaged = {**operation(empty, painted, "damaged-01"), "payload": payload}
    response = client.post(
        f"/api/live/rooms/{room['id']}/operations", headers=ALICE, json=damaged
    )
    assert response.status_code == 422
    assert client.get(f"/api/live/rooms/{room['id']}/operations", headers=ALICE).json() == []


def test_operation_endpoint_does_not_run_on_the_event_loop(client):
    route = next(
        route
        for route in client.app.routes
        if getattr(route, "path", "") == "/api/live/rooms/{room_id}/operations"
        and "POST" in getattr(route, "methods", set())
    )
    assert not inspect.iscoroutinefunction(route.endpoint)


def test_fast_overlap_matches_a_dense_reference():
    rng = np.random.default_rng(7)
    shape = (24, 20, 28)
    empty = np.zeros(shape, np.uint8)
    for _ in range(25):
        masks = []
        for _ in range(2):
            mask = empty.copy()
            low = [int(rng.integers(0, size - 2)) for size in shape]
            high = [int(rng.integers(low[axis] + 1, shape[axis] + 1)) for axis in range(3)]
            region = tuple(slice(low[axis], high[axis]) for axis in range(3))
            mask[region] = rng.integers(0, 2, size=mask[region].shape, dtype=np.uint8)
            if not mask.any():
                mask[low[0], low[1], low[2]] = 1
            masks.append(mask)
        first = operation(empty, masks[0], "first-0001")
        second = operation(empty, masks[1], "second-001")
        expected = int(np.count_nonzero(masks[0] & masks[1]))
        assert operation_overlap(first, second) == expected
        assert operation_overlap(second, first) == expected
        assert changed_voxel_count(first) == int(masks[0].sum())
        assert changed_voxel_count(first, changed_voxel_bits(first)) == int(masks[0].sum())


def test_large_box_conflict_check_is_still_correct(client):
    room = join(client, ALICE)
    join(client, BOB)
    shape = (128, 128, 128)
    empty = np.zeros(shape, np.uint8)
    first = empty.copy()
    first[0, 0, 0] = 1
    first[127, 127, 127] = 1
    second = empty.copy()
    second[0, 0, 0] = 1
    second[127, 127, 126] = 1
    second[127, 127, 127] = 1
    url = f"/api/live/rooms/{room['id']}/operations"
    # Bob's view of the room ends at his own first operation ...
    seen_by_bob = client.post(
        url, headers=BOB, json=operation(empty, second, "bob-00001")
    ).json()["sequence"]
    # ... then Alice paints, and Bob paints again without having seen that.
    alice_response = client.post(
        url, headers=ALICE, json=operation(empty, first, "alice-0001")
    )
    assert alice_response.status_code == 201

    concurrent = client.post(
        url,
        headers=BOB,
        json=operation(empty, second, "bob-00002", base_sequence=seen_by_bob),
    )
    assert concurrent.status_code == 201
    (conflict,) = concurrent.json()["conflicts"]
    # Correctness only: a wall-clock bound here would flicker on slow CI runners.
    assert conflict["overlap_voxels"] == 2
