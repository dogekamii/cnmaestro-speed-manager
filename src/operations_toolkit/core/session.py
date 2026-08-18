from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4


class SessionBusy(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OperationToken:
    value: str
    name: str


class SessionGate:
    """Freezes the active adapter while scan/publish/reconcile is running."""

    def __init__(self) -> None:
        self._session: object | None = None
        self._clear: Callable[[], None] | None = None
        self._operation: OperationToken | None = None

    @property
    def connection(self) -> object | None:
        return self._session

    @property
    def operation(self) -> str | None:
        return self._operation.name if self._operation else None

    def replace(self, session: object, clear: Callable[[], None]) -> None:
        self._assert_idle()
        if self._clear:
            self._clear()
        self._session, self._clear = session, clear

    def disconnect(self) -> None:
        self._assert_idle()
        if self._clear:
            self._clear()
        self._session = self._clear = None

    def shutdown(self) -> None:
        """Clear a session during process shutdown, even if an operation is active."""
        clear, self._clear = self._clear, None
        self._session = None
        self._operation = None
        if clear:
            clear()

    def begin(self, name: str) -> OperationToken:
        if self._operation:
            raise SessionBusy(f"{self._operation.name} is already active")
        token = OperationToken(uuid4().hex, name)
        self._operation = token
        return token

    def end(self, token: OperationToken) -> None:
        if token != self._operation:
            raise ValueError("operation token does not match")
        self._operation = None

    def _assert_idle(self) -> None:
        if self._operation:
            raise SessionBusy(f"connection is frozen while {self._operation.name} is active")
