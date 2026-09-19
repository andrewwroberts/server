"""Tests for rejecting reconnects to exhausted queue flow sessions."""

from __future__ import annotations

from unittest.mock import MagicMock, Mock

import pytest
from aiohttp import web

from music_assistant.controllers.streams.controller import StreamsController

QUEUE_ID = "q1"
SESSION_ID = "sess1"
ITEM_ID = "item1"
PLAYER_ID = "player1"


def _request(method: str = "GET") -> MagicMock:
    request = MagicMock()
    request.method = method
    request.match_info = {
        "queue_id": QUEUE_ID,
        "player_id": PLAYER_ID,
        "session_id": SESSION_ID,
        "queue_item_id": ITEM_ID,
        "fmt": "flac",
    }
    return request


def _controller(*, exhausted: bool) -> MagicMock:
    ctrl = MagicMock()
    ctrl.logger = Mock()
    ctrl._log_request = Mock()
    ctrl.mass = MagicMock()

    queue = MagicMock()
    queue.queue_id = QUEUE_ID
    queue.display_name = "Kitchen"
    ctrl.mass.player_queues.get.return_value = queue

    queue_data = MagicMock()
    queue_data.session_id = SESSION_ID
    ctrl.mass.player_queues.queue_data.return_value = queue_data
    ctrl.mass.player_queues.flow_queue_exhausted.return_value = exhausted

    return ctrl


async def test_exhausted_flow_get_is_refused_before_player_lookup() -> None:
    ctrl = _controller(exhausted=True)

    with pytest.raises(web.HTTPNotFound) as exc:
        await StreamsController.serve_queue_flow_stream(ctrl, _request())

    assert "already exhausted" in str(exc.value.reason)
    ctrl.mass.player_queues.flow_queue_exhausted.assert_called_once_with(
        QUEUE_ID,
        SESSION_ID,
    )
    ctrl.mass.players.get_player.assert_not_called()


async def test_active_flow_get_reaches_normal_player_validation() -> None:
    ctrl = _controller(exhausted=False)
    ctrl.mass.players.get_player.return_value = None

    with pytest.raises(web.HTTPNotFound) as exc:
        await StreamsController.serve_queue_flow_stream(ctrl, _request())

    assert "Unknown Player" in str(exc.value.reason)
    ctrl.mass.player_queues.flow_queue_exhausted.assert_called_once_with(
        QUEUE_ID,
        SESSION_ID,
    )


async def test_exhausted_flow_head_is_not_blocked_by_get_gate() -> None:
    ctrl = _controller(exhausted=True)
    ctrl.mass.players.get_player.return_value = None

    with pytest.raises(web.HTTPNotFound) as exc:
        await StreamsController.serve_queue_flow_stream(ctrl, _request("HEAD"))

    assert "Unknown Player" in str(exc.value.reason)
    ctrl.mass.player_queues.flow_queue_exhausted.assert_not_called()
