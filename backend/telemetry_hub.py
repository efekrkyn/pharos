"""Shared state for broadcasting drone telemetry to WebSocket clients."""

import asyncio
import logging

from fastapi import WebSocket

logger = logging.getLogger(__name__)

# Sent to clients before the drone has connected, and used as the initial
# state for newly connected clients.
WAITING_STATE = {
    "status": "waiting",
    "lat": None,
    "lon": None,
    "abs_alt": None,
    "rel_alt": None,
}


class TelemetryHub:
    """Tracks connected WebSocket clients and the latest telemetry snapshot."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self.latest: dict = dict(WAITING_STATE)

    async def register(self, websocket: WebSocket) -> None:
        """Accept a client and send it the current telemetry snapshot."""
        await websocket.accept()
        async with self._lock:
            self._clients.add(websocket)
        await websocket.send_json(self.latest)

    async def unregister(self, websocket: WebSocket) -> None:
        """Remove a client, ignoring it if already removed."""
        async with self._lock:
            self._clients.discard(websocket)

    async def broadcast(self, message: dict) -> None:
        """Store the latest telemetry and send it to every connected client."""
        self.latest = message
        async with self._lock:
            clients = list(self._clients)

        for websocket in clients:
            try:
                await websocket.send_json(message)
            except Exception:
                logger.debug("Dropping unresponsive WebSocket client")
                await self.unregister(websocket)
