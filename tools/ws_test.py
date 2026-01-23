import asyncio
import websockets
import json

WS_URL = "ws://127.0.0.1:5000/ws"

async def main():
    async with websockets.connect(WS_URL) as ws:
        print(f"Connected: {WS_URL}")
        # İlk mesaj (snapshot) bekliyoruz
        msg = await ws.recv()
        try:
            data = json.loads(msg)
            print("First message (parsed JSON):")
            print(json.dumps(data, indent=2))
        except Exception:
            print("First message (raw):")
            print(msg)

        # Sonraki birkaç mesajı da dinle (track/threat gelirse gör)
        print("\nListening for next 5 messages (Ctrl+C to stop)...")
        for i in range(5):
            msg = await ws.recv()
            try:
                data = json.loads(msg)
                print(f"\n#{i+1}")
                print(json.dumps(data, indent=2))
            except Exception:
                print(f"\n#{i+1} raw:", msg)

if __name__ == "__main__":
    asyncio.run(main())
