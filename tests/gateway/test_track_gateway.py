"""Track Gateway (NATS → WebSocket) tests."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from services.gateway.track_gateway import ConnectionHub


@pytest.mark.asyncio
async def test_connect_accepts_and_sends_snapshot():
    # Arrange
    hub = ConnectionHub()
    ws = AsyncMock()

    # Act
    await hub.connect(ws)

    # Assert
    ws.accept.assert_awaited_once()
    ws.send_text.assert_awaited_once()
    payload = ws.send_text.call_args[0][0]
    assert '"type": "snapshot"' in payload
    assert '"tracks": []' in payload


@pytest.mark.asyncio
async def test_broadcast_track_sends_to_all_clients():
    # Arrange
    hub = ConnectionHub()
    ws1, ws2 = AsyncMock(), AsyncMock()
    await hub.connect(ws1)
    await hub.connect(ws2)

    # Act
    await hub.broadcast_track({"track_id": "t1", "state": "confirmed"})

    # Assert — each client got snapshot (on connect) + the track
    assert ws1.send_text.await_count == 2
    assert ws2.send_text.await_count == 2


@pytest.mark.asyncio
async def test_deleted_track_emits_remove_event_and_drops_from_snapshot():
    # Arrange
    hub = ConnectionHub()
    ws = AsyncMock()
    await hub.connect(ws)

    # Act
    await hub.broadcast_track({"track_id": "t1", "state": "confirmed"})
    await hub.broadcast_track({"track_id": "t1", "state": "deleted"})

    # Assert
    last_payload = ws.send_text.await_args_list[-1][0][0]
    assert '"type": "remove"' in last_payload
    assert '"track_id": "t1"' in last_payload
    # re-connect would see empty snapshot
    ws2 = AsyncMock()
    await hub.connect(ws2)
    snapshot_payload = ws2.send_text.await_args[0][0]
    assert '"tracks": []' in snapshot_payload


@pytest.mark.asyncio
async def test_disconnect_removes_client():
    hub = ConnectionHub()
    ws = AsyncMock()
    await hub.connect(ws)
    await hub.disconnect(ws)
    # yeni track yayınlandığında artık o ws'e gitmez
    ws.send_text.reset_mock()
    await hub.broadcast_track({"track_id": "t1", "state": "confirmed"})
    ws.send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_track_id_is_ignored():
    hub = ConnectionHub()
    ws = AsyncMock()
    await hub.connect(ws)
    ws.send_text.reset_mock()
    await hub.broadcast_track({"state": "confirmed"})  # track_id yok
    ws.send_text.assert_not_awaited()
