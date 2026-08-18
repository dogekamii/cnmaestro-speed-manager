from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .session import SessionGate


@dataclass(slots=True)
class ModuleContext:
    data_dir: Path
    demo: bool = False
    session_gate: SessionGate = field(default_factory=SessionGate)
    services: dict[str, Any] = field(default_factory=dict)
    operation_changed: Callable[[bool], None] = field(default=lambda _active: None)


@runtime_checkable
class ModuleProvider(Protocol):
    """First-party module contract; registration is explicit and static."""

    module_id: str
    title: str
    description: str

    def create_view(self, parent: Any, context: ModuleContext) -> Any: ...
