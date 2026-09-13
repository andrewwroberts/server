"""Triad AMS matrix prototype player provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .provider import TriadMatrixTestProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType


SUPPORTED_FEATURES: set[ProviderFeature] = set()


async def setup(
    mass: MusicAssistant,
    manifest: ProviderManifest,
    config: ProviderConfig,
) -> ProviderInstanceType:
    """Initialize the Triad matrix prototype provider."""
    return TriadMatrixTestProvider(
        mass,
        manifest,
        config,
        SUPPORTED_FEATURES,
    )
