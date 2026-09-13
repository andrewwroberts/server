# Triad Matrix Prototype

Experimental Music Assistant provider for Andrew's Triad AMS16 whole-house
audio system.

## Physical wiring

| Bus | Native MA Sonos player ID | HA entity | Triad input |
| --- | --- | --- | --- |
| Connect 1 | `RINCON_B8E937997EE801400` | `media_player.connect_1` | 3 |
| Connect 2 | `RINCON_B8E937997EEE01400` | `media_player.connect_2` | 4 |

| Triad output | Room | HA entity |
| --- | --- | --- |
| 1 | Master Shower | `media_player.triad_master_shower` |
| 2 | Master Bath | `media_player.triad_master_bath` |
| 3 | Master Bedroom | `media_player.triad_master_bedroom` |
| 4 | Kitchen | `media_player.triad_kitchen` |
| 5 | Family Room | `media_player.triad_family_room` |
| 6 | Dining Room | `media_player.triad_dining_room` |
| 7 | Library | `media_player.triad_library` |
| 8 | Breakfast Room | `media_player.triad_breakfast_room` |
| 9 | Theater Room | `media_player.triad_theater_room` |
| 10 | Outdoor Eating Area | `media_player.triad_outdoor_eating_area` |
| 11 | Fire Pit | `media_player.triad_fire_pit` |
| 12 | Basement Weight Room | `media_player.triad_basement_weight_room` |
| 13 | Basement Rec Room | `media_player.triad_basement_rec_room` |

The provider keeps both Connects at 100% and unmuted as fixed source-level
renderers. Each room's Music Assistant volume and mute controls operate only
its own Triad output.

## Allocation and grouping

Starting playback reserves the first idle, unrouted Connect and routes the
logical room or its prebuilt group to that Triad input. A second independent
session uses the other Connect. A third independent session is refused while
both buses are occupied; an existing stream is never stopped or stolen.

Native Music Assistant grouping is maintained independently of bus allocation.
Rooms can be grouped before playback. Adding or removing a room during playback
changes only that room's Triad route and does not restart the Sonos stream.

Stopping a session stops its native Sonos backend, disconnects all of its Triad
rooms, verifies that no room is still routed to that source, and only then
releases the bus.

## Safety

This prototype does not modify the existing Home Assistant Universal Media
Players or YAML routing layer. It uses the proven Triad HA media-player entity
controls and the native Music Assistant Sonos players. It also refuses to:

- claim a Connect that is unavailable, playing, paused, or already reserved
- claim a Connect that is already routed to another Triad room
- reroute a room away from a different physical source
- release a bus while any Triad room remains routed to it
