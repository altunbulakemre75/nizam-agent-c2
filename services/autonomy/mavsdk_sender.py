"""MAVSDK bridge — InterceptCommand → PX4 offboard.

Not: Bu üretim-öncesi bir shell. Gerçek donanım/SITL testi
MAVSDK, PX4 SITL (Gazebo) ve güvenlik fence'leri gerektirir.
mavsdk Python paketi opsiyonel import edilir — yoksa mock modu.
"""
from __future__ import annotations

import logging

from services.autonomy.schemas import InterceptCommand, InterceptPhase

log = logging.getLogger(__name__)


class MAVSDKSender:
    """PX4 drone'a MAVSDK offboard komutu gönderici.

    Kullanım:
        sender = MAVSDKSender("udp://:14540")
        await sender.connect()
        await sender.dispatch(intercept_command)
    """

    def __init__(self, connection_url: str = "udp://:14540") -> None:
        self.connection_url = connection_url
        self._drone = None  # mavsdk.System lazy init

    async def connect(self) -> None:
        try:
            from mavsdk import System
        except ImportError:
            log.warning("mavsdk kurulu değil — mock modunda çalışıyor")
            self._drone = "mock"
            return
        self._drone = System()
        await self._drone.connect(system_address=self.connection_url)

    async def dispatch(self, cmd: InterceptCommand) -> None:
        """InterceptCommand'i drone'a gönder. Operatör onayı zorunludur."""
        if not cmd.operator_approved:
            raise RuntimeError("dispatch engeli: operator_approved=False")

        if self._drone == "mock" or self._drone is None:
            log.info(
                "MOCK MAVSDK dispatch: target=%s phase=%s wp=(%.6f,%.6f,%.1f)",
                cmd.target_track_id, cmd.phase.value,
                cmd.waypoint.latitude, cmd.waypoint.longitude, cmd.waypoint.altitude_m,
            )
            return

        # Gerçek MAVSDK path — offboard position + arm
        from mavsdk.offboard import OffboardError, PositionNedYaw

        if cmd.phase == InterceptPhase.ABORT or cmd.phase == InterceptPhase.RTB:
            await self._drone.action.return_to_launch()
            return

        await self._drone.action.arm()
        try:
            await self._drone.offboard.set_position_ned(
                PositionNedYaw(0.0, 0.0, -cmd.waypoint.altitude_m, 0.0)
            )
            await self._drone.offboard.start()
        except OffboardError as exc:
            log.error("offboard hatası: %s", exc)
            raise

    async def close(self) -> None:
        if self._drone and self._drone != "mock":
            # MAVSDK System nesnesi explicit close gerektirmiyor
            pass
