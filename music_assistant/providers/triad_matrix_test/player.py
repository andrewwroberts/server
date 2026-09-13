"""Logical room player for the Triad AMS matrix prototype."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import PlaybackState, PlayerFeature
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.player import DeviceInfo

from music_assistant.controllers.players.constants import PlayerLockPurpose
from music_assistant.models.player import Player, PlayerMedia

if TYPE_CHECKING:
    from .provider import TriadMatrixTestProvider


class TriadMatrixTestPlayer(Player):
    """One logical Music Assistant room backed by a Triad output."""

    def __init__(
        self,
        provider: TriadMatrixTestProvider,
        player_id: str,
        name: str,
        zone_entity: str,
        output_number: int,
    ) -> None:
        """Initialize logical room."""
        super().__init__(provider, player_id)

        self.zone_entity = zone_entity
        self.output_number = output_number

        self._attr_name = name
        self._attr_device_info = DeviceInfo(
            manufacturer="Triad / Control4",
            model=f"AMS16 Output {output_number}",
        )

        self._attr_supported_features = {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.PAUSE,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.SET_MEMBERS,
        }

        # Every logical room belonging to this provider can group with every
        # other logical room belonging to the same provider.
        self._attr_can_group_with = {provider.instance_id}

        self._attr_available = False
        self._attr_playback_state = PlaybackState.IDLE
        self._attr_volume_level = None
        self._attr_volume_muted = None

    @property
    def requires_flow_mode(self) -> bool:
        """Use one continuous MA stream for the shared Sonos transport."""
        return True

    @property
    def needs_poll(self) -> bool:
        """Poll HA for Triad volume/availability state."""
        return True

    @property
    def poll_interval(self) -> int:
        """Poll more frequently while this player owns playback."""
        if self.provider.bus_owner == self.player_id:
            return 2
        return 10

    def _effective_members(self) -> list[TriadMatrixTestPlayer]:
        """Return this leader plus its current logical room members."""
        ids = (
            list(self._attr_group_members)
            if self._attr_group_members
            else [self.player_id]
        )

        if self.player_id not in ids:
            ids.insert(0, self.player_id)

        result: list[TriadMatrixTestPlayer] = []

        for player_id in ids:
            player = self.provider.get_room_player(player_id)
            if player is not None:
                result.append(player)

        return result

    async def poll(self) -> None:
        """Refresh Triad state and mirror backend playback for the bus owner."""
        try:
            state = await self.provider.get_zone_state(self.zone_entity)
        except Exception as err:
            self.provider.logger.debug(
                "Unable to poll %s: %s",
                self.display_name,
                err,
            )
            self._attr_available = False
            self.update_state()
            return

        raw_state = state.get("state")
        attrs = state.get("attributes", {})

        backend = self.provider.get_backend_player(required=False)

        self._attr_available = (
            raw_state not in ("unavailable", "unknown", None)
            and backend is not None
        )

        self._attr_powered = raw_state != "off"

        volume = attrs.get("volume_level")
        if isinstance(volume, int | float):
            self._attr_volume_level = round(float(volume) * 100)

        muted = attrs.get("is_volume_muted")
        if isinstance(muted, bool):
            self._attr_volume_muted = muted

        if (
            self.provider.bus_owner == self.player_id
            and backend is not None
        ):
            self._attr_playback_state = backend.state.playback_state

        self.update_state()

    async def volume_set(self, volume_level: int) -> None:
        """Set the volume of this Triad output only."""
        volume_level = max(0, min(100, volume_level))

        await self.provider.call_media_player_service(
            self.zone_entity,
            "volume_set",
            {"volume_level": volume_level / 100},
        )

        self._attr_volume_level = volume_level
        self.update_state()

    async def volume_mute(self, muted: bool) -> None:
        """Mute or unmute this Triad output only."""
        await self.provider.call_media_player_service(
            self.zone_entity,
            "volume_mute",
            {"is_volume_muted": muted},
        )

        self._attr_volume_muted = muted
        self.update_state()

    async def play_media(self, media: PlayerMedia) -> None:
        """Start MA playback through Connect 1 and route selected rooms."""
        backend = await self.provider.claim_bus(self.player_id)

        members = self._effective_members()

        self.provider.logger.info(
            "TRIAD TEST PLAY: leader=%s members=%s backend=%s",
            self.display_name,
            [member.display_name for member in members],
            backend.display_name,
        )

        try:
            # Establish matrix routing first so audio is already pointed at
            # the desired rooms when the Sonos stream begins.
            for member in members:
                await self.provider.route_zone_to_bus(member)

            # This is the same internal forwarding pattern used by MA's own
            # Sync Group / Universal Group providers.
            async with self.mass.players.get_player_lock(
                backend.player_id,
                PlayerLockPurpose.PLAYBACK,
            ):
                await self.mass.players._handle_play_media(
                    backend.player_id,
                    media,
                )

        except Exception:
            # Never leave the prototype claiming the shared transport after
            # a failed startup.
            for member in members:
                try:
                    await self.provider.turn_off_zone(member)
                except Exception:
                    pass

            await self.provider.release_bus(self.player_id)
            raise

        self._attr_current_media = media
        self._attr_active_source = media.source_id or self.player_id
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def play(self) -> None:
        """Resume playback on the shared Sonos backend."""
        if self.provider.bus_owner != self.player_id:
            raise PlayerCommandFailed(
                f"{self.display_name} does not currently own Connect 1."
            )

        backend = self.provider.get_backend_player()
        assert backend is not None

        async with self.mass.players.get_player_lock(
            backend.player_id,
            PlayerLockPurpose.PLAYBACK,
        ):
            await self.mass.players._handle_cmd_play(
                backend.player_id,
            )

        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def pause(self) -> None:
        """Pause playback without changing any matrix routing."""
        if self.provider.bus_owner != self.player_id:
            raise PlayerCommandFailed(
                f"{self.display_name} does not currently own Connect 1."
            )

        backend = self.provider.get_backend_player()
        assert backend is not None

        async with self.mass.players.get_player_lock(
            backend.player_id,
            PlayerLockPurpose.PLAYBACK,
        ):
            await self.mass.players._handle_cmd_pause(
                backend.player_id,
            )

        self._attr_playback_state = PlaybackState.PAUSED
        self.update_state()

    async def stop(self) -> None:
        """Stop the shared stream and turn off this logical room group."""
        if self.provider.bus_owner != self.player_id:
            self._attr_playback_state = PlaybackState.IDLE
            self._attr_current_media = None
            self.update_state()
            return

        backend = self.provider.get_backend_player(required=False)

        self.provider.logger.info(
            "TRIAD TEST STOP: leader=%s members=%s",
            self.display_name,
            [member.display_name for member in self._effective_members()],
        )

        if backend is not None:
            async with self.mass.players.get_player_lock(
                backend.player_id,
                PlayerLockPurpose.PLAYBACK,
            ):
                await self.mass.players._handle_cmd_stop(
                    backend.player_id,
                )

        for member in self._effective_members():
            await self.provider.turn_off_zone(member)

        await self.provider.release_bus(self.player_id)

        self._attr_playback_state = PlaybackState.IDLE
        self._attr_current_media = None
        self._attr_active_source = None
        self.update_state()

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Dynamically add/remove Triad rooms without restarting Sonos."""
        current = dict.fromkeys(
            self._attr_group_members or [self.player_id]
        )

        added: list[TriadMatrixTestPlayer] = []
        removed: list[TriadMatrixTestPlayer] = []

        for member_id in player_ids_to_add or []:
            if member_id == self.player_id:
                continue

            member = self.provider.get_room_player(member_id)

            if member is None:
                raise PlayerCommandFailed(
                    f"{member_id} is not a Triad prototype room."
                )

            if (
                member.state.synced_to
                and member.state.synced_to != self.player_id
            ):
                raise PlayerCommandFailed(
                    f"{member.display_name} is already grouped elsewhere."
                )

            if member_id not in current:
                current[member_id] = None
                added.append(member)

        for member_id in player_ids_to_remove or []:
            if member_id == self.player_id:
                continue

            member = self.provider.get_room_player(member_id)

            if member_id in current:
                current.pop(member_id, None)

                if member is not None:
                    removed.append(member)

        other_members = [
            player_id
            for player_id in current
            if player_id != self.player_id
        ]

        self._attr_group_members = (
            [self.player_id, *other_members]
            if other_members
            else []
        )

        active = self.provider.bus_owner == self.player_id

        self.provider.logger.info(
            "TRIAD TEST MEMBERS: leader=%s add=%s remove=%s active=%s final=%s",
            self.display_name,
            [member.display_name for member in added],
            [member.display_name for member in removed],
            active,
            self._attr_group_members,
        )

        # This is the key experiment:
        #
        # During active playback, adding a room performs ONLY a matrix route.
        # It does not call play_media, pause, play, stop, or otherwise touch
        # the Sonos transport.
        if active:
            for member in added:
                await self.provider.route_zone_to_bus(member)

            for member in removed:
                await self.provider.turn_off_zone(member)

        self.update_state()

        for member in [*added, *removed]:
            member.update_state()
