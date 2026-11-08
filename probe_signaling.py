"""Check an app-provided A4x session ticket without logging its credentials."""

import argparse
import asyncio
import inspect
import json
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import websockets


async def probe(ticket_file: Path) -> None:
    document = json.loads(ticket_file.read_text(encoding="utf-8"))
    ticket = document.get("data", document)
    endpoint = urlsplit(ticket["signalServer"])
    if endpoint.scheme != "wss" or not endpoint.hostname:
        raise ValueError("The camera session must use a TLS WebSocket endpoint")
    if not endpoint.hostname.endswith(".smartvideogo.com"):
        raise ValueError("Unexpected signaling domain; inspect the ticket before use")
    if ticket.get("expirationTime", 0) < time.time() * 1000:
        raise ValueError("Session ticket expired; refresh it from the phone")
    uri = urlunsplit((endpoint.scheme, endpoint.netloc,
                      ticket.get("websocketPath") or endpoint.path, "", ""))
    options = {"open_timeout": 12, "close_timeout": 3}
    if "proxy" in inspect.signature(websockets.connect).parameters:
        options["proxy"] = None
    print("Connecting to", endpoint.hostname, flush=True)
    async with websockets.connect(uri, **options) as connection:
        await connection.send(json.dumps({
            "method": "AUTH", "clientType": "app", "status": "normal",
            "accessToken": ticket["accessToken"], "id": ticket["id"],
        }))
        for _ in range(6):
            raw = await asyncio.wait_for(connection.recv(), timeout=12)
            response = json.loads(raw)
            method = response.get("method", "unknown")
            code = response.get("code")
            print("Signaling response:", method, "code:", code, flush=True)
            if method == "AUTH_RESPONSE":
                if code != 0:
                    raise RuntimeError("Signaling rejected the session ticket")
                print("Desktop signaling authentication succeeded.", flush=True)
                return
        raise RuntimeError("No authentication response received")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticket", type=Path)
    args = parser.parse_args()
    asyncio.run(probe(args.ticket))
