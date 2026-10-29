import asyncio
import json
from pathlib import Path

import websockets

TOKEN_PATH = Path("data/.carbon_token")

async def send_and_receive(ws, prompt: str) -> str:
    payload = {"action": "submit", "prompt": prompt}
    await ws.send(json.dumps(payload))
    tokens = []
    while True:
        raw = await ws.recv()
        msg = json.loads(raw)
        evt = msg.get("event")
        if evt == "token":
            tokens.append(msg.get("text", ""))
        elif evt == "completed":
            break
        elif evt in ("error", "failed"):
            return f"ERROR: {msg.get('error') or msg.get('message')}"
    return "".join(tokens).strip()

async def run_memory_check():
    print("=" * 60)
    print("Carbon End-to-End Live Memory Pipeline Verification")
    print("=" * 60)

    token = TOKEN_PATH.read_text(encoding="utf-8").strip()
    headers = {"Authorization": f"Bearer {token}"}
    ws_url = "ws://127.0.0.1:8765/ws/chat"

    async with websockets.connect(ws_url, additional_headers=headers) as ws:
        # 1. Store preference 1
        print("\n[Step 1] Storing: 'Remember this: my favourite programming language is Java.'")
        ans1 = await send_and_receive(ws, "Remember this: my favourite programming language is Java.")
        print(f"Response: {ans1}")
        assert "Java" in ans1 or "remembered" in ans1.lower()

        # 2. Query preference 1 immediately
        print("\n[Step 2] Querying: 'What is my favourite programming language?'")
        ans2 = await send_and_receive(ws, "What is my favourite programming language?")
        print(f"Response: {ans2}")
        assert "Java" in ans2

        # 3. Store preference 2
        print("\n[Step 3] Storing: 'Remember this: my favorite framework is SwiftUI.'")
        ans3 = await send_and_receive(ws, "Remember this: my favorite framework is SwiftUI.")
        print(f"Response: {ans3}")
        assert "SwiftUI" in ans3 or "remembered" in ans3.lower()

        # 4. Query preference 2
        print("\n[Step 4] Querying: 'What is my favorite framework?'")
        ans4 = await send_and_receive(ws, "What is my favorite framework?")
        print(f"Response: {ans4}")
        assert "SwiftUI" in ans4

        # 5. Query unknown preference
        print("\n[Step 5] Querying unknown: 'What is my favorite car?'")
        ans5 = await send_and_receive(ws, "What is my favorite car?")
        print(f"Response: {ans5}")
        assert "do not" in ans5.lower() or "not recorded" in ans5.lower() or "don't" in ans5.lower()

    print("\n✓ Live memory storage, recall, multi-preference, and unknown handling all PASSED!")

if __name__ == "__main__":
    asyncio.run(run_memory_check())

