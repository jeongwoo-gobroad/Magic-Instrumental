# SPDX-License-Identifier: GPL-3.0-only
import asyncio
import json

import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from relay.server import Relay, media_info, transport_state

MEDIA = {"source": "a" * 64, "duration": 60}
STATE = {"position": 0, "rate": 1, "key": 0, "bpm": 120, "beats": 4, "unit": 4,
         "origin": 0, "count": False, "countBars": 1, "loop": None, "loopCount": False}


async def receive(ws, kind):
    async with asyncio.timeout(5):
        while True:
            value = await ws.receive_json()
            if value["type"] == kind:
                return value


def test_room_permissions_clock_reservation_and_ephemeral_share(tmp_path):
    async def run():
        relay = Relay(public_url="https://relay.example", player_url="https://player.example/",
                      storage=tmp_path, origins=["https://player.example"])
        async with TestServer(relay.app()) as server, ClientSession() as client:
            url = str(server.make_url("/"))
            response = await client.post(url + "api/rooms", json={"media": MEDIA})
            leader = await response.json()
            assert len(leader["code"]) == 8 and leader["code"].isalnum()
            assert leader["token"] not in leader["invite"]
            lw = await client.ws_connect(url + "api/ws")
            await lw.send_json(leader)
            assert (await receive(lw, "welcome"))["leader"]
            response = await client.post(url + "api/join", json={"code": leader["code"]})
            member = await response.json()
            mw = await client.ws_connect(url + "api/ws")
            await mw.send_json(member)
            assert not (await receive(mw, "welcome"))["leader"]
            await lw.send_json({"type": "ping", "id": 123})
            assert abs((await receive(lw, "pong"))["time"] - relay.now()) < 100
            await mw.send_json({"type": "command", "action": "play", "state": STATE})
            assert "리더만" in (await receive(mw, "error"))["message"]
            for ws in (lw, mw):
                await ws.send_json({"type": "ready", "ready": True, "media": MEDIA, "rtt": 20})
            await receive(mw, "presence")
            await asyncio.sleep(.05)
            await lw.send_json({"type": "command", "action": "play", "state": STATE})
            command = await receive(lw, "command")
            assert 800 < command["at"] - relay.now() <= 1000
            assert (await receive(mw, "command"))["seq"] == command["seq"]
            await lw.send_json({"type": "ack", "seq": command["seq"]})
            # A missing member acknowledgement cancels the whole reservation.
            assert "일부 기기" in (await receive(lw, "halt"))["reason"]
            headers = {"Authorization": "Bearer " + leader["token"]}
            code = leader["code"]
            fake_file = b"opaque .magic content " * 10
            response = await client.post(url + f"api/rooms/{code}/file", data=fake_file,
                                         headers=headers)
            assert response.status == 200
            assert (await client.get(url + f"api/rooms/{code}/file")).status == 403
            member_headers = {"Authorization": "Bearer " + member["token"]}
            response = await client.get(url + f"api/rooms/{code}/file", headers=member_headers)
            assert await response.read() == fake_file
            response = await client.get(url + f"api/rooms/{code}/qr", headers=headers)
            assert (await response.read()).startswith(b"\x89PNG")
            assert (await client.post(url + f"api/rooms/{code}/file", data=fake_file,
                                      headers=member_headers)).status == 403
            response = await client.post(url + "api/rooms", json={"media": MEDIA},
                                         headers={"Origin": "https://untrusted.example"})
            assert response.status == 403
            await lw.close()
            await asyncio.sleep(.1)
            assert not relay.rooms and not list(tmp_path.glob("*.magic"))
            await mw.close()
    asyncio.run(run())


def test_payloads_reject_nonfinite_or_invalid_ranges():
    with pytest.raises(ValueError):
        media_info({"source": "x", "duration": 60})
    for change in ({"position": float("nan")}, {"rate": 3}, {"loop": [20, 10]},
                   {"key": "1"}, {"beats": 4.2}, {"count": 1}):
        with pytest.raises(ValueError):
            transport_state(STATE | change, 60)
    assert json.dumps(transport_state(STATE, 60))
