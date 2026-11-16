"""One Supabase client per thread, behind a single shared name.

The bot runs every database call in a worker thread (``asyncio.to_thread``,
APScheduler jobs, the Flask dashboard). A supabase-py client keeps one HTTP/2
connection, and two threads using that connection at the same moment fail
with ``httpx.ReadError: [Errno 11] Resource temporarily unavailable`` — in
production that dropped kitchen numpad entries, quarantine rows and cooked
form posts whenever a receipt and a kitchen tap landed together.

``ThreadLocalClient`` looks like the client but hands each thread its own,
created on first use, so no connection is ever shared between threads.
"""

from __future__ import annotations

import threading
from typing import Any, Callable


class ThreadLocalClient:
    """Proxy for a client object: every attribute comes from the calling
    thread's own instance (built by ``factory`` the first time that thread
    asks). Safe to pass around and to use from any thread."""

    def __init__(self, factory: Callable[[], Any]) -> None:
        # Set through object.__setattr__-free names: __getattr__ is only
        # consulted for names that are not found normally.
        self._factory = factory
        self._local = threading.local()

    def _client(self) -> Any:
        client = getattr(self._local, "client", None)
        if client is None:
            client = self._factory()
            self._local.client = client
        return client

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return getattr(self._client(), name)
