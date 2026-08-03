"""Minimal Music Assistant websocket client, for driving playback from a shell.

Runs inside the Music Assistant container:

    docker exec app-<alloc> /app/venv/bin/python /tmp/ma_cli.py players
    docker exec app-<alloc> /app/venv/bin/python /tmp/ma_cli.py raw <command> '<json>'

The token is read from /tmp/.ma-token, staged there by tools/ma_token.sh. It is
not minted here and not passed on a command line: MA opens its databases with
`PRAGMA locking_mode=exclusive`, so auth.db cannot be written while MA runs,
and a command line is readable by anything that can see /proc.
"""

import asyncio
import json
import os
import sys

import aiohttp

URL = os.environ.get("MA_WS_URL", "ws://127.0.0.1:8095/ws")
TOKEN_FILE = "/tmp/.ma-token"


def read_token() -> str:
    try:
        with open(TOKEN_FILE) as handle:
            token = handle.read().strip()
    except OSError:
        raise SystemExit(
            f"no token at {TOKEN_FILE}; run tools/ma_token.sh from a checkout"
        ) from None
    if not token:
        raise SystemExit(f"{TOKEN_FILE} is empty")
    return token


async def call(ws, command, args, msg_id):
    await ws.send_json({"message_id": str(msg_id), "command": command, "args": args})
    while True:
        msg = await asyncio.wait_for(ws.receive_json(), timeout=90)
        if msg.get("message_id") == str(msg_id):
            return msg


async def run(what, token):
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(URL) as ws:
            await asyncio.wait_for(ws.receive_json(), timeout=15)
            auth = await call(ws, "auth", {"token": token}, 0)
            if "error_code" in auth:
                raise SystemExit(f"auth failed: {auth.get('details')}")

            if what == "players":
                res = await call(ws, "players/all", {}, 1)
                if "error_code" in res:
                    raise SystemExit(json.dumps(res))
                for p in res.get("result", []):
                    print(
                        f"{str(p.get('provider')):<26} {str(p.get('player_id')):<38} "
                        f"{str(p.get('type')):<8} {str(p.get('state')):<10} "
                        f"{p.get('display_name')}"
                    )
                return

            if what == "raw":
                command = sys.argv[2]
                args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
                res = await call(ws, command, args, 1)
                print(json.dumps(res, indent=2, default=str))
                return

            raise SystemExit(f"unknown: {what}")


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: ma_cli.py players | raw <command> [json args]")
    asyncio.run(run(sys.argv[1], read_token()))


main()
