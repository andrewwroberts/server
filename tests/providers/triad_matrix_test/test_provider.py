"""Tests for the Triad matrix prototype provider."""

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


def _backend(player_id: str, state: PlaybackState = PlaybackState.IDLE) -> SimpleNamespace:
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
            players=SimpleNamespace(get_player=backends.get),
        ),
    )
    provider.logger = MagicMock()
    provider._get_zone_states = AsyncMock(  # type: ignore[method-assign]
        return_value={
            str(room["entity_id"]): {
                "entity_id": room["entity_id"],
                "state": "off",
                "attributes": {"source": None},
            }
            for room in ROOMS.values()
        }
    )
    return provider


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


async def test_two_claims_then_third_is_refused() -> None:
    """Two independent owners should reserve both buses without bus stealing."""
    provider = _provider()
    first_id, second_id, third_id = list(ROOMS)[:3]

    first_bus, _ = await provider.claim_bus(first_id, [first_id])
    second_bus, _ = await provider.claim_bus(second_id, [second_id])

    assert first_bus.source_name == "Connect 1"
    assert second_bus.source_name == "Connect 2"

    with pytest.raises(PlayerCommandFailed, match="left untouched"):
        await provider.claim_bus(third_id, [third_id])

    assert provider.get_bus_for_owner(first_id) is first_bus
    assert provider.get_bus_for_owner(second_id) is second_bus
    assert provider.get_bus_for_owner(third_id) is None


async def test_claim_skips_externally_busy_buses() -> None:
    """Playback and an existing matrix route should both protect a bus."""
    provider = _provider((PlaybackState.PLAYING, PlaybackState.IDLE))
    owner_id, routed_id = list(ROOMS)[:2]
    routed_player = provider.get_room_player(routed_id)
    assert routed_player is not None
    provider._get_zone_states.return_value[routed_player.zone_entity][  # type: ignore[attr-defined]
        "attributes"
    ]["source"] = "Connect 2"

    with pytest.raises(PlayerCommandFailed, match="No free Triad music source"):
        await provider.claim_bus(owner_id, [owner_id])

    assert all(bus.owner_id is None for bus in provider._buses)


async def test_release_requires_clear_matrix_routes() -> None:
    """A bus should remain reserved until every room is disconnected."""
    provider = _provider()
    owner_id = next(iter(ROOMS))
    bus, _ = await provider.claim_bus(owner_id, [owner_id])
    owner = provider.get_room_player(owner_id)
    assert owner is not None
    states = provider._get_zone_states.return_value  # type: ignore[attr-defined]
    states[owner.zone_entity]["attributes"]["source"] = bus.source_name

    with pytest.raises(PlayerCommandFailed, match="Refusing to release"):
        await provider.release_bus(owner_id)

    assert provider.get_bus_for_owner(owner_id) is bus

    states[owner.zone_entity]["attributes"]["source"] = None
    await provider.release_bus(owner_id)

    assert provider.get_bus_for_owner(owner_id) is None
