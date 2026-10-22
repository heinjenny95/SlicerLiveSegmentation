"""A slow request is not an immediate room outage or a lost edit."""
from unittest.mock import MagicMock

import pytest

from tests import controller_harness as h


@pytest.mark.parametrize('lane', ['edit-push', 'edit-pull', 'chat-send', 'presence', 'maintenance'])
@pytest.mark.parametrize('message', ["SSH request 'operations' did not respond within 30 seconds",
                                    "The shared folder is busy. Try joining or saving again in a moment."])
def test_first_timeout_retries_without_popup_or_discard(monkeypatch, lane, message):
    scene, _ = h.install_fake_slicer(monkeypatch)
    peer = h.Peer('alice', scene)
    c = peer.controller
    c._disconnect_for_connection_loss = MagicMock()
    operation = {'client_operation_id': 'stable-id'}
    c.outgoing = [operation]
    c.pending_chat = [{'client_message_id': 'stable-chat', 'text': 'hello'}]
    c._worker_results.put({'lane': lane, 'session_token': c._session_token,
                          'error': message,
                          'health_checked': lane == 'maintenance', 'health_ok': False})
    c._drain_worker_results()
    assert not peer.errors, 'First timeout showed the offline error dialog'
    c._disconnect_for_connection_loss.assert_not_called()
    assert c.connected and c.outgoing == [operation] and c.pending_chat
    assert not c._transport_retry_ready(lane)
    assert len(c.error_history) == 1


def test_persistent_timeout_warns_once_and_healthy_other_lane_does_not_erase_it(monkeypatch):
    scene, _ = h.install_fake_slicer(monkeypatch)
    peer = h.Peer('alice', scene)
    c = peer.controller
    now = [100.0]
    monkeypatch.setattr(h.collaboration.time, 'monotonic', lambda: now[0])
    def timeout():
        c._worker_results.put({'lane': 'edit-push', 'session_token': c._session_token,
                              'error': "SSH request 'push_operation' did not respond within 30 seconds"})
        c._drain_worker_results()
    timeout()
    now[0] += 61
    timeout()
    assert len(peer.errors) == 1
    for _ in range(3):
        now[0] += 10
        timeout()
    assert len(peer.errors) == 1, 'Persistent delay keeps reopening the warning'
    c._worker_results.put({'lane': 'maintenance', 'session_token': c._session_token,
                          'health_checked': True, 'health_ok': True})
    c._drain_worker_results()
    assert 'edit-push' in c._transport_delays and not c.connection_healthy
    c._worker_results.put({'lane': 'edit-push', 'session_token': c._session_token})
    c._drain_worker_results()
    assert not c._transport_delays and c.connection_healthy


def test_authentication_failure_is_not_hidden_as_a_slow_network(monkeypatch):
    scene, _ = h.install_fake_slicer(monkeypatch)
    peer = h.Peer('alice', scene)
    c = peer.controller
    c._worker_results.put({'lane': 'edit-pull', 'session_token': c._session_token,
                          'error': 'Authentication failed'})
    c._drain_worker_results()
    assert peer.errors == ['Authentication failed']


def test_partial_chat_timeout_retains_failed_message_and_backoff(monkeypatch):
    scene, _ = h.install_fake_slicer(monkeypatch)
    c = h.Peer('alice', scene).controller
    c.pending_chat = [{'client_message_id': 'pending', 'text': 'hello'}]
    c._worker_results.put({'lane': 'chat-send', 'session_token': c._session_token,
                          'chat_ids': [], 'command_errors': ["SSH request 'send_chat' timed out"]})
    c._drain_worker_results()
    assert c.pending_chat and not c._transport_retry_ready('chat-send')
    assert not c.connection_healthy
