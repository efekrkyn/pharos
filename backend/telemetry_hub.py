"""Shared state for broadcasting drone telemetry to WebSocket clients."""

import asyncio
import logging

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class TelemetryHub:
    """Tracks connected WebSocket clients and the latest telemetry snapshot per drone."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        # drone_id -> latest telemetry message for that drone.
        self.latest: dict[str, dict] = {}

    async def register(self, websocket: WebSocket) -> None:
        """Accept a client and send it the current telemetry snapshot for every drone."""
        await websocket.accept()
        async with self._lock:
            self._clients.add(websocket)
        for message in self.latest.values():
            await websocket.send_json(message)

    async def unregister(self, websocket: WebSocket) -> None:
        """Remove a client, ignoring it if already removed."""
        async with self._lock:
            self._clients.discard(websocket)

    async def broadcast(self, message: dict) -> None:
        """Store the latest telemetry for message["drone_id"] and send it to every client."""
        self.latest[message["drone_id"]] = message
        async with self._lock:
            clients = list(self._clients)

        for websocket in clients:
            try:
                await websocket.send_json(message)
            except Exception:
                logger.debug("Dropping unresponsive WebSocket client")
                await self.unregister(websocket)
