"""Provider implementation for the Triad AMS matrix prototype."""

from __future__ import annotations

import asyncio
from typing import Any, TYPE_CHECKING

from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.models.player_provider import PlayerProvider

from .player import TriadMatrixTestPlayer

if TYPE_CHECKING:
    from music_assistant.models.player import Player


# Physical transport:
#
# Connect 1
#   Sonos ID: RINCON_B8E937997EE801400
#   Home Assistant: media_player.connect_1
#   Triad AMS input: 3
#
BACKEND_PLAYER_ID = "RINCON_B8E937997EE801400"
TRIAD_SOURCE_NAME = "Connect 1"

# Deliberately only two rooms for the first experiment.
ROOMS = {
    "triad_test_kitchen": {
        "name": "Triad Test - Kitchen",
        "entity_id": "media_player.triad_kitchen",
        "output": 4,
    },
    "triad_test_dining_room": {
        "name": "Triad Test - Dining Room",
        "entity_id": "media_player.triad_dining_room",
        "output": 6,
    },
}


class TriadMatrixTestProvider(PlayerProvider):
    """Experimental provider representing Triad outputs as MA room players."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize provider."""
        super().__init__(*args, **kwargs)
        self._players_by_id: dict[str, TriadMatrixTestPlayer] = {}
        self._bus_owner: str | None = None
        self._bus_lock = asyncio.Lock()

    async def get_config_entries(self) -> tuple:
        """Return provider configuration entries."""
        return ()

    async def handle_async_init(self) -> None:
        """Initialize runtime state."""
        self.logger.info(
            "Triad prototype initialized: backend=%s source=%s",
            BACKEND_PLAYER_ID,
            TRIAD_SOURCE_NAME,
        )

    async def loaded_in_mass(self) -> None:
        """Register the two experimental room players."""
        await self.discover_players()

    async def unload(self, is_removed: bool = False) -> None:
        """Unload the provider."""
        for player in list(self._players_by_id.values()):
            await self.mass.players.unregister(player.player_id)
        self._players_by_id.clear()

    async def discover_players(self) -> None:
        """Register Kitchen and Dining Room."""
        for player_id, room in ROOMS.items():
            if player_id in self._players_by_id:
                continue

            player = TriadMatrixTestPlayer(
                provider=self,
                player_id=player_id,
                name=str(room["name"]),
                zone_entity=str(room["entity_id"]),
                output_number=int(room["output"]),
            )
            self._players_by_id[player_id] = player
            await self.mass.players.register(player)

            self.logger.info(
                "Registered Triad prototype room %s -> %s (output %s)",
                player.display_name,
                player.zone_entity,
                player.output_number,
            )

    @property
    def bus_owner(self) -> str | None:
        """Return the logical room currently owning Connect 1."""
        return self._bus_owner

    def get_room_player(
        self,
        player_id: str,
    ) -> TriadMatrixTestPlayer | None:
        """Return one of this provider's logical room players."""
        return self._players_by_id.get(player_id)

    def get_backend_player(
        self,
        required: bool = True,
    ) -> Player | None:
        """Return the native MA Sonos Connect player."""
        player = self.mass.players.get_player(BACKEND_PLAYER_ID)

        if player is not None and player.state.available:
            return player

        if required:
            raise PlayerCommandFailed(
                "Triad prototype requires native Sonos player "
                f"{BACKEND_PLAYER_ID}, but it is not currently available."
            )

        return None

    def get_hass_provider(self, required: bool = True) -> Any:
        """Return MA's existing Home Assistant provider."""
        provider = self.mass.get_provider("hass")

        if provider is not None and provider.available:
            return provider

        if required:
            raise PlayerCommandFailed(
                "Triad prototype requires the Music Assistant "
                "Home Assistant provider, but it is not available."
            )

        return None

    async def call_media_player_service(
        self,
        entity_id: str,
        service: str,
        service_data: dict[str, Any] | None = None,
    ) -> None:
        """Call a Home Assistant media_player service."""
        hass_provider = self.get_hass_provider()

        kwargs: dict[str, Any] = {
            "domain": "media_player",
            "service": service,
            "target": {"entity_id": entity_id},
        }

        if service_data is not None:
            kwargs["service_data"] = service_data

        await hass_provider.hass.call_service(**kwargs)

    async def get_zone_state(self, entity_id: str) -> dict[str, Any]:
        """Return current state of a Triad HA media_player."""
        hass_provider = self.get_hass_provider()

        states = await hass_provider.get_states(
            entity_ids=[entity_id],
        )

        if not states:
            raise PlayerCommandFailed(
                f"Home Assistant did not return state for {entity_id}."
            )

        return states[0]

    async def route_zone_to_bus(self, player: TriadMatrixTestPlayer) -> None:
        """Route one Triad output to Connect 1 / input 3."""
        self.logger.info(
            "TRIAD TEST ROUTE: %s (output %s) -> %s",
            player.display_name,
            player.output_number,
            TRIAD_SOURCE_NAME,
        )

        # This intentionally matches the sequence already proven in Andrew's
        # Home Assistant routing scripts:
        #   1. turn the Triad output on
        #   2. select "Connect 1"
        await self.call_media_player_service(
            player.zone_entity,
            "turn_on",
        )

        await self.call_media_player_service(
            player.zone_entity,
            "select_source",
            {"source": TRIAD_SOURCE_NAME},
        )

    async def turn_off_zone(self, player: TriadMatrixTestPlayer) -> None:
        """Turn off exactly one Triad output."""
        self.logger.info(
            "TRIAD TEST OFF: %s (output %s)",
            player.display_name,
            player.output_number,
        )

        await self.call_media_player_service(
            player.zone_entity,
            "turn_off",
        )

    async def claim_bus(self, owner_id: str) -> Player:
        """Reserve Connect 1 for one logical MA playback session."""
        async with self._bus_lock:
            backend = self.get_backend_player()
            assert backend is not None

            if self._bus_owner is None:
                if backend.state.playback_state in (
                    PlaybackState.PLAYING,
                    PlaybackState.PAUSED,
                ):
                    raise PlayerCommandFailed(
                        "Triad prototype refused to seize Connect 1 because "
                        f"{backend.display_name} is already "
                        f"{backend.state.playback_state.value}."
                    )

                self._bus_owner = owner_id

                self.logger.info(
                    "TRIAD TEST BUS CLAIM: %s claimed %s",
                    owner_id,
                    backend.display_name,
                )

            elif self._bus_owner != owner_id:
                owner = self._players_by_id.get(self._bus_owner)
                owner_name = (
                    owner.display_name
                    if owner is not None
                    else self._bus_owner
                )

                raise PlayerCommandFailed(
                    "Triad prototype has only one test bus. "
                    f"Connect 1 is already owned by {owner_name}."
                )

            return backend

    async def release_bus(self, owner_id: str) -> None:
        """Release Connect 1 if owned by this logical player."""
        async with self._bus_lock:
            if self._bus_owner != owner_id:
                return

            self.logger.info(
                "TRIAD TEST BUS RELEASE: %s released Connect 1",
                owner_id,
            )
            self._bus_owner = None
