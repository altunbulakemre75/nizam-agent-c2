"""MAVSDK sender safety tests."""
from __future__ import annotations

import pytest

from services.autonomy.mavsdk_sender import MAVSDKSender
from services.autonomy.schemas import InterceptCommand, InterceptPhase, Waypoint


def _command(**overrides) -> InterceptCommand:
    base = dict(
        target_track_id="t1",
        phase=InterceptPhase.APPROACH,
        waypoint=Waypoint(latitude=39.9, longitude=32.8, altitude_m=100.0),
        operator_approved=True,
    )
    base.update(overrides)
    return InterceptCommand(**base)


@pytest.mark.asyncio
async def test_dispatch_refuses_unapproved():
    sender = MAVSDKSender()
    await sender.connect()  # mock
    with pytest.raises(RuntimeError, match="operator_approved"):
        await sender.dispatch(_command(operator_approved=False))


@pytest.mark.asyncio
async def test_dispatch_approved_mock_mode_succeeds():
    sender = MAVSDKSender()
    await sender.connect()  # mock (mavsdk import edilemezse)
    # Mock modda herhangi bir exception atmamalı
    await sender.dispatch(_command())
