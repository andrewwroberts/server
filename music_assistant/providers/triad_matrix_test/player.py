"""Logical room player for the Triad AMS matrix prototype."""

from __future__ import annotations

from time import time
from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import MediaType, PlaybackState, PlayerFeature
from music_assistant_models.errors import PlayerCommandFailed
from music_assistant_models.player import DeviceInfo

from music_assistant.controllers.players.constants import PlayerLockPurpose
from music_assistant.models.player import Player, PlayerMedia

if TYPE_CHECKING:
    from .provider import MatrixBus, TriadMatrixTestProvider


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
        self._attr_can_group_with = {provider.instance_id}
        self._attr_available = False
        self._attr_playback_state = PlaybackState.IDLE
        self._attr_volume_level = None
        self._attr_volume_muted = None
        self._intentional_pause = False
        # Wall-clock time at which the current hidden-Sonos transport session
        # was started. Sonos may briefly retain the previous session's elapsed
        # anchor while the new stream is starting; never extrapolate from an
        # anchor older than this session.
        self._transport_started_at: float | None = None
        # Raw Sonos flow position observed when the current transport first
        # genuinely began rendering. Sonos includes its startup/buffering time
        # in that position, so logical Triad stream-time subtracts this fixed
        # per-transport origin rather than exposing the startup lead.
        self._transport_elapsed_origin: float | None = None

    @property
    def requires_flow_mode(self) -> bool:
        """Use one continuous MA stream for the shared Sonos transport."""
        return True

    @property
    def auto_stop_paused_queue(self) -> bool:
        """Keep a deliberately paused Triad session and its reserved bus alive."""
        return False

    @property
    def needs_poll(self) -> bool:
        """Poll Home Assistant for Triad room state."""
        return True

    @property
    def poll_interval(self) -> int:
        """Poll more frequently while this player owns a source bus."""
        if self._prov.get_bus_for_owner(self.player_id):
            return 2
        return 10

    async def poll(self) -> None:
        """Refresh Triad state and mirror the reserved Sonos backend."""
        try:
            state = await self._prov.get_zone_state(self.zone_entity)
        except Exception as err:
            self._prov.logger.debug(
                "Unable to poll %s: %s",
                self.display_name,
                err,
            )
            self._attr_available = False
            self.update_state()
            return

        raw_state = state.get("state")
        attrs = state.get("attributes", {})
        bus = self._prov.get_bus_for_owner(self.player_id)
        backend = self._prov.get_backend_player(bus, required=False) if bus is not None else None

        self._attr_available = raw_state not in ("unavailable", "unknown", None) and (
            backend is not None if bus is not None else self._prov.has_available_backend()
        )
        self._attr_powered = raw_state != "off"

        volume = attrs.get("volume_level")
        if isinstance(volume, int | float):
            self._attr_volume_level = round(float(volume) * 100)

        muted = attrs.get("is_volume_muted")
        if isinstance(muted, bool):
            self._attr_volume_muted = muted

        if backend is not None:
            backend_playback_state = backend.state.playback_state
            queue = self.mass.player_queues.get(self.player_id)
            queue_data = self.mass.player_queues.queue_data_or_none(self.player_id)

            # The hidden Sonos backend only transports the audio. The logical Triad
            # room's Now Playing metadata must follow its MA queue, otherwise the
            # first PlayerMedia object remains stuck while later tracks play.
            queue_active = bool(
                queue is not None and getattr(queue, "active", False)
            )
            queue_current_item = (
                getattr(queue, "current_item", None) if queue is not None else None
            )

            if queue_active and queue_current_item is not None:
                current_queue_item_id = (
                    self._attr_current_media.queue_item_id
                    if self._attr_current_media is not None
                    else None
                )
                if current_queue_item_id != queue_current_item.queue_item_id:
                    try:
                        self._attr_current_media = (
                            await self.mass.player_queues.player_media_from_queue_item(
                                queue_current_item
                            )
                        )
                    except Exception as err:
                        self._prov.logger.debug(
                            "Unable to refresh Now Playing metadata for %s: %s",
                            self.display_name,
                            err,
                        )

                queue_elapsed = getattr(queue, "elapsed_time", None)
                if (
                    self._attr_current_media is not None
                    and self._attr_current_media.source_id == self.player_id
                    and queue_elapsed is not None
                ):
                    self._attr_current_media.elapsed_time = int(queue_elapsed)
                    self._attr_current_media.elapsed_time_last_updated = getattr(
                        queue,
                        "elapsed_time_last_updated",
                        None,
                    )

            media_belongs_to_queue = (
                self._attr_current_media is not None
                and self._attr_current_media.source_id == self.player_id
            )
            flow_exhausted = bool(
                queue_data is not None
                and queue_data.session_id is not None
                and self.mass.player_queues.flow_queue_exhausted(
                    self.player_id,
                    queue_data.session_id,
                )
            )

            # An explicit Triad pause owns the logical transport state until a
            # new play_media() call clears _intentional_pause. Sonos implements
            # pause as STOP and can continue reporting PLAYING briefly while that
            # stop is in flight. Treating that transient PLAYING report as a
            # resume would re-enable flow reconciliation and allow the renderer
            # clock to move the queue playhead backwards during the pause.
            if self._intentional_pause:
                backend_playback_state = PlaybackState.PAUSED
            elif (
                backend_playback_state == PlaybackState.PAUSED
                and queue is not None
                and media_belongs_to_queue
                and (
                    queue.ended
                    or flow_exhausted
                )
            ):
                backend_playback_state = PlaybackState.IDLE

            # The hidden Sonos renderer is the transport clock for the continuous
            # flow stream. A new Triad transport is not considered logically
            # PLAYING until that renderer has actually advanced. Sonos can report
            # PLAYING while it is still fetching/buffering the flow URL; publishing
            # PLAYING at that point causes HA to extrapolate the elapsed timer
            # before any audio has actually begun.
            backend_elapsed = backend.state.elapsed_time
            backend_elapsed_updated = backend.state.elapsed_time_last_updated
            transport_started_at = getattr(
                self,
                "_transport_started_at",
                None,
            )
            transport_elapsed_origin = getattr(
                self,
                "_transport_elapsed_origin",
                None,
            )

            transport_confirmed = (
                transport_started_at is not None
                and backend_playback_state == PlaybackState.PLAYING
                and isinstance(backend_elapsed, int | float)
                and backend_elapsed > 0
                and isinstance(backend_elapsed_updated, int | float)
                and backend_elapsed_updated >= transport_started_at
            )

            if transport_started_at is not None and not transport_confirmed:
                # Keep the logical player non-running while the new Sonos
                # transport has not advanced. This prevents corrected elapsed
                # time and HA's media_position clock from running during startup.
                self._attr_playback_state = (
                    PlaybackState.PAUSED
                    if self._attr_playback_state == PlaybackState.PAUSED
                    else PlaybackState.IDLE
                )
                self._attr_elapsed_time = 0.0
                self._attr_elapsed_time_last_updated = time()

            elif transport_confirmed:
                # Sonos' first positive renderer position already contains the
                # fetch/buffer startup interval. Treat that raw position as this
                # transport's zero. From here onward we subtract only this fixed
                # renderer-space origin; no wall-clock estimate is involved.
                transport_elapsed_origin = float(backend_elapsed)
                self._transport_elapsed_origin = transport_elapsed_origin
                self._transport_started_at = None

                self._attr_playback_state = backend_playback_state
                self._attr_elapsed_time = 0.0
                self._attr_elapsed_time_last_updated = time()

                if queue is not None:
                    queue.elapsed_time_last_updated = time()

            else:
                self._attr_playback_state = backend_playback_state

                if (
                    transport_elapsed_origin is not None
                    and isinstance(backend_elapsed, int | float)
                ):
                    self._attr_elapsed_time = max(
                        0.0,
                        float(backend_elapsed) - transport_elapsed_origin,
                    )
                else:
                    self._attr_elapsed_time = backend_elapsed

                self._attr_elapsed_time_last_updated = backend_elapsed_updated

        self.update_state()

    async def volume_set(self, volume_level: int) -> None:
        """Set the volume of this Triad output only."""
        volume_level = max(0, min(100, volume_level))

        await self._prov.set_zone_volume(
            self,
            volume_level,
        )

        self._attr_volume_level = volume_level
        self.update_state()

    async def volume_mute(self, muted: bool) -> None:
        """Mute or unmute this Triad output only."""
        await self._prov.set_zone_mute(
            self,
            muted,
        )

        self._attr_volume_muted = muted
        self.update_state()

    async def _media_for_backend(self, media: PlayerMedia) -> PlayerMedia:
        """Convert queue media to one continuous flow stream for the hidden Sonos bus."""
        if (
            not media.source_id
            or not media.queue_item_id
            or media.media_type
            in (
                MediaType.RADIO,
                MediaType.AUDIO_SOURCE,
                MediaType.ANNOUNCEMENT,
                MediaType.FLOW_STREAM,
            )
        ):
            return media

        # Resolve the URL against the logical Triad player, not the hidden Sonos
        # Connect. Triad requires flow mode; resolving against Sonos would create
        # a /single/ stream and make Sonos try to enqueue individual queue items.
        flow_uri = await self.mass.streams.resolve_stream_url(
            self.player_id,
            media,
        )

        return PlayerMedia(
            uri=flow_uri,
            media_type=MediaType.FLOW_STREAM,
            title=media.title,
            artist=media.artist,
            album=media.album,
            image_url=media.image_url,
            source_id=media.source_id,
            # Deliberately omit queue_item_id here. Sonos treats any media carrying
            # both source_id and queue_item_id as a regular cloud-queue item before
            # it considers FLOW_STREAM, which sends playback back through /single/
            # and causes MA to attempt enqueue_next_media. The flow URL itself
            # already contains the starting queue-item id.
            queue_session_id=media.queue_session_id,
            custom_data={
                **(media.custom_data or {}),
                "triad_logical_player_id": self.player_id,
                "triad_start_queue_item_id": media.queue_item_id,
            },
        )

    async def play_media(self, media: PlayerMedia) -> None:
        """Start MA playback on an available source bus."""
        self._intentional_pause = False
        backend_media = await self._media_for_backend(media)
        members = self._effective_members()
        bus, backend = await self._prov.claim_bus(
            self.player_id,
            [member.player_id for member in members],
        )
        routed: list[TriadMatrixTestPlayer] = []

        self._prov.logger.info(
            "TRIAD PLAY: leader=%s members=%s bus=%s backend=%s",
            self.display_name,
            [member.display_name for member in members],
            bus.source_name,
            backend.display_name,
        )

        try:
            await self._prov.prepare_backend(bus, backend)

            for member in members:
                await self._prov.route_zone_to_bus(member, bus)
                routed.append(member)

            # Mark the new transport before issuing play_media. Any Sonos
            # elapsed-time timestamp older than this point belongs to the
            # previous stream and must not be extrapolated as the new session.
            self._transport_started_at = time()
            self._transport_elapsed_origin = None

            async with self.mass.players.get_player_lock(
                backend.player_id,
                PlayerLockPurpose.PLAYBACK,
            ):
                await self.mass.players._handle_play_media(
                    backend.player_id,
                    backend_media,
                )

        except Exception:
            self._transport_started_at = None
            self._transport_elapsed_origin = None
            cleanup_errors = await self._cleanup_session(bus, routed)
            if cleanup_errors:
                self._prov.logger.error(
                    "Triad startup cleanup retained %s for safety: %s",
                    bus.source_name,
                    "; ".join(cleanup_errors),
                )
            else:
                try:
                    await self._prov.release_bus(self.player_id)
                except Exception as err:
                    self._prov.logger.error(
                        "Triad startup cleanup retained %s for safety: %s",
                        bus.source_name,
                        err,
                    )
            raise

        self._attr_current_media = media
        self._attr_active_source = media.source_id or self.player_id

        # Loading the flow URL successfully is not the same thing as audible
        # playback having started. Keep fresh starts idle, and resumes paused,
        # until poll() sees the hidden Sonos renderer actually advance.
        if self._attr_playback_state != PlaybackState.PAUSED:
            self._attr_playback_state = PlaybackState.IDLE
        self.update_state()

    async def play(self) -> None:
        """Resume the logical queue by rebuilding its flow stream at the saved position."""
        self._get_owned_bus()

        # Sonos deliberately implements pause of an MA stream as STOP. Therefore
        # there is no backend transport to "unpause". Resume through the queue
        # controller instead: it preserves queue.resume_pos, rotates/reloads the
        # stream as needed, and eventually calls this player's play_media again.
        await self.mass.player_queues.resume(self.player_id)

        self._intentional_pause = False
        # play_media/poll owns the transition back to PLAYING. Do not start the
        # logical elapsed clock before the hidden Sonos renderer has advanced.

    async def pause(self) -> None:
        """Pause playback without changing matrix routing."""
        bus = self._get_owned_bus()
        backend = self._prov.get_backend_player(bus)
        assert backend is not None

        # Freeze the logical player BEFORE touching the Sonos renderer.
        #
        # Sonos implements pause of an MA flow stream as STOP. During that STOP
        # its renderer clock can reset toward zero before the backend state
        # update reaches us. If the logical Triad player is still PLAYING in
        # that window, flow-mode reconciliation interprets the reset cumulative
        # clock as the beginning of the flow and rewinds queue.current_item to
        # the first track. player_queues.pause() has already captured the real
        # resume_pos, so that leaves the queue with a first-track current item
        # and a second-track resume position.
        #
        # Publishing PAUSED first freezes flow-mode reconciliation on the
        # already-correct queue item and elapsed position while Sonos stops.
        self._intentional_pause = True
        self._transport_started_at = None
        self._transport_elapsed_origin = None
        self._attr_playback_state = PlaybackState.PAUSED
        self.update_state()

        # Invalidate the old MA flow before telling Sonos to stop. Any trailing
        # GET for the old flow URL is then rejected instead of restarting from
        # that URL's original queue item.
        session_id = self._detach_paused_audio_session()

        async with self.mass.players.get_player_lock(
            backend.player_id,
            PlayerLockPurpose.PLAYBACK,
        ):
            # Call the Sonos provider directly. MA's generic _handle_cmd_pause
            # rejects the hidden Connect because its active "Music Assistant Queue"
            # source advertises can_play_pause=False. Sonos itself deliberately
            # handles an MA-queue pause by stopping its renderer, which is the
            # transport behavior this logical Triad pause needs.
            await backend.pause()

        # Now that the renderer is stopped, release the detached session's
        # buffers/provider slots. The queue itself remains paused and resumable.
        await self._cleanup_paused_audio_session(session_id)

        # The logical state was frozen before backend.pause(); do not publish
        # another transport transition here. The queue remains paused/resumable
        # on the exact item and position captured by PlayerQueuesController.
        self.update_state()

    def _detach_paused_audio_session(self) -> str | None:
        """Invalidate the current flow session before stopping the Sonos renderer."""
        queue_data = self.mass.player_queues.queue_data_or_none(self.player_id)
        if queue_data is None or queue_data.session_id is None:
            return None

        session_id = queue_data.session_id

        # Nothing should be allowed to create or attach another buffer for the
        # old playback session while pause tears it down.
        self.mass.cancel_task(f"preload_next_item_{self.player_id}")
        self.mass.cancel_timer(f"enqueue_next_item_{self.player_id}")
        self.mass.cancel_task(f"enqueue_next_item_{self.player_id}")
        self.mass.cancel_task(f"prepare_next_audio_buffer_{self.player_id}")

        if queue_data.session_id != session_id:
            return None

        # This must happen before backend.pause(). A trailing Sonos GET using
        # the old /flow/<session>/... URL will then fail the stream controller's
        # session validation instead of restarting an earlier queue item.
        queue_data.session_id = None
        self.mass.streams.close_superseded_item_streams(
            self.player_id,
            None,
        )
        return session_id

    async def _cleanup_paused_audio_session(self, session_id: str | None) -> None:
        """Release buffers and provider source slots for a detached paused session."""
        if session_id is None:
            return

        self.mass.streams.audio_processing.clear(
            self.player_id,
            session_id,
        )
        await self.mass.player_queues._cleanup_queue_audio_data(
            self.player_id,
            session_id,
        )

    async def stop(self) -> None:
        """Stop and release this logical room group's source bus."""
        bus = self._prov.get_bus_for_owner(self.player_id)
        if bus is None:
            self._set_idle()
            return

        members = self._effective_members()
        self._prov.logger.info(
            "TRIAD STOP: leader=%s members=%s bus=%s",
            self.display_name,
            [member.display_name for member in members],
            bus.source_name,
        )

        errors = await self._cleanup_session(bus, members)
        if errors:
            raise PlayerCommandFailed(
                f"Could not safely release {bus.source_name}; it remains reserved. "
                f"{'; '.join(errors)}"
            )

        await self._prov.release_bus(self.player_id)
        self._set_idle()

        for member in members:
            if member is not self:
                member.update_state()

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Add or remove logical rooms without restarting an active stream."""
        current = dict.fromkeys(self._attr_group_members or [self.player_id])
        added: list[TriadMatrixTestPlayer] = []
        removed: list[TriadMatrixTestPlayer] = []

        for member_id in player_ids_to_add or []:
            if member_id == self.player_id:
                continue

            member = self._prov.get_room_player(member_id)
            if member is None:
                raise PlayerCommandFailed(f"{member_id} is not a Triad prototype room.")

            if member_bus := self._prov.get_bus_for_owner(member_id):
                reclaimed = await self._prov.reconcile_idle_bus_owner(member_id)
                if not reclaimed:
                    raise PlayerCommandFailed(
                        f"{member.display_name} is using {member_bus.source_name} "
                        "for an independent stream; stop it before grouping."
                    )

            if member.state.synced_to and member.state.synced_to != self.player_id:
                raise PlayerCommandFailed(f"{member.display_name} is already grouped elsewhere.")

            if member_id not in current:
                current[member_id] = None
                added.append(member)

        for member_id in player_ids_to_remove or []:
            if member_id == self.player_id:
                continue

            member = self._prov.get_room_player(member_id)
            if member_id in current:
                current.pop(member_id, None)

                if member is not None:
                    removed.append(member)

        bus = self._prov.get_bus_for_owner(self.player_id)
        completed_added: list[TriadMatrixTestPlayer] = []
        completed_removed: list[TriadMatrixTestPlayer] = []

        if bus is not None:
            try:
                for member in added:
                    await self._prov.route_zone_to_bus(member, bus)
                    completed_added.append(member)

                for member in removed:
                    await self._prov.turn_off_zone(member)
                    completed_removed.append(member)
            except Exception:
                await self._rollback_member_changes(
                    bus,
                    completed_added,
                    completed_removed,
                )
                raise

        other_members = [player_id for player_id in current if player_id != self.player_id]
        self._attr_group_members = [self.player_id, *other_members] if other_members else []

        self._prov.logger.info(
            "TRIAD MEMBERS: leader=%s add=%s remove=%s active_bus=%s final=%s",
            self.display_name,
            [member.display_name for member in added],
            [member.display_name for member in removed],
            bus.source_name if bus else None,
            self._attr_group_members,
        )

        self.update_state()
        for member in [*added, *removed]:
            member.update_state()

    def _effective_members(self) -> list[TriadMatrixTestPlayer]:
        """Return this leader plus its current logical room members."""
        ids = list(self._attr_group_members) if self._attr_group_members else [self.player_id]
        if self.player_id not in ids:
            ids.insert(0, self.player_id)

        return [
            player
            for player_id in ids
            if (player := self._prov.get_room_player(player_id)) is not None
        ]

    def _get_owned_bus(self) -> MatrixBus:
        """Return this player's reserved source bus."""
        bus = self._prov.get_bus_for_owner(self.player_id)
        if bus is None:
            raise PlayerCommandFailed(
                f"{self.display_name} does not currently own a Triad source bus."
            )
        return bus

    async def _cleanup_session(
        self,
        bus: MatrixBus,
        members: list[TriadMatrixTestPlayer],
    ) -> list[str]:
        """Stop a backend and disconnect the rooms attached to its session."""
        errors: list[str] = []
        backend = self._prov.get_backend_player(bus, required=False)

        if backend is None:
            errors.append(f"{bus.source_name} backend is unavailable")
        else:
            try:
                async with self.mass.players.get_player_lock(
                    backend.player_id,
                    PlayerLockPurpose.PLAYBACK,
                ):
                    await self.mass.players._handle_cmd_stop(
                        backend.player_id,
                    )
                await self._prov.wait_for_backend_idle(bus, backend)
            except Exception as err:
                errors.append(f"stop {bus.source_name}: {err}")

        for member in members:
            try:
                await self._prov.turn_off_zone(member)
            except Exception as err:
                errors.append(f"disconnect {member.display_name}: {err}")

        return errors

    async def _rollback_member_changes(
        self,
        bus: MatrixBus,
        added: list[TriadMatrixTestPlayer],
        removed: list[TriadMatrixTestPlayer],
    ) -> None:
        """Best-effort restore the physical group after a membership error."""
        rollback_errors: list[str] = []

        for member in reversed(added):
            try:
                await self._prov.turn_off_zone(member)
            except Exception as err:
                rollback_errors.append(f"remove {member.display_name}: {err}")

        for member in reversed(removed):
            try:
                await self._prov.route_zone_to_bus(member, bus)
            except Exception as err:
                rollback_errors.append(f"restore {member.display_name}: {err}")

        if rollback_errors:
            self._prov.logger.error(
                "Triad membership rollback was incomplete on %s: %s",
                bus.source_name,
                "; ".join(rollback_errors),
            )

    def _set_idle(self) -> None:
        """Reset local playback state after a successful stop."""
        self._intentional_pause = False
        self._attr_playback_state = PlaybackState.IDLE
        self._attr_current_media = None
        self._attr_active_source = None
        self._attr_elapsed_time = None
        self._attr_elapsed_time_last_updated = None
        self._transport_started_at = None
        self._transport_elapsed_origin = None
        self.update_state()

    @property
    def _prov(self) -> TriadMatrixTestProvider:
        """Return the typed Triad provider."""
        return cast("TriadMatrixTestProvider", self.provider)
