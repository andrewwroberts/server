"""Setup flow for the Triad matrix prototype."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession


async def run_setup(session: SetupSession) -> None:
    """Create the prototype provider with its fixed test configuration."""
    await session.finish({})
