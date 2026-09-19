"""Tests for the Triad matrix provider lifecycle."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.player import PlayerMedia
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.controllers.player_queues.controller import PlayerQueuesController
from music_assistant.controllers.players.controller import PlayerController
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

    third = provider.get_room_player(third_id)
    assert third is not None

    with pytest.raises(
        PlayerCommandFailed,
        match="both Triad music streams are already playing",
    ) as exc_info:
        await provider.claim_bus(third_id, [third_id])

    assert exc_info.value.translation_key == "all_streams_busy"
    assert exc_info.value.translation_owner == "provider.triad_matrix_test"
    assert exc_info.value.translation_args == [
        third.display_name,
        f"{first.display_name}, {second.display_name}",
    ]

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


async def test_ownerless_paused_backend_without_routes_is_reclaimed_before_claim() -> None:
    """An unrouted ownerless paused backend should be stopped and reused."""
    provider = _provider(
        (
            PlaybackState.PAUSED,
            PlaybackState.PLAYING,
        )
    )
    new_owner_id = next(iter(ROOMS))

    async def fake_prepare(bus: MatrixBus, backend: Any) -> bool:
        backend.state.playback_state = PlaybackState.IDLE
        return True

    prepare = AsyncMock(side_effect=fake_prepare)
    provider._prepare_backend_for_reclaim = prepare  # type: ignore[method-assign]

    bus, _ = await provider.claim_bus(new_owner_id, [new_owner_id])

    assert bus.source_name == "Connect 1"
    assert bus.owner_id == new_owner_id
    prepare.assert_awaited_once()


async def test_ownerless_paused_backend_without_routes_stop_failure_refuses_claim() -> None:
    """A failed stop of an unrouted paused backend must prevent its reuse."""
    provider = _provider(
        (
            PlaybackState.PAUSED,
            PlaybackState.PLAYING,
        )
    )
    new_owner_id = next(iter(ROOMS))

    prepare = AsyncMock(return_value=False)
    provider._prepare_backend_for_reclaim = prepare  # type: ignore[method-assign]

    with pytest.raises(PlayerCommandFailed, match="left untouched"):
        await provider.claim_bus(new_owner_id, [new_owner_id])

    prepare.assert_awaited_once()
    assert all(bus.owner_id is None for bus in provider._buses)


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


async def test_requested_room_stale_route_on_other_bus_is_reclaimed_before_claim() -> None:
    """A requested room must be detached from another stale bus before bus selection."""
    provider = _provider()
    owner_id = next(iter(ROOMS))

    owner = _set_route(provider, owner_id, "Connect 2")
    turn_off = _install_fake_turn_off(provider)

    bus, _ = await provider.claim_bus(owner_id, [owner_id])

    assert bus.source_name == "Connect 1"
    assert bus.owner_id == owner_id
    turn_off.assert_awaited_once_with(owner)


async def test_requested_room_active_route_on_other_bus_refuses_claim() -> None:
    """An active requested room on another bus must never be disconnected or stolen."""
    provider = _provider(
        (
            PlaybackState.IDLE,
            PlaybackState.PLAYING,
        )
    )
    owner_id = next(iter(ROOMS))

    _set_route(provider, owner_id, "Connect 2")
    owner = provider.get_room_player(owner_id)
    assert owner is not None
    owner.state.playback_state = PlaybackState.PLAYING

    turn_off = AsyncMock()
    provider.turn_off_zone = turn_off  # type: ignore[method-assign]

    with pytest.raises(PlayerCommandFailed, match="already routed to Connect 2"):
        await provider.claim_bus(owner_id, [owner_id])

    turn_off.assert_not_awaited()
    assert all(bus.owner_id is None for bus in provider._buses)


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


async def test_intentionally_paused_owned_session_is_reclaimable() -> None:
    """A logical PAUSED session may yield its Connect to a newer play request."""
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


async def test_ownerless_logically_paused_room_is_reclaimable() -> None:
    """A physically routed logical PAUSED room may be displaced on demand."""
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
    turn_off = _install_fake_turn_off(provider)

    async def fake_prepare(bus: MatrixBus, backend: Any) -> bool:
        backend.state.playback_state = PlaybackState.IDLE
        return True

    prepare = AsyncMock(side_effect=fake_prepare)
    provider._prepare_backend_for_reclaim = prepare  # type: ignore[method-assign]

    reclaimed = await provider._reconcile_ownerless_idle_bus_locked(bus)

    assert reclaimed is True
    prepare.assert_awaited_once()
    turn_off.assert_awaited_once_with(player)


async def test_new_request_reclaims_paused_bus_not_playing_bus() -> None:
    """One active stream is protected while the paused Connect is reused."""
    provider = _provider(
        (
            PlaybackState.PLAYING,
            PlaybackState.PAUSED,
        )
    )
    playing_id, paused_id, new_id = list(ROOMS)[:3]

    playing = _set_route(provider, playing_id, "Connect 1")
    playing.state.playback_state = PlaybackState.PLAYING
    provider._buses[0].owner_id = playing_id
    provider._buses[0].last_played_at = 100.0

    paused = _set_route(provider, paused_id, "Connect 2")
    paused.state.playback_state = PlaybackState.PAUSED
    provider._buses[1].owner_id = paused_id
    provider._buses[1].last_played_at = 200.0

    turn_off = _install_fake_turn_off(provider)

    async def fake_prepare(bus: MatrixBus, backend: Any) -> bool:
        backend.state.playback_state = PlaybackState.IDLE
        return True

    provider._prepare_backend_for_reclaim = AsyncMock(  # type: ignore[method-assign]
        side_effect=fake_prepare
    )

    bus, _ = await provider.claim_bus(new_id, [new_id])

    assert bus is provider._buses[1]
    assert provider._buses[0].owner_id == playing_id
    assert provider._buses[1].owner_id == new_id
    turn_off.assert_awaited_once_with(paused)


async def test_two_nonplaying_buses_reclaim_least_recently_playing() -> None:
    """When neither stream is active, the least-recently-playing bus yields."""
    provider = _provider(
        (
            PlaybackState.PAUSED,
            PlaybackState.PAUSED,
        )
    )
    newer_id, older_id, new_id = list(ROOMS)[:3]

    newer = _set_route(provider, newer_id, "Connect 1")
    newer.state.playback_state = PlaybackState.PAUSED
    provider._buses[0].owner_id = newer_id
    provider._buses[0].last_played_at = 200.0

    older = _set_route(provider, older_id, "Connect 2")
    older.state.playback_state = PlaybackState.PAUSED
    provider._buses[1].owner_id = older_id
    provider._buses[1].last_played_at = 100.0

    turn_off = _install_fake_turn_off(provider)

    async def fake_prepare(bus: MatrixBus, backend: Any) -> bool:
        backend.state.playback_state = PlaybackState.IDLE
        return True

    provider._prepare_backend_for_reclaim = AsyncMock(  # type: ignore[method-assign]
        side_effect=fake_prepare
    )

    bus, _ = await provider.claim_bus(new_id, [new_id])

    assert bus is provider._buses[1]
    assert provider._buses[0].owner_id == newer_id
    assert provider._buses[1].owner_id == new_id
    turn_off.assert_awaited_once_with(older)


def test_mark_bus_playing_updates_lru_timestamp() -> None:
    """Observed real playback updates the bus activity used for LRU selection."""
    provider = _provider()
    owner_id = next(iter(ROOMS))
    bus = provider._buses[0]
    bus.owner_id = owner_id

    assert bus.last_played_at == 0.0

    provider.mark_bus_playing(owner_id)

    assert bus.last_played_at > 0.0


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
    """A grouped follower without its leader on the bus must block cleanup."""
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


async def test_ownerless_paused_group_is_reclaimed_together() -> None:
    """A complete non-playing logical group may yield its ownerless Connect."""
    provider = _provider(
        (
            PlaybackState.PAUSED,
            PlaybackState.PLAYING,
        )
    )
    leader_id, follower_id, new_owner_id = list(ROOMS)[:3]

    leader = _set_route(provider, leader_id, "Connect 1")
    follower = _set_route(provider, follower_id, "Connect 1")
    leader.state.playback_state = PlaybackState.PAUSED
    follower.state.playback_state = PlaybackState.PAUSED
    follower.state.synced_to = leader_id

    turn_off = _install_fake_turn_off(provider)

    async def fake_prepare(bus: MatrixBus, backend: Any) -> bool:
        backend.state.playback_state = PlaybackState.IDLE
        return True

    prepare = AsyncMock(side_effect=fake_prepare)
    provider._prepare_backend_for_reclaim = prepare  # type: ignore[method-assign]

    bus, _ = await provider.claim_bus(new_owner_id, [new_owner_id])

    assert bus is provider._buses[0]
    assert bus.owner_id == new_owner_id
    prepare.assert_awaited_once()
    assert turn_off.await_count == 2
    assert {
        awaited.args[0].player_id
        for awaited in turn_off.await_args_list
    } == {
        leader_id,
        follower_id,
    }


async def test_ownerless_mixed_group_routes_refuse_without_cleanup() -> None:
    """A grouped session sharing a bus with another route remains fail-closed."""
    provider = _provider(
        (
            PlaybackState.IDLE,
            PlaybackState.PLAYING,
        )
    )
    leader_id, follower_id, unrelated_id, new_owner_id = list(ROOMS)[:4]

    leader = _set_route(provider, leader_id, "Connect 1")
    follower = _set_route(provider, follower_id, "Connect 1")
    unrelated = _set_route(provider, unrelated_id, "Connect 1")

    leader.state.playback_state = PlaybackState.PAUSED
    follower.state.playback_state = PlaybackState.PAUSED
    unrelated.state.playback_state = PlaybackState.IDLE
    follower.state.synced_to = leader_id

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


async def test_poll_preserves_intentional_pause_while_backend_still_playing() -> None:
    """A Sonos PLAYING report during STOP must not cancel an explicit pause."""
    player_id = next(iter(ROOMS))

    state = await _poll_playback_state(
        PlaybackState.PLAYING,
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


async def test_triad_pause_bypasses_generic_backend_source_guard() -> None:
    """Pause must call the Sonos backend directly instead of MA's source guard."""
    provider = _provider(
        (
            PlaybackState.PLAYING,
            PlaybackState.IDLE,
        )
    )
    player_id = next(iter(ROOMS))
    bus = provider._buses[0]
    bus.owner_id = player_id

    backend = provider.mass.players.get_player(bus.backend_player_id)
    assert backend is not None

    generic_pause = AsyncMock(
        side_effect=AssertionError("generic pause handler must not be called")
    )
    provider.mass.players._handle_cmd_pause = generic_pause
    provider.mass.players.get_player_lock = MagicMock(
        return_value=asyncio.Lock()
    )

    player = TriadMatrixTestPlayer.__new__(TriadMatrixTestPlayer)
    player._provider = provider
    player.mass = provider.mass
    player._player_id = player_id
    cleanup_audio = AsyncMock()
    queue_data = SimpleNamespace(session_id="session-1")
    provider.mass.player_queues = SimpleNamespace(
        queue_data_or_none=MagicMock(return_value=queue_data),
        _cleanup_queue_audio_data=cleanup_audio,
    )
    clear_processing = MagicMock()
    close_superseded_item_streams = MagicMock()
    provider.mass.streams = SimpleNamespace(
        audio_processing=SimpleNamespace(
            clear=clear_processing,
        ),
        close_superseded_item_streams=close_superseded_item_streams,
    )
    provider.mass.cancel_task = MagicMock()
    provider.mass.cancel_timer = MagicMock()

    async def assert_session_detached_before_backend_stop() -> None:
        # The logical transport must already be frozen before Sonos STOP can
        # reset its renderer clock. Otherwise flow reconciliation can rewind
        # the queue to the first item during the pause transition.
        assert player._intentional_pause is True
        assert player._attr_playback_state == PlaybackState.PAUSED
        assert player._transport_started_at is None
        assert player._transport_elapsed_origin is None

        # The old flow session must also already be invalid so a trailing Sonos
        # GET cannot restart the flow at its original start item.
        assert queue_data.session_id is None
        close_superseded_item_streams.assert_called_once_with(
            player_id,
            None,
        )

        # Buffer/provider cleanup deliberately follows the physical stop.
        clear_processing.assert_not_called()
        cleanup_audio.assert_not_awaited()

    backend.pause = AsyncMock(
        side_effect=assert_session_detached_before_backend_stop
    )

    player._intentional_pause = False
    player._attr_playback_state = PlaybackState.PLAYING
    player._transport_started_at = 1000.0
    player._transport_elapsed_origin = 8.0
    cast("Any", player).update_state = MagicMock()

    await player.pause()

    backend.pause.assert_awaited_once_with()
    generic_pause.assert_not_awaited()

    assert queue_data.session_id is None
    close_superseded_item_streams.assert_called_once_with(
        player_id,
        None,
    )
    clear_processing.assert_called_once_with(
        player_id,
        "session-1",
    )
    cleanup_audio.assert_awaited_once_with(
        player_id,
        "session-1",
    )

    provider.mass.cancel_task.assert_any_call(
        f"preload_next_item_{player_id}"
    )
    provider.mass.cancel_timer.assert_called_once_with(
        f"enqueue_next_item_{player_id}"
    )
    provider.mass.cancel_task.assert_any_call(
        f"enqueue_next_item_{player_id}"
    )
    provider.mass.cancel_task.assert_any_call(
        f"prepare_next_audio_buffer_{player_id}"
    )

    assert player._transport_started_at is None
    assert player._transport_elapsed_origin is None
    assert player._intentional_pause is True
    assert player._attr_playback_state == PlaybackState.PAUSED


async def test_poll_tracks_logical_queue_now_playing_metadata() -> None:
    """Poll must follow the MA queue item rather than stale backend metadata."""
    provider = _provider(
        (
            PlaybackState.PLAYING,
            PlaybackState.IDLE,
        )
    )
    player_id = next(iter(ROOMS))
    room = ROOMS[player_id]
    bus = provider._buses[0]
    bus.owner_id = player_id

    backend = provider.mass.players.get_player(bus.backend_player_id)
    assert backend is not None
    # Backend transport position is the authoritative flow-stream clock.
    backend.state.elapsed_time = 73.25
    backend.state.elapsed_time_last_updated = 888.0

    current_item = SimpleNamespace(
        queue_id=player_id,
        queue_item_id="current-item",
    )
    current_media = SimpleNamespace(
        source_id=player_id,
        queue_item_id="current-item",
        title="Current Track",
        elapsed_time=None,
        elapsed_time_last_updated=None,
    )
    queue = SimpleNamespace(
        active=True,
        ended=False,
        current_item=current_item,
        elapsed_time=40.0,
        corrected_elapsed_time=42.5,
        elapsed_time_last_updated=1234.0,
    )
    queue_data = SimpleNamespace(session_id="session-1")
    media_from_item = AsyncMock(return_value=current_media)

    provider.mass.player_queues = SimpleNamespace(
        get=lambda queue_id: queue if queue_id == player_id else None,
        queue_data_or_none=lambda queue_id: (
            queue_data if queue_id == player_id else None
        ),
        flow_queue_exhausted=lambda queue_id, session_id: False,
        player_media_from_queue_item=media_from_item,
    )

    provider.get_zone_state = AsyncMock(
        return_value={
            "entity_id": room["entity_id"],
            "state": "on",
            "attributes": {
                "volume_level": 0.25,
                "is_volume_muted": False,
            },
        }
    )

    player = TriadMatrixTestPlayer.__new__(TriadMatrixTestPlayer)
    player._provider = provider
    player.mass = provider.mass
    player._player_id = player_id
    player.zone_entity = str(room["entity_id"])
    player._intentional_pause = False
    player._attr_current_media = SimpleNamespace(
        source_id=player_id,
        queue_item_id="stale-item",
        title="Stale Track",
    )
    player._attr_playback_state = PlaybackState.PLAYING
    cast("Any", player).update_state = MagicMock()

    await player.poll()

    media_from_item.assert_awaited_once_with(current_item)
    assert player._attr_current_media is current_media
    assert player._attr_current_media.title == "Current Track"
    # Current-media position is track-relative and comes from the logical queue.
    assert player._attr_current_media.elapsed_time == 40
    assert player._attr_current_media.elapsed_time_last_updated == 1234.0

    # Player elapsed is flow-stream-relative and must come from the actual renderer.
    assert player._attr_elapsed_time == 73.25
    assert player._attr_elapsed_time_last_updated == 888.0

    # A newly started transport must reject an elapsed anchor that predates
    # that transport. It must also remain logically non-playing so neither MA
    # nor HA can extrapolate an elapsed timer during renderer startup.
    player._attr_playback_state = PlaybackState.IDLE
    player._transport_started_at = 1000.0
    player._transport_elapsed_origin = None
    backend.state.elapsed_time = 9999.0
    backend.state.elapsed_time_last_updated = 999.0

    await player.poll()

    assert player._attr_playback_state == PlaybackState.IDLE
    assert player._attr_elapsed_time == 0.0
    assert player._transport_started_at == 1000.0

    # Even a fresh timestamp is not sufficient while the renderer position is
    # still zero: Sonos can report PLAYING before audible playback begins.
    backend.state.elapsed_time = 0.0
    backend.state.elapsed_time_last_updated = 1001.0

    await player.poll()

    assert player._attr_playback_state == PlaybackState.IDLE
    assert player._attr_elapsed_time == 0.0
    assert player._transport_started_at == 1000.0

    # Sonos may already report several seconds of renderer time when audible
    # playback finally starts. That first positive value becomes transport zero
    # instead of being exposed as an immediate elapsed-time jump.
    old_queue_anchor = queue.elapsed_time_last_updated
    backend.state.elapsed_time = 8.25
    backend.state.elapsed_time_last_updated = 1002.0

    await player.poll()

    assert player._attr_playback_state == PlaybackState.PLAYING
    assert player._attr_elapsed_time == 0.0
    assert player._transport_started_at is None
    assert player._transport_elapsed_origin == 8.25
    assert queue.elapsed_time_last_updated > old_queue_anchor

    # Subsequent renderer progress is measured relative to the fixed transport
    # origin, so only genuinely post-start playback time is exposed.
    backend.state.elapsed_time = 10.25
    backend.state.elapsed_time_last_updated = 1004.0

    await player.poll()

    assert player._attr_playback_state == PlaybackState.PLAYING
    assert player._attr_elapsed_time == 2.0
    assert player._attr_elapsed_time_last_updated == 1004.0



async def test_backend_queue_media_is_forced_to_triad_flow_stream() -> None:
    """Queue tracks handed to hidden Sonos must use the logical Triad flow URL."""
    player_id = next(iter(ROOMS))

    resolve_stream_url = AsyncMock(
        return_value=(
            "http://mass.test/flow/session-1/"
            f"{player_id}/item-1/{player_id}.flac"
        )
    )

    player = TriadMatrixTestPlayer.__new__(TriadMatrixTestPlayer)
    player.mass = SimpleNamespace(
        streams=SimpleNamespace(
            resolve_stream_url=resolve_stream_url,
        )
    )
    player._player_id = player_id

    original = PlayerMedia(
        uri="spotify://track/example",
        media_type=MediaType.TRACK,
        title="Track One",
        artist="Artist One",
        album="Album One",
        source_id=player_id,
        queue_item_id="item-1",
        queue_session_id="session-1",
    )

    backend_media = await player._media_for_backend(original)

    resolve_stream_url.assert_awaited_once_with(player_id, original)
    assert backend_media is not original
    assert backend_media.media_type == MediaType.FLOW_STREAM
    assert "/flow/" in backend_media.uri
    assert "/single/" not in backend_media.uri
    assert backend_media.source_id == player_id
    # Critical Sonos transport requirement: if queue_item_id is present together
    # with source_id, Sonos enters its cloud-queue path and resolves /single/.
    assert backend_media.queue_item_id is None
    assert backend_media.queue_session_id == "session-1"
    assert backend_media.title == "Track One"
    assert backend_media.artist == "Artist One"
    assert backend_media.album == "Album One"
    assert backend_media.custom_data == {
        "triad_logical_player_id": player_id,
        "triad_start_queue_item_id": "item-1",
    }

    # This mirrors the Sonos play_media cloud-queue gate. A Triad flow handoff
    # must never satisfy it.
    assert not (
        backend_media.source_id
        and backend_media.queue_item_id
    )



async def test_triad_play_resumes_queue_instead_of_hidden_backend() -> None:
    """A paused Triad session must rebuild its MA flow stream on resume."""
    provider = _provider(
        (
            PlaybackState.IDLE,
            PlaybackState.IDLE,
        )
    )
    player_id = next(iter(ROOMS))

    # The paused queue may have yielded its old physical Connect. Resuming must
    # still reach the queue controller so the rebuilt flow can claim a bus.
    assert provider.get_bus_for_owner(player_id) is None

    queue_resume = AsyncMock()
    provider.mass.player_queues = SimpleNamespace(
        resume=queue_resume,
    )

    generic_backend_play = AsyncMock()
    provider.mass.players._handle_cmd_play = generic_backend_play

    player = TriadMatrixTestPlayer.__new__(TriadMatrixTestPlayer)
    player._provider = provider
    player.mass = provider.mass
    player._player_id = player_id
    player._intentional_pause = True
    player._attr_playback_state = PlaybackState.PAUSED
    cast("Any", player).update_state = MagicMock()

    await player.play()

    queue_resume.assert_awaited_once_with(player_id)
    generic_backend_play.assert_not_awaited()
    assert player._intentional_pause is False
    # Resume rebuilds the flow transport, but the logical player must remain
    # paused until the hidden Sonos renderer actually advances.
    assert player._attr_playback_state == PlaybackState.PAUSED


async def test_triad_pause_is_not_auto_stopped_by_queue_watchdog() -> None:
    """A deliberately paused Triad queue must keep its reserved session alive."""
    queue_id = next(iter(ROOMS))

    queue = SimpleNamespace(
        active=True,
        state=PlaybackState.PLAYING,
        corrected_elapsed_time=37.8,
        resume_pos=0,
    )
    queue_data = SimpleNamespace(
        queue=queue,
        transitioning=False,
    )

    logical_player = SimpleNamespace(
        state=SimpleNamespace(
            playback_state=PlaybackState.PAUSED,
        ),
        auto_stop_paused_queue=False,
        extra_data={},
    )

    handle_pause = AsyncMock()
    create_task = MagicMock()

    controller = PlayerQueuesController.__new__(PlayerQueuesController)
    controller._queue_data = {
        queue_id: queue_data,
    }
    controller.mass = SimpleNamespace(
        cancel_timer=MagicMock(),
        players=SimpleNamespace(
            _handle_cmd_pause=handle_pause,
            get_player=lambda player_id: (
                logical_player if player_id == queue_id else None
            ),
        ),
        create_task=create_task,
    )
    controller._check_player_permission = MagicMock()

    await controller.pause(queue_id)

    handle_pause.assert_awaited_once_with(queue_id)
    assert queue.resume_pos == 37
    create_task.assert_not_called()



async def test_ad_hoc_leader_transfer_preserves_remaining_group_and_queue() -> None:
    """Removing a playing leader must move its queue and preserve remaining rooms."""
    leader_id = "triad_test_kitchen"
    new_leader_id = "triad_test_breakfast_room"
    remaining_id = "triad_test_dining_room"

    leader = SimpleNamespace(
        player_id=leader_id,
        name="Kitchen",
    )
    active_queue = SimpleNamespace(
        state=PlaybackState.PLAYING,
    )

    events: list[tuple[Any, ...]] = []

    async def transfer_queue(
        source_queue_id: str,
        target_queue_id: str,
        auto_play: bool | None = None,
    ) -> None:
        events.append(
            ("transfer", source_queue_id, target_queue_id, auto_play)
        )

    async def set_members(
        target_player: str,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        events.append(
            (
                "group",
                target_player,
                tuple(player_ids_to_add or []),
                tuple(player_ids_to_remove or []),
            )
        )

    async def resume(queue_id: str) -> None:
        events.append(("resume", queue_id))

    controller = PlayerController.__new__(PlayerController)
    controller.logger = MagicMock()
    controller.mass = SimpleNamespace(
        player_queues=SimpleNamespace(
            transfer_queue=transfer_queue,
            resume=resume,
        )
    )
    controller.get_active_queue = MagicMock(return_value=active_queue)
    controller._select_ad_hoc_leader = MagicMock(
        return_value=new_leader_id
    )
    controller.cmd_set_members = set_members

    await controller._transfer_ad_hoc_leadership(
        leader,
        [new_leader_id, remaining_id],
    )

    controller._select_ad_hoc_leader.assert_called_once_with(
        leader,
        [new_leader_id, remaining_id],
    )

    assert events == [
        (
            "transfer",
            leader_id,
            new_leader_id,
            False,
        ),
        (
            "group",
            new_leader_id,
            (remaining_id,),
            (),
        ),
        (
            "resume",
            new_leader_id,
        ),
    ]
