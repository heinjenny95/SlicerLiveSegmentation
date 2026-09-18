"""Only users who joined a room may use it (review finding L-03)."""

from __future__ import annotations

import pytest

SIGNATURE = "a" * 64
OTHER_SIGNATURE = "b" * 64
ALICE = {"X-LiveSeg-User": "alice"}
MALLORY = {"X-LiveSeg-User": "mallory"}


def join(client, headers, signature=SIGNATURE, room_name="Project A"):
    return client.post(
        "/api/live/rooms/join",
        headers=headers,
        json={"room_name": room_name, "volume_signature": signature},
    )


def preflight(client, headers, signature, room_name="Project A"):
    response = client.post(
        "/api/live/preflight",
        headers=headers,
        json={
            "room_name": room_name,
            "volume_signature": signature,
            "plugin_version": "0.15.1",
            "protocol_version": 3,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def room_id(client):
    response = join(client, ALICE)
    assert response.status_code == 200, response.text
    identifier = response.json()["id"]
    sent = client.post(
        f"/api/live/rooms/{identifier}/messages",
        headers=ALICE,
        json={"text": "lesion in segment VII", "client_message_id": "message-0001"},
    )
    assert sent.status_code == 201, sent.text
    return identifier


READ_ENDPOINTS = (
    "operations",
    "messages",
    "locks",
    "roles",
    "reviews",
    "access-requests",
    "conflicts",
    "material-template",
    "audit",
)


@pytest.mark.parametrize("endpoint", READ_ENDPOINTS)
def test_user_who_never_joined_cannot_read_a_room(client, room_id, endpoint):
    assert client.get(f"/api/live/rooms/{room_id}/{endpoint}", headers=ALICE).status_code == 200
    refused = client.get(f"/api/live/rooms/{room_id}/{endpoint}", headers=MALLORY)
    assert refused.status_code == 403
    assert "lesion" not in refused.text


def test_user_who_never_joined_cannot_write_to_a_room(client, room_id):
    chat = client.post(
        f"/api/live/rooms/{room_id}/messages",
        headers=MALLORY,
        json={"text": "hello", "client_message_id": "message-0002"},
    )
    presence = client.post(f"/api/live/rooms/{room_id}/presence", headers=MALLORY, json={})
    lock = client.put(
        f"/api/live/rooms/{room_id}/locks/S1", headers=MALLORY, json={"locked": True}
    )
    assert [chat.status_code, presence.status_code, lock.status_code] == [403, 403, 403]
    messages = client.get(f"/api/live/rooms/{room_id}/messages", headers=ALICE).json()
    assert [item["text"] for item in messages] == ["lesion in segment VII"]


def test_wrong_source_volume_does_not_admit_a_user(client, room_id):
    assert join(client, MALLORY, OTHER_SIGNATURE).status_code == 409
    assert client.get(f"/api/live/rooms/{room_id}/messages", headers=MALLORY).status_code == 403


def test_joining_with_the_source_volume_admits_a_user(client, room_id):
    assert join(client, MALLORY, SIGNATURE).status_code == 200
    messages = client.get(f"/api/live/rooms/{room_id}/messages", headers=MALLORY)
    assert messages.status_code == 200
    assert [item["text"] for item in messages.json()] == ["lesion in segment VII"]
    # Membership of one room says nothing about another.
    other = join(client, ALICE, OTHER_SIGNATURE, room_name="Project B").json()["id"]
    assert client.get(f"/api/live/rooms/{other}/messages", headers=MALLORY).status_code == 403


def test_unknown_room_is_still_reported_as_missing(client):
    assert client.get("/api/live/rooms/does-not-exist/messages", headers=ALICE).status_code == 404


def test_preflight_does_not_reveal_another_users_volume_signature(client, room_id):
    preflight(client, ALICE, SIGNATURE)

    probe = preflight(client, MALLORY, OTHER_SIGNATURE)
    assert SIGNATURE not in str(probe)
    (alice_entry,) = [
        item for item in probe["preflight_participants"] if item["user"] == "alice"
    ]
    assert alice_entry["volume_signature_matches"] is False
    assert alice_entry["volume_signature"] != OTHER_SIGNATURE
    assert probe["room_compatible"] is False

    # With the leaked value gone, the documented attack no longer works.
    assert join(client, MALLORY, alice_entry["volume_signature"]).status_code == 409
    assert client.get(f"/api/live/rooms/{room_id}/messages", headers=MALLORY).status_code == 403


def test_preflight_still_confirms_a_matching_second_computer(client, room_id):
    preflight(client, ALICE, SIGNATURE)
    report = preflight(client, {"X-LiveSeg-User": "bob"}, SIGNATURE)
    (alice_entry,) = [
        item for item in report["preflight_participants"] if item["user"] == "alice"
    ]
    assert alice_entry["volume_signature_matches"] is True
    # 0.15.1 clients compare this field with the signature they sent.
    assert alice_entry["volume_signature"] == report["requested_volume_signature"]
