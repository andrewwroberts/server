"""Provider implementation for the Triad AMS matrix prototype."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypedDict

from music_assistant_models.enums import PlaybackState, PlayerFeature
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.controllers.players.constants import PlayerLockPurpose
from music_assistant.models.player_provider import PlayerProvider

from .player import TriadMatrixTestPlayer

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry

    from music_assistant.models.player import Player


@dataclass(frozen=True, slots=True)
class BusDefinition:
    """Describe one physical Sonos-to-Triad source bus."""

    source_name: str
    backend_player_id: str
    backend_entity_id: str
    triad_input: int


@dataclass(slots=True)
class MatrixBus:
    """Track the runtime owner of one physical source bus."""

    definition: BusDefinition
    owner_id: str | None = None

    @property
    def source_name(self) -> str:
        """Return the Home Assistant source name for this bus."""
        return self.definition.source_name

    @property
    def backend_player_id(self) -> str:
        """Return the native Music Assistant backend player ID."""
        return self.definition.backend_player_id


class RoomDefinition(TypedDict):
    """Describe one logical Triad room."""

    name: str
    entity_id: str
    output: int


BUS_DEFINITIONS = (
    BusDefinition(
        source_name="Connect 1",
        backend_player_id="RINCON_B8E937997EE801400",
        backend_entity_id="media_player.connect_1",
        triad_input=3,
    ),
    BusDefinition(
        source_name="Connect 2",
        backend_player_id="RINCON_B8E937997EEE01400",
        backend_entity_id="media_player.connect_2",
        triad_input=4,
    ),
)

ROOMS: dict[str, RoomDefinition] = {
    "triad_test_master_shower": {
        "name": "Master Shower",
        "entity_id": "media_player.triad_master_shower",
        "output": 1,
    },
    "triad_test_master_bath": {
        "name": "Master Bath",
        "entity_id": "media_player.triad_master_bath",
        "output": 2,
    },
    "triad_test_master_bedroom": {
        "name": "Master Bedroom",
        "entity_id": "media_player.triad_master_bedroom",
        "output": 3,
    },
    "triad_test_kitchen": {
        "name": "Kitchen",
        "entity_id": "media_player.triad_kitchen",
        "output": 4,
    },
    "triad_test_family_room": {
        "name": "Family Room",
        "entity_id": "media_player.triad_family_room",
        "output": 5,
    },
    "triad_test_dining_room": {
        "name": "Dining Room",
        "entity_id": "media_player.triad_dining_room",
        "output": 6,
    },
    "triad_test_library": {
        "name": "Library",
        "entity_id": "media_player.triad_library",
        "output": 7,
    },
    "triad_test_breakfast_room": {
        "name": "Breakfast Room",
        "entity_id": "media_player.triad_breakfast_room",
        "output": 8,
    },
    "triad_test_theater_room": {
        "name": "Theater Room",
        "entity_id": "media_player.triad_theater_room",
        "output": 9,
    },
    "triad_test_outdoor_eating_area": {
        "name": "Outdoor Eating Area",
        "entity_id": "media_player.triad_outdoor_eating_area",
        "output": 10,
    },
    "triad_test_fire_pit": {
        "name": "Fire Pit",
        "entity_id": "media_player.triad_fire_pit",
        "output": 11,
    },
    "triad_test_basement_weight_room": {
        "name": "Basement Weight Room",
        "entity_id": "media_player.triad_basement_weight_room",
        "output": 12,
    },
    "triad_test_basement_rec_room": {
        "name": "Basement Rec Room",
        "entity_id": "media_player.triad_basement_rec_room",
        "output": 13,
    },
}


class TriadMatrixTestProvider(PlayerProvider):
    """Experimental provider representing Triad outputs as MA room players."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize provider."""
        super().__init__(*args, **kwargs)
        self._players_by_id: dict[str, TriadMatrixTestPlayer] = {}
        self._buses = [MatrixBus(definition) for definition in BUS_DEFINITIONS]
        self._bus_lock = asyncio.Lock()

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return provider configuration entries."""
        return ()

    async def handle_async_init(self) -> None:
        """Initialize runtime state."""
        self.logger.info(
            "Triad prototype initialized with buses: %s",
            ", ".join(f"{bus.source_name}={bus.backend_player_id}" for bus in self._buses),
        )

    async def loaded_in_mass(self) -> None:
        """Register the logical Triad room players."""
        await self.discover_players()

    async def unload(self, is_removed: bool = False) -> None:
        """Unload the provider."""
        for player in list(self._players_by_id.values()):
            await self.mass.players.unregister(player.player_id)
        self._players_by_id.clear()
        for bus in self._buses:
            bus.owner_id = None

    async def discover_players(self) -> None:
        """Register all configured Triad rooms."""
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

    def get_room_player(
        self,
        player_id: str,
    ) -> TriadMatrixTestPlayer | None:
        """Return one of this provider's logical room players."""
        return self._players_by_id.get(player_id)

    def get_bus_for_owner(self, owner_id: str) -> MatrixBus | None:
        """Return the source bus reserved by a logical room leader."""
        return next(
            (bus for bus in self._buses if bus.owner_id == owner_id),
            None,
        )

    def get_backend_player(
        self,
        bus: MatrixBus,
        required: bool = True,
    ) -> Player | None:
        """Return the native Music Assistant renderer for a source bus."""
        player = self.mass.players.get_player(bus.backend_player_id)

        if player is not None and player.state.available:
            return player

        if required:
            raise PlayerCommandFailed(
                f"Triad {bus.source_name} requires native Sonos player "
                f"{bus.backend_player_id}, but it is not currently available."
            )

        return None

    def has_available_backend(self) -> bool:
        """Return whether at least one Sonos source bus is available."""
        return any(self.get_backend_player(bus, required=False) is not None for bus in self._buses)

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
        states = await self._get_zone_states([entity_id])

        if entity_id not in states:
            raise PlayerCommandFailed(f"Home Assistant did not return state for {entity_id}.")

        return states[entity_id]

    async def claim_bus(
        self,
        owner_id: str,
        member_ids: list[str],
    ) -> tuple[MatrixBus, Player]:
        """
        Reserve one idle source bus for a logical playback session.

        :param owner_id: Logical room leader that owns the playback session.
        :param member_ids: Logical rooms that will initially hear the session.
        """
        async with self._bus_lock:
            if bus := self.get_bus_for_owner(owner_id):
                backend = self.get_backend_player(bus)
                assert backend is not None
                return bus, backend

            zone_entities = [player.zone_entity for player in self._players_by_id.values()]
            states = await self._get_zone_states(zone_entities)
            if missing := set(zone_entities) - states.keys():
                raise PlayerCommandFailed(
                    "Home Assistant did not return every Triad room state; "
                    f"no source bus was claimed. Missing: {', '.join(sorted(missing))}."
                )
            requested_members = set(member_ids)
            unavailable: list[str] = []
            busy: list[str] = []

            for bus in self._buses:
                if bus.owner_id is not None:
                    owner = self.get_room_player(bus.owner_id)
                    owner_name = owner.display_name if owner else bus.owner_id
                    busy.append(f"{bus.source_name} is reserved by {owner_name}")
                    continue

                backend = self.get_backend_player(bus, required=False)
                if backend is None:
                    unavailable.append(f"{bus.source_name} is unavailable")
                    continue

                if backend.state.playback_state != PlaybackState.IDLE:
                    busy.append(
                        f"{bus.source_name} is already {backend.state.playback_state.value}"
                    )
                    continue

                routed_elsewhere = [
                    player.display_name
                    for player_id, player in self._players_by_id.items()
                    if player_id not in requested_members
                    and (states.get(player.zone_entity, {}).get("attributes") or {}).get("source")
                    == bus.source_name
                ]
                if routed_elsewhere:
                    busy.append(
                        f"{bus.source_name} is already routed to {', '.join(routed_elsewhere)}"
                    )
                    continue

                bus.owner_id = owner_id
                self.logger.info(
                    "TRIAD BUS CLAIM: %s claimed %s (%s)",
                    owner_id,
                    bus.source_name,
                    backend.display_name,
                )
                return bus, backend

            detail = "; ".join([*busy, *unavailable])
            raise PlayerCommandFailed(
                "No free Triad music source is available; existing playback "
                f"and routes were left untouched. {detail}"
            )

    async def release_bus(self, owner_id: str) -> None:
        """Release a source bus after verifying no Triad room remains routed to it."""
        async with self._bus_lock:
            bus = self.get_bus_for_owner(owner_id)
            if bus is None:
                return

            zone_entities = [player.zone_entity for player in self._players_by_id.values()]
            states = await self._get_zone_states(zone_entities)
            if missing := set(zone_entities) - states.keys():
                raise PlayerCommandFailed(
                    f"Refusing to release {bus.source_name} without every Triad "
                    f"room state. Missing: {', '.join(sorted(missing))}."
                )
            routed_rooms = [
                player.display_name
                for player in self._players_by_id.values()
                if (states.get(player.zone_entity, {}).get("attributes") or {}).get("source")
                == bus.source_name
            ]
            if routed_rooms:
                raise PlayerCommandFailed(
                    f"Refusing to release {bus.source_name} while it remains "
                    f"routed to {', '.join(routed_rooms)}."
                )

            self.logger.info(
                "TRIAD BUS RELEASE: %s released %s",
                owner_id,
                bus.source_name,
            )
            bus.owner_id = None

    async def prepare_backend(self, bus: MatrixBus, backend: Player) -> None:
        """Set a Sonos Connect to the fixed line-level source volume."""
        missing = {
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
        } - backend.supported_features
        if missing:
            raise PlayerCommandFailed(
                f"{bus.source_name} cannot be fixed at source level because "
                f"{backend.display_name} lacks {', '.join(x.name for x in missing)}."
            )

        async with self.mass.players.get_player_lock(
            backend.player_id,
            PlayerLockPurpose.VOLUME,
        ):
            if backend.state.volume_level != 100:
                await backend.volume_set(100)
            if backend.state.volume_muted is not False:
                await backend.volume_mute(False)

        for _attempt in range(20):
            if backend.state.volume_level == 100 and backend.state.volume_muted is False:
                return
            await asyncio.sleep(0.1)

        raise PlayerCommandFailed(
            f"{bus.source_name} did not confirm fixed source level; "
            f"volume={backend.state.volume_level}, muted={backend.state.volume_muted}."
        )

    async def wait_for_backend_idle(self, bus: MatrixBus, backend: Player) -> None:
        """Wait for a stopped source backend to report idle."""
        for _attempt in range(20):
            if backend.state.playback_state == PlaybackState.IDLE:
                return
            await asyncio.sleep(0.25)

        raise PlayerCommandFailed(
            f"{bus.source_name} did not report idle after stop; "
            f"state={backend.state.playback_state.value}."
        )

    async def route_zone_to_bus(
        self,
        player: TriadMatrixTestPlayer,
        bus: MatrixBus,
    ) -> None:
        """Route one Triad output to a reserved source bus."""
        if bus.owner_id is None:
            raise PlayerCommandFailed(
                f"Cannot route {player.display_name}: {bus.source_name} is not reserved."
            )

        state = await self.get_zone_state(player.zone_entity)
        current_source = (state.get("attributes") or {}).get("source")
        if current_source not in (None, bus.source_name):
            raise PlayerCommandFailed(
                f"Refusing to reroute {player.display_name} from "
                f"{current_source} to {bus.source_name}."
            )

        self.logger.info(
            "TRIAD ROUTE: %s (output %s) -> %s (input %s)",
            player.display_name,
            player.output_number,
            bus.source_name,
            bus.definition.triad_input,
        )

        await self._verified_media_player_command(
            entity_id=player.zone_entity,
            service="select_source",
            service_data={"source": bus.source_name},
            verifier=lambda new_state: (
                new_state.get("state") not in ("off", "unavailable", "unknown", None)
                and (new_state.get("attributes") or {}).get("source") == bus.source_name
            ),
            description=(
                f"route {player.display_name} output {player.output_number} to {bus.source_name}"
            ),
        )

    async def turn_off_zone(self, player: TriadMatrixTestPlayer) -> None:
        """Disconnect exactly one Triad output and verify it is off."""
        self.logger.info(
            "TRIAD OFF: %s (output %s)",
            player.display_name,
            player.output_number,
        )

        await self._verified_media_player_command(
            entity_id=player.zone_entity,
            service="turn_off",
            service_data=None,
            verifier=lambda state: (
                state.get("state") == "off"
                and (state.get("attributes") or {}).get("source") is None
            ),
            description=(f"disconnect {player.display_name} output {player.output_number}"),
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
            verifier=lambda state: self._volume_matches(state, expected),
            description=(
                f"set {player.display_name} output {player.output_number} volume to {volume_level}%"
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
            verifier=lambda state: (state.get("attributes") or {}).get("is_volume_muted") is muted,
            description=(f"set {player.display_name} output {player.output_number} muted={muted}"),
        )

    async def _get_zone_states(
        self,
        entity_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        """Return Home Assistant states keyed by entity ID."""
        hass_provider = self.get_hass_provider()
        states = await hass_provider.get_states(entity_ids=entity_ids)
        return {str(state["entity_id"]): state for state in states if state.get("entity_id")}

    @staticmethod
    def _volume_matches(state: dict[str, Any], expected: float) -> bool:
        """Return whether a Home Assistant state has the expected volume."""
        volume = (state.get("attributes") or {}).get("volume_level")
        return isinstance(volume, int | float) and abs(float(volume) - expected) < 0.005

    async def _verified_media_player_command(
        self,
        *,
        entity_id: str,
        service: str,
        service_data: dict[str, Any] | None,
        verifier: Callable[[dict[str, Any]], bool],
        description: str,
        attempts: int = 5,
    ) -> dict[str, Any]:
        """Run a Home Assistant media-player command and verify its state."""
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
                            "TRIAD VERIFY: %s succeeded on attempt %d",
                            description,
                            attempt,
                        )
                    return last_state

                self.logger.warning(
                    "TRIAD VERIFY: %s not confirmed on attempt %d/%d",
                    description,
                    attempt,
                    attempts,
                )

            except Exception as err:
                last_error = err
                self.logger.warning(
                    "TRIAD VERIFY: %s raised on attempt %d/%d: %s",
                    description,
                    attempt,
                    attempts,
                    err,
                )

            if attempt < attempts:
                await asyncio.sleep(0.35)

        detail = (
            f"last_state={last_state!r}" if last_state is not None else f"last_error={last_error!r}"
        )

        raise PlayerCommandFailed(
            f"Triad command could not be verified after {attempts} attempts: "
            f"{description}; {detail}"
        )
