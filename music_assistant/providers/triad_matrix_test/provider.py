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

# Deliberately only two low-disruption rooms for the first experiment.
ROOMS = {
    "triad_test_master_bedroom": {
        "name": "Triad Test - Master Bedroom",
        "entity_id": "media_player.triad_master_bedroom",
        "output": 3,
    },
    "triad_test_master_bath": {
        "name": "Triad Test - Master Bath",
        "entity_id": "media_player.triad_master_bath",
        "output": 2,
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

    async def _verified_media_player_command(
        self,
        *,
        entity_id: str,
        service: str,
        service_data: dict[str, Any] | None,
        verifier: Any,
        description: str,
        attempts: int = 5,
    ) -> dict[str, Any]:
        """Run an HA media-player command and verify its resulting state.

        The Triad HA integration intentionally swallows transient AMS protocol
        errors at the entity layer. A successful HA service call therefore does
        not prove that the matrix accepted the command. Verification against the
        entity's cached state is required before the prototype proceeds.
        """
        last_state: dict[str, Any] | None = None
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                await self.call_media_player_service(
                    entity_id,
                    service,
                    service_data,
                )
                await asyncio.sleep(0.35)

                last_state = await self.get_zone_state(entity_id)

                if verifier(last_state):
                    if attempt > 1:
                        self.logger.info(
                            "TRIAD TEST VERIFY: %s succeeded on attempt %d",
                            description,
                            attempt,
                        )
                    return last_state

                self.logger.warning(
                    "TRIAD TEST VERIFY: %s not confirmed on attempt %d/%d",
                    description,
                    attempt,
                    attempts,
                )

            except Exception as err:
                last_error = err
                self.logger.warning(
                    "TRIAD TEST VERIFY: %s raised on attempt %d/%d: %s",
                    description,
                    attempt,
                    attempts,
                    err,
                )

            if attempt < attempts:
                await asyncio.sleep(0.35)

        detail = (
            f"last_state={last_state!r}"
            if last_state is not None
            else f"last_error={last_error!r}"
        )

        raise PlayerCommandFailed(
            f"Triad command could not be verified after {attempts} attempts: "
            f"{description}; {detail}"
        )

    async def route_zone_to_bus(self, player: TriadMatrixTestPlayer) -> None:
        """Route one Triad output to Connect 1 and verify the route."""
        self.logger.info(
            "TRIAD TEST ROUTE: %s (output %s) -> %s",
            player.display_name,
            player.output_number,
            TRIAD_SOURCE_NAME,
        )

        await self._verified_media_player_command(
            entity_id=player.zone_entity,
            service="select_source",
            service_data={"source": TRIAD_SOURCE_NAME},
            verifier=lambda state: (
                state.get("state") not in ("off", "unavailable", "unknown", None)
                and (state.get("attributes") or {}).get("source")
                == TRIAD_SOURCE_NAME
            ),
            description=(
                f"route {player.display_name} output "
                f"{player.output_number} to {TRIAD_SOURCE_NAME}"
            ),
        )

    async def turn_off_zone(self, player: TriadMatrixTestPlayer) -> None:
        """Disconnect exactly one Triad output and verify it is off."""
        self.logger.info(
            "TRIAD TEST OFF: %s (output %s)",
            player.display_name,
            player.output_number,
        )

        await self._verified_media_player_command(
            entity_id=player.zone_entity,
            service="turn_off",
            service_data=None,
            verifier=lambda state: state.get("state") == "off",
            description=(
                f"disconnect {player.display_name} output "
                f"{player.output_number}"
            ),
        )

    async def set_zone_volume(
        self,
        player: TriadMatrixTestPlayer,
        volume_level: int,
    ) -> None:
        """Set one Triad output volume and verify the resulting level."""
        expected = max(0, min(100, volume_level)) / 100

        await self._verified_media_player_command(
            entity_id=player.zone_entity,
            service="volume_set",
            service_data={"volume_level": expected},
            verifier=lambda state: (
                isinstance(
                    (state.get("attributes") or {}).get("volume_level"),
                    (int, float),
                )
                and abs(
                    float(
                        (state.get("attributes") or {}).get("volume_level")
                    )
                    - expected
                )
                < 0.005
            ),
            description=(
                f"set {player.display_name} output "
                f"{player.output_number} volume to {volume_level}%"
            ),
        )

    async def set_zone_mute(
        self,
        player: TriadMatrixTestPlayer,
        muted: bool,
    ) -> None:
        """Set one Triad output mute state and verify it."""
        await self._verified_media_player_command(
            entity_id=player.zone_entity,
            service="volume_mute",
            service_data={"is_volume_muted": muted},
            verifier=lambda state: (
                (state.get("attributes") or {}).get("is_volume_muted")
                is muted
            ),
            description=(
                f"set {player.display_name} output "
                f"{player.output_number} muted={muted}"
            ),
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
