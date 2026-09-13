# Triad Matrix Prototype

Experimental two-room Music Assistant provider for Andrew's Triad AMS16
whole-house audio system.

## Physical wiring used by this prototype

- Sonos Connect 1
  - MA player ID: `RINCON_B8E937997EE801400`
  - Home Assistant: `media_player.connect_1`
  - Triad input: 3
- Kitchen
  - Home Assistant: `media_player.triad_kitchen`
  - Triad output: 4
- Dining Room
  - Home Assistant: `media_player.triad_dining_room`
  - Triad output: 6

The provider uses the source name `Connect 1`, matching the working Home
Assistant Triad routing configuration.

## Goal

Music Assistant exposes Kitchen and Dining Room as room players.

Playback started on one room claims Connect 1. A grouped room is routed to
the same Triad input.

Adding or removing a room during playback changes only the matrix routing.
The Sonos stream must not restart.

## Safety

This prototype:

- does not modify existing Home Assistant Universal Media Players
- does not modify the existing Music Assistant installation/data
- only controls Kitchen and Dining Room
- only uses Connect 1
- refuses to claim Connect 1 when the native Sonos player is already playing
- turns off only prototype group outputs when playback is stopped
