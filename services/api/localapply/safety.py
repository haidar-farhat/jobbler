"""The kill switch.

Process-global, checked by the executor before **every** action rather than once per loop
iteration. A long-running action can finish, but nothing new starts once this is engaged.

Deliberately not in the database: stopping automation must not depend on a healthy DB
connection.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime


class KillSwitch:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._engaged = False
        self._reason: str | None = None
        self._engaged_at: datetime | None = None

    @property
    def engaged(self) -> bool:
        return self._engaged

    @property
    def reason(self) -> str | None:
        return self._reason

    @property
    def engaged_at(self) -> datetime | None:
        return self._engaged_at

    def engage(self, reason: str = "Stopped by user") -> None:
        with self._lock:
            self._engaged = True
            self._reason = reason
            self._engaged_at = datetime.now(UTC)

    def reset(self) -> None:
        """Re-arming is an explicit, separate act from stopping."""
        with self._lock:
            self._engaged = False
            self._reason = None
            self._engaged_at = None

    def status(self) -> dict:
        return {
            "engaged": self._engaged,
            "reason": self._reason,
            "engaged_at": self._engaged_at.isoformat() if self._engaged_at else None,
        }


class AutomationHalted(RuntimeError):
    """Raised when an action is attempted while the kill switch is engaged."""


#: The single global instance. Import this, do not construct another.
KILL_SWITCH = KillSwitch()
