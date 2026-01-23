import asyncio
import websockets

async def main():
    url = "ws://127.0.0.1:8000/ws"
    try:
        async with websockets.connect(url) as ws:
            print("CONNECTED:", url)
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=2)
                print("RECV:", msg)
            except asyncio.TimeoutError:
                print("NO MSG (OK) - connection stays open")
    except Exception as e:
        print("FAILED:", repr(e))

asyncio.run(main())
