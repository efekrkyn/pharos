"""Entry point: connect to PX4 SITL and stream position telemetry."""

import asyncio
import logging

from drone.connection import ConnectionTimeoutError, connect_drone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def print_position(drone) -> None:
    """Print latitude, longitude, and altitude each time position updates."""
    async for position in drone.telemetry.position():
        print(
            f"lat={position.latitude_deg:.7f} "
            f"lon={position.longitude_deg:.7f} "
            f"abs_alt={position.absolute_altitude_m:.2f}m "
            f"rel_alt={position.relative_altitude_m:.2f}m"
        )


async def main() -> None:
    """Connect to the drone and stream telemetry until interrupted."""
    try:
        drone = await connect_drone()
    except ConnectionTimeoutError as exc:
        logger.error("%s", exc)
        return

    await print_position(drone)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stopped by user")
