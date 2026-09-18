"""Tests for the Triad matrix provider lifecycle."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.providers.triad_matrix_test.player import TriadMatrixTestPlayer
from music_assistant.providers.triad_matrix_test.provider import (
    BUS_DEFINITIONS,
    ROOMS,
    MatrixBus,
    TriadMatrixTestProvider,
)


def _backend(
    player_id: str,
    state: PlaybackState = PlaybackState.IDLE,
) -> SimpleNamespace:
    """Build a minimal native Sonos player stand-in."""
    return SimpleNamespace(
        player_id=player_id,
        display_name=player_id,
        state=SimpleNamespace(
            available=True,
            playback_state=state,
        ),
    )


def _provider(
    backend_states: tuple[PlaybackState, PlaybackState] = (
        PlaybackState.IDLE,
        PlaybackState.IDLE,
    ),
) -> TriadMatrixTestProvider:
    """Build a provider without running the base provider initializer."""
    provider = TriadMatrixTestProvider.__new__(TriadMatrixTestProvider)
    provider._bus_lock = asyncio.Lock()
    provider._buses = [MatrixBus(definition) for definition in BUS_DEFINITIONS]

    provider._players_by_id = cast(
        "dict[str, TriadMatrixTestPlayer]",
        {
            player_id: SimpleNamespace(
                player_id=player_id,
                display_name=str(room["name"]),
                zone_entity=str(room["entity_id"]),
                state=SimpleNamespace(
                    playback_state=PlaybackState.IDLE,
                    synced_to=None,
                ),
            )
            for player_id, room in ROOMS.items()
        },
    )

    backends = {
        definition.backend_player_id: _backend(
            definition.backend_player_id,
            backend_states[index],
        )
        for index, definition in enumerate(BUS_DEFINITIONS)
    }

    provider.mass = cast(
        "Any",
        SimpleNamespace(
            players=SimpleNamespace(
                get_player=backends.get,
            ),
        ),
    )
    provider.logger = MagicMock()

    provider._get_zone_states = AsyncMock(  # type: ignore[method-assign]
        return_value={
            str(room["entity_id"]): {
                "entity_id": room["entity_id"],
                "state": "off",
                "attributes": {
                    "source": None,
                },
            }
            for room in ROOMS.values()
        }
    )

    return provider


def _set_route(
    provider: TriadMatrixTestProvider,
    player_id: str,
    source: str,
) -> TriadMatrixTestPlayer:
    """Make a fake physical Triad room route to the requested source."""
    player = provider.get_room_player(player_id)
    assert player is not None

    states = provider._get_zone_states.return_value  # type: ignore[attr-defined]
    states[player.zone_entity]["state"] = "on"
    states[player.zone_entity]["attributes"]["source"] = source

    return player


def _install_fake_turn_off(
    provider: TriadMatrixTestProvider,
) -> AsyncMock:
    """Install a successful physical-disconnect stand-in."""
    states = provider._get_zone_states.return_value  # type: ignore[attr-defined]

    async def fake_turn_off(player: TriadMatrixTestPlayer) -> None:
        states[player.zone_entity]["state"] = "off"
        states[player.zone_entity]["attributes"]["source"] = None

    mock = AsyncMock(side_effect=fake_turn_off)
    provider.turn_off_zone = mock  # type: ignore[method-assign]
    return mock


def test_physical_map() -> None:
    """The provider should expose the validated 13-room and two-bus map."""
    assert [room["output"] for room in ROOMS.values()] == list(range(1, 14))
    assert [bus.source_name for bus in BUS_DEFINITIONS] == [
        "Connect 1",
        "Connect 2",
    ]
    assert [bus.triad_input for bus in BUS_DEFINITIONS] == [3, 4]
    assert [bus.backend_player_id for bus in BUS_DEFINITIONS] == [
        "RINCON_B8E937997EE801400",
        "RINCON_B8E937997EEE01400",
    ]


async def test_two_active_claims_then_third_is_refused() -> None:
    """Two active sessions should reserve both buses without bus stealing."""
    provider = _provider()
    first_id, second_id, third_id = list(ROOMS)[:3]

    first_bus, _ = await provider.claim_bus(first_id, [first_id])
    first = provider.get_room_player(first_id)
    assert first is not None
    first.state.playback_state = PlaybackState.PLAYING

    second_bus, _ = await provider.claim_bus(second_id, [second_id])
    second = provider.get_room_player(second_id)
    assert second is not None
    second.state.playback_state = PlaybackState.PLAYING

    assert first_bus.source_name == "Connect 1"
    assert second_bus.source_name == "Connect 2"

    with pytest.raises(PlayerCommandFailed, match="left untouched"):
        await provider.claim_bus(third_id, [third_id])

    assert provider.get_bus_for_owner(first_id) is first_bus
    assert provider.get_bus_for_owner(second_id) is second_bus
    assert provider.get_bus_for_owner(third_id) is None


async def test_ownerless_idle_route_is_reclaimed_before_claim() -> None:
    """An idle ownerless physical route should be cleared before reuse."""
    provider = _provider()
    new_owner_id, stale_id = list(ROOMS)[:2]

    stale = _set_route(provider, stale_id, "Connect 1")
    turn_off = _install_fake_turn_off(provider)

    bus, _ = await provider.claim_bus(new_owner_id, [new_owner_id])

    assert bus.source_name == "Connect 1"
    assert bus.owner_id == new_owner_id
    turn_off.assert_awaited_once_with(stale)


async def test_ownerless_paused_route_is_reclaimed_before_claim() -> None:
    """A stale ownerless route with a paused backend should be reclaimable."""
    provider = _provider(
        (
            PlaybackState.PAUSED,
            PlaybackState.PLAYING,
        )
    )
    new_owner_id, stale_id = list(ROOMS)[:2]

    stale = _set_route(provider, stale_id, "Connect 1")
    turn_off = _install_fake_turn_off(provider)

    async def fake_prepare(bus: MatrixBus, backend: Any) -> bool:
        backend.state.playback_state = PlaybackState.IDLE
        return True

    prepare = AsyncMock(side_effect=fake_prepare)
    provider._prepare_backend_for_reclaim = prepare  # type: ignore[method-assign]

    bus, _ = await provider.claim_bus(new_owner_id, [new_owner_id])

    assert bus.source_name == "Connect 1"
    assert bus.owner_id == new_owner_id
    prepare.assert_awaited_once()
    turn_off.assert_awaited_once_with(stale)


async def test_requested_room_with_ownerless_paused_route_can_be_reused() -> None:
    """A requested room's stale paused route should be cleaned and reused."""
    provider = _provider(
        (
            PlaybackState.PAUSED,
            PlaybackState.PLAYING,
        )
    )
    owner_id = next(iter(ROOMS))

    owner = _set_route(provider, owner_id, "Connect 1")
    turn_off = _install_fake_turn_off(provider)

    async def fake_prepare(bus: MatrixBus, backend: Any) -> bool:
        backend.state.playback_state = PlaybackState.IDLE
        return True

    prepare = AsyncMock(side_effect=fake_prepare)
    provider._prepare_backend_for_reclaim = prepare  # type: ignore[method-assign]

    bus, _ = await provider.claim_bus(owner_id, [owner_id])

    assert bus.source_name == "Connect 1"
    assert bus.owner_id == owner_id
    prepare.assert_awaited_once()
    turn_off.assert_awaited_once_with(owner)


async def test_owned_idle_session_with_paused_backend_is_reclaimable() -> None:
    """An idle logical owner may release an ended paused backend."""
    provider = _provider(
        (
            PlaybackState.PAUSED,
            PlaybackState.PLAYING,
        )
    )
    owner_id = next(iter(ROOMS))
    owner = _set_route(provider, owner_id, "Connect 1")

    bus = provider._buses[0]
    bus.owner_id = owner_id

    turn_off = _install_fake_turn_off(provider)

    async def fake_prepare(bus: MatrixBus, backend: Any) -> bool:
        backend.state.playback_state = PlaybackState.IDLE
        return True

    prepare = AsyncMock(side_effect=fake_prepare)
    provider._prepare_backend_for_reclaim = prepare  # type: ignore[method-assign]

    reclaimed = await provider.reconcile_idle_bus_owner(owner_id)

    assert reclaimed is True
    assert bus.owner_id is None
    prepare.assert_awaited_once()
    turn_off.assert_awaited_once_with(owner)


async def test_intentionally_paused_owned_session_is_not_reclaimed() -> None:
    """A logical PAUSED session must remain reserved indefinitely."""
    provider = _provider(
        (
            PlaybackState.PAUSED,
            PlaybackState.PLAYING,
        )
    )
    owner_id = next(iter(ROOMS))
    owner = _set_route(provider, owner_id, "Connect 1")
    owner.state.playback_state = PlaybackState.PAUSED

    bus = provider._buses[0]
    bus.owner_id = owner_id

    provider._prepare_backend_for_reclaim = AsyncMock(  # type: ignore[method-assign]
        return_value=True
    )
    provider.turn_off_zone = AsyncMock()  # type: ignore[method-assign]

    reclaimed = await provider.reconcile_idle_bus_owner(owner_id)

    assert reclaimed is False
    assert bus.owner_id == owner_id
    provider._prepare_backend_for_reclaim.assert_not_awaited()
    provider.turn_off_zone.assert_not_awaited()


async def test_ownerless_logically_paused_room_is_not_reclaimed() -> None:
    """A physically routed logical PAUSED room must block reclamation."""
    provider = _provider(
        (
            PlaybackState.PAUSED,
            PlaybackState.PLAYING,
        )
    )
    player_id = next(iter(ROOMS))
    player = _set_route(provider, player_id, "Connect 1")
    player.state.playback_state = PlaybackState.PAUSED

    bus = provider._buses[0]

    provider._prepare_backend_for_reclaim = AsyncMock(  # type: ignore[method-assign]
        return_value=True
    )
    provider.turn_off_zone = AsyncMock()  # type: ignore[method-assign]

    reclaimed = await provider._reconcile_ownerless_idle_bus_locked(bus)

    assert reclaimed is False
    provider._prepare_backend_for_reclaim.assert_not_awaited()
    provider.turn_off_zone.assert_not_awaited()


async def test_ownerless_logically_playing_room_is_not_reclaimed() -> None:
    """A physically routed logical PLAYING room must block reclamation."""
    provider = _provider(
        (
            PlaybackState.PLAYING,
            PlaybackState.IDLE,
        )
    )
    player_id = next(iter(ROOMS))
    player = _set_route(provider, player_id, "Connect 1")
    player.state.playback_state = PlaybackState.PLAYING

    bus = provider._buses[0]

    provider._prepare_backend_for_reclaim = AsyncMock(  # type: ignore[method-assign]
        return_value=True
    )
    provider.turn_off_zone = AsyncMock()  # type: ignore[method-assign]

    reclaimed = await provider._reconcile_ownerless_idle_bus_locked(bus)

    assert reclaimed is False
    provider._prepare_backend_for_reclaim.assert_not_awaited()
    provider.turn_off_zone.assert_not_awaited()


async def test_missing_raw_state_prevents_claim() -> None:
    """Incomplete HA state must fail safely before claiming a bus."""
    provider = _provider()
    owner_id = next(iter(ROOMS))

    states = provider._get_zone_states.return_value  # type: ignore[attr-defined]
    states.pop(next(iter(states)))

    with pytest.raises(
        PlayerCommandFailed,
        match="did not return every Triad room state",
    ):
        await provider.claim_bus(owner_id, [owner_id])

    assert all(bus.owner_id is None for bus in provider._buses)


async def test_ownerless_paused_backend_stop_failure_refuses_claim() -> None:
    """A failed stale-backend stop must leave the route untouched."""
    provider = _provider(
        (
            PlaybackState.PAUSED,
            PlaybackState.PLAYING,
        )
    )
    new_owner_id, stale_id = list(ROOMS)[:2]

    _set_route(provider, stale_id, "Connect 1")

    prepare = AsyncMock(return_value=False)
    turn_off = AsyncMock()
    provider._prepare_backend_for_reclaim = prepare  # type: ignore[method-assign]
    provider.turn_off_zone = turn_off  # type: ignore[method-assign]

    with pytest.raises(PlayerCommandFailed, match="left untouched"):
        await provider.claim_bus(new_owner_id, [new_owner_id])

    prepare.assert_awaited_once()
    turn_off.assert_not_awaited()
    assert all(bus.owner_id is None for bus in provider._buses)


async def test_ownerless_disconnect_failure_refuses_claim() -> None:
    """A failed physical disconnect must prevent ownership from being granted."""
    provider = _provider(
        (
            PlaybackState.IDLE,
            PlaybackState.PLAYING,
        )
    )
    new_owner_id, stale_id = list(ROOMS)[:2]

    stale = _set_route(provider, stale_id, "Connect 1")

    turn_off = AsyncMock(
        side_effect=PlayerCommandFailed("disconnect verification failed")
    )
    provider.turn_off_zone = turn_off  # type: ignore[method-assign]

    with pytest.raises(PlayerCommandFailed, match="left untouched"):
        await provider.claim_bus(new_owner_id, [new_owner_id])

    turn_off.assert_awaited_once_with(stale)
    assert all(bus.owner_id is None for bus in provider._buses)


async def test_ownerless_mixed_idle_and_playing_routes_refuses_without_cleanup() -> None:
    """One active room must protect every route sharing its physical bus."""
    provider = _provider(
        (
            PlaybackState.IDLE,
            PlaybackState.PLAYING,
        )
    )
    new_owner_id, idle_id, active_id = list(ROOMS)[:3]

    _set_route(provider, idle_id, "Connect 1")
    active = _set_route(provider, active_id, "Connect 1")
    active.state.playback_state = PlaybackState.PLAYING

    prepare = AsyncMock(return_value=True)
    turn_off = AsyncMock()
    provider._prepare_backend_for_reclaim = prepare  # type: ignore[method-assign]
    provider.turn_off_zone = turn_off  # type: ignore[method-assign]

    with pytest.raises(PlayerCommandFailed, match="left untouched"):
        await provider.claim_bus(new_owner_id, [new_owner_id])

    prepare.assert_not_awaited()
    turn_off.assert_not_awaited()
    assert all(bus.owner_id is None for bus in provider._buses)


async def test_ownerless_grouped_route_refuses_without_cleanup() -> None:
    """A grouped logical room must protect an ownerless physical route."""
    provider = _provider(
        (
            PlaybackState.IDLE,
            PlaybackState.PLAYING,
        )
    )
    new_owner_id, stale_id, other_id = list(ROOMS)[:3]

    stale = _set_route(provider, stale_id, "Connect 1")
    stale.state.synced_to = other_id

    prepare = AsyncMock(return_value=True)
    turn_off = AsyncMock()
    provider._prepare_backend_for_reclaim = prepare  # type: ignore[method-assign]
    provider.turn_off_zone = turn_off  # type: ignore[method-assign]

    with pytest.raises(PlayerCommandFailed, match="left untouched"):
        await provider.claim_bus(new_owner_id, [new_owner_id])

    prepare.assert_not_awaited()
    turn_off.assert_not_awaited()
    assert all(bus.owner_id is None for bus in provider._buses)


async def test_ownerless_backend_playing_refuses_without_disconnect() -> None:
    """A playing backend must never be reclaimed from an ownerless route."""
    provider = _provider(
        (
            PlaybackState.PLAYING,
            PlaybackState.PLAYING,
        )
    )
    new_owner_id, stale_id = list(ROOMS)[:2]

    _set_route(provider, stale_id, "Connect 1")

    turn_off = AsyncMock()
    provider.turn_off_zone = turn_off  # type: ignore[method-assign]

    with pytest.raises(PlayerCommandFailed, match="left untouched"):
        await provider.claim_bus(new_owner_id, [new_owner_id])

    turn_off.assert_not_awaited()
    assert all(bus.owner_id is None for bus in provider._buses)


async def test_multiple_ownerless_idle_routes_are_cleaned_together() -> None:
    """All stale idle rooms on one bus should be disconnected before reuse."""
    provider = _provider(
        (
            PlaybackState.IDLE,
            PlaybackState.PLAYING,
        )
    )
    stale_one_id, stale_two_id, new_owner_id = list(ROOMS)[:3]

    stale_one = _set_route(provider, stale_one_id, "Connect 1")
    stale_two = _set_route(provider, stale_two_id, "Connect 1")

    turn_off = _install_fake_turn_off(provider)

    bus, _ = await provider.claim_bus(new_owner_id, [new_owner_id])

    assert bus.source_name == "Connect 1"
    assert bus.owner_id == new_owner_id
    assert turn_off.await_count == 2
    turn_off.assert_any_await(stale_one)
    turn_off.assert_any_await(stale_two)


async def test_owned_disconnect_failure_retains_bus_reservation() -> None:
    """A failed disconnect must not release an owned stale bus."""
    provider = _provider(
        (
            PlaybackState.IDLE,
            PlaybackState.PLAYING,
        )
    )
    owner_id = next(iter(ROOMS))
    owner = _set_route(provider, owner_id, "Connect 1")

    bus = provider._buses[0]
    bus.owner_id = owner_id

    turn_off = AsyncMock(
        side_effect=PlayerCommandFailed("disconnect verification failed")
    )
    provider.turn_off_zone = turn_off  # type: ignore[method-assign]

    reclaimed = await provider.reconcile_idle_bus_owner(owner_id)

    assert reclaimed is False
    assert bus.owner_id == owner_id
    turn_off.assert_awaited_once_with(owner)


async def test_owned_paused_backend_stop_failure_retains_bus_reservation() -> None:
    """A failed paused-backend stop must retain logical ownership."""
    provider = _provider(
        (
            PlaybackState.PAUSED,
            PlaybackState.PLAYING,
        )
    )
    owner_id = next(iter(ROOMS))

    _set_route(provider, owner_id, "Connect 1")

    bus = provider._buses[0]
    bus.owner_id = owner_id

    prepare = AsyncMock(return_value=False)
    turn_off = AsyncMock()
    provider._prepare_backend_for_reclaim = prepare  # type: ignore[method-assign]
    provider.turn_off_zone = turn_off  # type: ignore[method-assign]

    reclaimed = await provider.reconcile_idle_bus_owner(owner_id)

    assert reclaimed is False
    assert bus.owner_id == owner_id
    prepare.assert_awaited_once()
    turn_off.assert_not_awaited()


async def test_backend_unavailable_with_ownerless_route_is_not_modified() -> None:
    """An unavailable backend must leave its physical route untouched."""
    provider = _provider(
        (
            PlaybackState.IDLE,
            PlaybackState.PLAYING,
        )
    )
    new_owner_id, stale_id = list(ROOMS)[:2]

    _set_route(provider, stale_id, "Connect 1")

    backend = provider.mass.players.get_player(
        BUS_DEFINITIONS[0].backend_player_id
    )
    assert backend is not None
    backend.state.available = False

    turn_off = AsyncMock()
    provider.turn_off_zone = turn_off  # type: ignore[method-assign]

    with pytest.raises(PlayerCommandFailed, match="No free Triad music source"):
        await provider.claim_bus(new_owner_id, [new_owner_id])

    turn_off.assert_not_awaited()
    assert all(bus.owner_id is None for bus in provider._buses)


async def test_release_requires_clear_matrix_routes() -> None:
    """A bus should remain reserved until every room is disconnected."""
    provider = _provider()
    owner_id = next(iter(ROOMS))

    bus, _ = await provider.claim_bus(owner_id, [owner_id])

    owner = provider.get_room_player(owner_id)
    assert owner is not None

    states = provider._get_zone_states.return_value  # type: ignore[attr-defined]
    states[owner.zone_entity]["state"] = "on"
    states[owner.zone_entity]["attributes"]["source"] = bus.source_name

    with pytest.raises(PlayerCommandFailed, match="Refusing to release"):
        await provider.release_bus(owner_id)

    assert provider.get_bus_for_owner(owner_id) is bus

    states[owner.zone_entity]["state"] = "off"
    states[owner.zone_entity]["attributes"]["source"] = None

    await provider.release_bus(owner_id)

    assert provider.get_bus_for_owner(owner_id) is None


async def _poll_playback_state(
    backend_state: PlaybackState,
    *,
    queue_ended: bool = False,
    flow_exhausted: bool = False,
    intentional_pause: bool = False,
    media_source_id: str | None,
) -> PlaybackState:
    """Poll one logical room against controlled backend/queue lifecycle state."""
    provider = _provider((backend_state, PlaybackState.PLAYING))
    player_id = next(iter(ROOMS))
    room = ROOMS[player_id]

    bus = provider._buses[0]
    bus.owner_id = player_id

    backend = provider.mass.players.get_player(BUS_DEFINITIONS[0].backend_player_id)
    assert backend is not None
    backend.state.elapsed_time = None
    backend.state.elapsed_time_last_updated = None

    session_id = "test-session"
    queue = SimpleNamespace(ended=queue_ended)
    queue_data = SimpleNamespace(session_id=session_id)

    provider.mass.player_queues = SimpleNamespace(
        get=lambda queue_id: queue if queue_id == player_id else None,
        queue_data_or_none=lambda queue_id: (
            queue_data if queue_id == player_id else None
        ),
        flow_queue_exhausted=lambda queue_id, candidate_session_id: (
            flow_exhausted
            and queue_id == player_id
            and candidate_session_id == session_id
        ),
    )
    provider.get_zone_state = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "entity_id": room["entity_id"],
            "state": "on",
            "attributes": {},
        }
    )

    player = TriadMatrixTestPlayer.__new__(TriadMatrixTestPlayer)
    player._provider = provider
    player.mass = provider.mass
    player._player_id = player_id
    player.zone_entity = str(room["entity_id"])
    player._intentional_pause = intentional_pause
    player._attr_playback_state = (
        PlaybackState.PAUSED if intentional_pause else PlaybackState.PLAYING
    )
    player._attr_current_media = (
        SimpleNamespace(source_id=media_source_id) if media_source_id is not None else None
    )
    cast("Any", player).update_state = MagicMock()

    await player.poll()
    return player._attr_playback_state


async def test_poll_treats_ended_queue_paused_backend_as_idle() -> None:
    """A persisted ended queue must stay logically idle if Sonos lingers paused."""
    player_id = next(iter(ROOMS))

    state = await _poll_playback_state(
        PlaybackState.PAUSED,
        queue_ended=True,
        media_source_id=player_id,
    )

    assert state == PlaybackState.IDLE


async def test_poll_treats_exhausted_flow_paused_backend_as_idle() -> None:
    """Flow exhaustion must break the paused-backend end-of-queue deadlock."""
    player_id = next(iter(ROOMS))

    state = await _poll_playback_state(
        PlaybackState.PAUSED,
        flow_exhausted=True,
        media_source_id=player_id,
    )

    assert state == PlaybackState.IDLE


async def test_poll_preserves_pre_end_backend_pause() -> None:
    """A paused backend before flow exhaustion must not be guessed to have ended."""
    player_id = next(iter(ROOMS))

    state = await _poll_playback_state(
        PlaybackState.PAUSED,
        media_source_id=player_id,
    )

    assert state == PlaybackState.PAUSED


async def test_poll_preserves_intentional_pause_for_active_queue() -> None:
    """An explicit user pause must remain logically paused."""
    player_id = next(iter(ROOMS))

    state = await _poll_playback_state(
        PlaybackState.PAUSED,
        intentional_pause=True,
        media_source_id=player_id,
    )

    assert state == PlaybackState.PAUSED


async def test_poll_preserves_intentional_pause_for_exhausted_flow() -> None:
    """Flow exhaustion must never override an explicit user pause."""
    player_id = next(iter(ROOMS))

    state = await _poll_playback_state(
        PlaybackState.PAUSED,
        flow_exhausted=True,
        intentional_pause=True,
        media_source_id=player_id,
    )

    assert state == PlaybackState.PAUSED


async def test_poll_preserves_intentional_pause_if_backend_reports_idle() -> None:
    """A transient backend idle report must not release an explicit pause."""
    player_id = next(iter(ROOMS))

    state = await _poll_playback_state(
        PlaybackState.IDLE,
        intentional_pause=True,
        media_source_id=player_id,
    )

    assert state == PlaybackState.PAUSED


async def test_poll_preserves_playing_backend_for_exhausted_flow() -> None:
    """Flow exhaustion must not hide audio the backend still reports as playing."""
    player_id = next(iter(ROOMS))

    state = await _poll_playback_state(
        PlaybackState.PLAYING,
        flow_exhausted=True,
        media_source_id=player_id,
    )

    assert state == PlaybackState.PLAYING


async def test_poll_preserves_paused_unrelated_media() -> None:
    """Queue lifecycle state must not mask paused media from another source."""
    state = await _poll_playback_state(
        PlaybackState.PAUSED,
        queue_ended=True,
        flow_exhausted=True,
        media_source_id="some_other_source",
    )

    assert state == PlaybackState.PAUSED
