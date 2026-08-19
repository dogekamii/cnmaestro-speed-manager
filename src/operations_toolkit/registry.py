from __future__ import annotations

from .core.modules import ModuleProvider
from .modules.cnmaestro.provider import BulkSpeedChangesProvider


def first_party_modules() -> tuple[ModuleProvider, ...]:
    """Static registry only; runtime third-party plugin loading is intentionally unsupported."""
    return (BulkSpeedChangesProvider(),)
