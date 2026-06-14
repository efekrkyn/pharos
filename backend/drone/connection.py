"""Connection helper for talking to a PX4 SITL instance via MAVSDK."""

import asyncio
import logging

from mavsdk import System

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_ADDRESS = "udp://:14540"
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_MAVSDK_SERVER_PORT = 50051


class ConnectionTimeoutError(RuntimeError):
    """Raised when no drone reports a connection within the timeout."""


async def connect_drone(
    system_address: str = DEFAULT_SYSTEM_ADDRESS,
    timeout: float = DEFAULT_CONNECT_TIMEOUT,
    port: int = DEFAULT_MAVSDK_SERVER_PORT,
) -> System:
    """Connect to a drone over MAVLink and wait until it is ready.

    Args:
        system_address: MAVSDK connection string. PX4 SITL broadcasts MAVLink
            on UDP port 14540 by default, so the listening address is
            "udp://:14540".
        timeout: Seconds to wait for the "connected" state before giving up.
        port: gRPC port for the mavsdk_server instance MAVSDK spawns for this
            System. Each concurrent System() needs its own port (default
            50051) to avoid colliding with other instances.

    Returns:
        A System object connected to the drone.

    Raises:
        ConnectionTimeoutError: If no connection is established in time.
    """
    drone = System(port=port)

    logger.info("Connecting to drone at %s ...", system_address)
    await drone.connect(system_address=system_address)

    async def _wait_for_connection() -> None:
        async for state in drone.core.connection_state():
            if state.is_connected:
                logger.info("Drone connected (system discovered)")
                return

    try:
        await asyncio.wait_for(_wait_for_connection(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise ConnectionTimeoutError(
            f"No drone connected within {timeout:.0f}s on {system_address}. "
            "Is PX4 SITL running and configured to send MAVLink to this port?"
        ) from exc

    return drone
