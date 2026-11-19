# SPDX-License-Identifier: GPL-3.0-only
"""Ephemeral coordination and opt-in opaque file transfer; no media processing."""

import asyncio
import contextlib
import hashlib
import io
import json
import math
import os
import re
import secrets
import tempfile
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import segno
from aiohttp import WSMsgType, web

MAX_FILE = 1024**3
MAX_ROOMS = 16
MAX_MEMBERS = 16
TTL = 4 * 3600
ROOT = Path(__file__).resolve().parents[1]
STATIC = {"index.html", "sw.js", "manifest.webmanifest", "icon-192.png", "icon-512.png"}
STATE_KEYS = {"position", "rate", "key", "bpm", "beats", "unit", "origin", "count",
              "countBars", "loop", "loopCount"}


def finite(value, lo, hi):
    return type(value) in (int, float) and math.isfinite(value) and lo <= value <= hi


def media_info(value):
    if not isinstance(value, dict):
        raise ValueError("곡을 먼저 열어 주세요.")
    digest = value.get("source")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("곡의 검증 정보가 없습니다. 새 버전에서 .magic을 저장하세요.")
    if not finite(value.get("duration"), .01, 3600):
        raise ValueError("곡 길이가 올바르지 않습니다.")
    return {"source": digest, "duration": value["duration"]}


def transport_state(value, duration):
    if not isinstance(value, dict) or set(value) != STATE_KEYS:
        raise ValueError("재생 설정이 올바르지 않습니다.")
    for key, lo, hi in (("position", 0, duration), ("rate", .25, 2), ("key", -24, 24),
                        ("bpm", 20, 400), ("beats", 1, 16), ("origin", 0, 1800)):
        if not finite(value[key], lo, hi):
            raise ValueError("재생 설정의 범위를 확인하세요.")
    if (type(value["key"]) is not int or type(value["beats"]) is not int
            or value["unit"] not in (2, 4, 8, 16) or value["countBars"] not in (1, 2, 4)
            or type(value["count"]) is not bool or type(value["loopCount"]) is not bool):
        raise ValueError("박자·카운트 설정이 올바르지 않습니다.")
    loop = value["loop"]
    if loop is not None and not (
        isinstance(loop, list) and len(loop) == 2 and finite(loop[0], 0, duration)
        and finite(loop[1], loop[0] + .001, duration)
        and loop[0] <= value["position"] < loop[1]
    ):
        raise ValueError("반복 구간이 올바르지 않습니다.")
    return value


class Peer:
    def __init__(self, name, leader=False):
        self.token = secrets.token_urlsafe(32)
        self.name = str(name or ("리더" if leader else "멤버"))[:40]
        self.leader = leader
        self.ws = None
        self.ready = False
        self.rtt = 1000.0
        self.seen = time.monotonic()


class Room:
    def __init__(self, code, media, name):
        self.code, self.media = code, media
        leader = Peer(name, True)
        self.peers = {leader.token: leader}
        self.leader = leader
        self.created = time.monotonic()
        self.seq = 0
        self.pending = None
        self.shared = None
        self.uploading = False


class Relay:
    def __init__(self, *, public_url, player_url, storage, origins=()):
        self.public_url = public_url.rstrip("/")
        self.player_url = player_url
        self.storage = Path(storage)
        self.storage.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.rooms = {}
        self.limits = defaultdict(deque)
        self.origins = set(origins) | {self.public_url, "https://jeongwoo-gobroad.github.io"}
        self.epoch = time.time() * 1000 - time.monotonic() * 1000
        self.cleanup_task = None
        self.reserved_bytes = 0

    def now(self):
        # A monotonic UTC mapping avoids a running room jumping on an NTP correction.
        return self.epoch + time.monotonic() * 1000

    def limited(self, request, group, count):
        # Trust the single reverse proxy only when it connects from loopback.
        remote = request.remote
        if remote in {"127.0.0.1", "::1"}:
            remote = request.headers.get("X-Forwarded-For", remote).split(",")[-1].strip()
        key = (remote, group)
        bucket = self.limits[key]
        now = time.monotonic()
        while bucket and bucket[0] < now - 60:
            bucket.popleft()
        if len(bucket) >= count:
            raise web.HTTPTooManyRequests(text="잠시 기다린 뒤 다시 시도하세요.")
        bucket.append(now)
        return remote

    async def body(self, request):
        if request.content_length is None or request.content_length > 8192:
            raise web.HTTPBadRequest(text="요청이 너무 큽니다.")
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("요청 형식을 확인하세요.")
        return data

    def authorized(self, request, leader=False):
        room = self.rooms.get(request.match_info["code"])
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        peer = room.peers.get(token) if room else None
        if not peer or (leader and not peer.leader):
            raise web.HTTPForbidden(text="방 참가 권한이 없습니다.")
        peer.seen = time.monotonic()
        return room, peer

    def invite(self, room):
        return self.player_url.split("#")[0] + "#" + urlencode({
            "relay": self.public_url, "join": room.code,
        })

    async def broadcast(self, room, message):
        for peer in list(room.peers.values()):
            if peer.ws and not peer.ws.closed:
                with contextlib.suppress(ConnectionError, RuntimeError):
                    await peer.ws.send_json(message)

    async def presence(self, room):
        await self.broadcast(room, {
            "type": "presence", "code": room.code, "shared": room.shared is not None,
            "peers": [{"name": p.name, "leader": p.leader, "ready": p.ready,
                       "connected": bool(p.ws and not p.ws.closed)}
                      for p in room.peers.values()],
        })

    async def halt(self, room, reason):
        room.seq += 1
        room.pending = None
        await self.broadcast(room, {"type": "halt", "seq": room.seq, "reason": reason})

    async def close_room(self, room, reason):
        self.rooms.pop(room.code, None)
        if room.shared:
            room.shared["path"].unlink(missing_ok=True)
            room.shared = None
        await self.halt(room, reason)
        for peer in list(room.peers.values()):
            if peer.ws:
                await peer.ws.close(code=1001, message=b"Room closed")

    async def create(self, request):
        ip = self.limited(request, "create", 5)
        data = await self.body(request)
        if len(self.rooms) >= MAX_ROOMS:
            raise web.HTTPServiceUnavailable(text="합주 방이 가득 찼습니다.")
        media = media_info(data.get("media"))
        while True:
            seed = f"{ip}:{time.time_ns()}:{secrets.token_hex(32)}".encode()
            number = int.from_bytes(hashlib.sha256(seed).digest()[:8], "big")
            alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            code = "".join(alphabet[(number // 36**i) % 36] for i in range(8))
            if code not in self.rooms:
                break
        room = Room(code, media, data.get("name"))
        self.rooms[code] = room
        return web.json_response({"code": code, "token": room.leader.token,
                                  "invite": self.invite(room), "media": media})

    async def join(self, request):
        self.limited(request, "join", 15)
        data = await self.body(request)
        code = str(data.get("code", "")).upper()
        room = self.rooms.get(code)
        if not room or not room.leader.ws or room.leader.ws.closed:
            raise web.HTTPNotFound(text="열린 방을 찾지 못했습니다. 초대 코드를 확인하세요.")
        if len(room.peers) >= MAX_MEMBERS:
            raise web.HTTPConflict(text="한 방에는 최대 16명이 참가할 수 있습니다.")
        peer = Peer(data.get("name"))
        room.peers[peer.token] = peer
        await self.halt(room, "새 멤버가 들어왔습니다. 모두 준비한 뒤 다시 시작하세요.")
        await self.presence(room)
        return web.json_response({"code": code, "token": peer.token, "media": room.media})

    async def qr(self, request):
        room, _ = self.authorized(request)
        output = io.BytesIO()
        segno.make(self.invite(room), micro=False).save(output, kind="png", scale=5)
        return web.Response(body=output.getvalue(), content_type="image/png")

    async def websocket(self, request):
        self.limited(request, "ws", 40)
        ws = web.WebSocketResponse(timeout=1, heartbeat=10, receive_timeout=30, max_msg_size=8192)
        await ws.prepare(request)
        room = peer = None
        try:
            auth = await ws.receive_json(timeout=5)
            room = self.rooms.get(str(auth.get("code", "")))
            peer = room.peers.get(auth.get("token")) if room else None
            if not peer or peer.ws:
                await ws.close(code=1008, message=b"Invalid session")
                return ws
            peer.ws = ws
            await ws.send_json({"type": "welcome", "leader": peer.leader})
            await self.presence(room)
            bucket = deque()
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    break
                now = time.monotonic()
                while bucket and bucket[0] < now - 1:
                    bucket.popleft()
                if len(bucket) >= 30:
                    await ws.close(code=1008, message=b"Too many messages")
                    break
                bucket.append(now)
                peer.seen = now
                data = None
                try:
                    data = json.loads(message.data)
                    if not isinstance(data, dict):
                        raise ValueError("요청 형식을 확인하세요.")
                    await self.message(room, peer, data)
                except (ValueError, KeyError, TypeError) as error:
                    if isinstance(data, dict) and data.get("type") == "ready":
                        peer.ready = False
                        await self.halt(room, "멤버의 준비 정보를 확인하지 못해 멈췄습니다.")
                        await self.presence(room)
                    await ws.send_json({"type": "error", "message": str(error)[:200]})
        except (TimeoutError, ValueError, TypeError, AttributeError):
            await ws.close(code=1008, message=b"Invalid message")
        finally:
            if peer and peer.ws is ws:
                peer.ready = False
                room.peers.pop(peer.token, None)
                if peer.leader:
                    await self.close_room(room, "리더가 연결을 종료했습니다.")
                elif room.code in self.rooms:
                    await self.halt(room, "멤버 연결이 끊겨 합주를 멈췄습니다.")
                    await self.presence(room)
        return ws

    async def message(self, room, peer, data):
        kind = data.get("type")
        if kind == "ping":
            if not finite(data.get("id"), 0, 1e12):
                raise ValueError("시간 확인 요청이 올바르지 않습니다.")
            await peer.ws.send_json({"type": "pong", "id": data["id"], "time": self.now()})
        elif kind == "ready":
            peer.ready = False
            if data.get("ready") is True:
                media = media_info(data.get("media"))
                if (media["source"] != room.media["source"]
                        or abs(media["duration"] - room.media["duration"]) > .05):
                    raise ValueError("리더와 같은 원곡 음원으로 만든 .magic을 열어 주세요.")
                if not finite(data.get("rtt"), 0, 1300):
                    raise ValueError("연결 지연이 큽니다. 네트워크를 확인한 뒤 준비를 누르세요.")
                peer.rtt = data["rtt"]
                peer.ready = True
            if not peer.ready:
                await self.halt(room, "준비가 해제되어 합주를 멈췄습니다.")
            await self.presence(room)
        elif kind == "command":
            if not peer.leader:
                raise ValueError("재생 조작은 리더만 할 수 있습니다.")
            if room.pending:
                raise ValueError("예약한 동작이 끝난 뒤 다시 조작하세요.")
            action = data.get("action")
            if action not in {"play", "seek", "pause"}:
                raise ValueError("지원하지 않는 재생 동작입니다.")
            if not all(p.ready and p.ws and not p.ws.closed for p in room.peers.values()):
                raise ValueError("모두 ‘합주 준비’를 누른 뒤 조작하세요.")
            state = transport_state(data.get("state"), room.media["duration"])
            delay = min(5, max(1, math.ceil(max(p.rtt for p in room.peers.values()) * .003 + .6)))
            room.seq += 1
            event = {"type": "command", "seq": room.seq, "action": action,
                     "resume": data.get("resume") is True, "state": state,
                     "at": self.now() + delay * 1000, "delay": delay}
            room.pending = {"event": event, "acks": set(), "tokens": set(room.peers)}
            await self.broadcast(room, event)
        elif kind == "ack":
            pending = room.pending
            if pending and data.get("seq") == pending["event"]["seq"]:
                pending["acks"].add(peer.token)
        else:
            raise ValueError("지원하지 않는 요청입니다.")

    async def share(self, request):
        room, _ = self.authorized(request, True)
        self.limited(request, "upload", 4)
        length = request.content_length
        if length is None or not 22 <= length <= MAX_FILE:
            raise web.HTTPBadRequest(text="공유 파일은 최대 1 GB입니다.")
        used = sum(r.shared["size"] for r in self.rooms.values() if r.shared)
        if room.uploading or used + self.reserved_bytes + length > 2 * MAX_FILE:
            raise web.HTTPServiceUnavailable(text="공유 저장 공간을 준비할 수 없습니다.")
        room.uploading = True
        self.reserved_bytes += length
        path = self.storage / (secrets.token_hex(24) + ".magic")
        digest, size = hashlib.sha256(), 0
        try:
            with path.open("xb") as output:
                os.chmod(path, 0o600)
                async for chunk in request.content.iter_chunked(1024 * 1024):
                    size += len(chunk)
                    if size > length or room.code not in self.rooms:
                        raise web.HTTPBadRequest(text="파일 공유가 취소되었습니다.")
                    output.write(chunk)
                    digest.update(chunk)
            if size != length:
                raise web.HTTPBadRequest(text="파일 전송이 끝나지 않았습니다.")
            if room.shared:
                room.shared["path"].unlink(missing_ok=True)
            room.shared = {"path": path, "size": size, "sha256": digest.hexdigest()}
            await self.presence(room)
            return web.json_response({"shared": True, "bytes": size})
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        finally:
            room.uploading = False
            self.reserved_bytes -= length

    async def download(self, request):
        room, _ = self.authorized(request)
        self.limited(request, "download", 8)
        if not room.shared:
            raise web.HTTPNotFound(text="리더가 아직 파일을 공유하지 않았습니다.")
        return web.FileResponse(room.shared["path"], headers={
            "Content-Type": "application/octet-stream",
            "Content-Disposition": 'attachment; filename="shared-practice.magic"',
            "X-Content-SHA256": room.shared["sha256"],
        })

    async def clean(self):
        while True:
            await asyncio.sleep(.1)
            now = time.monotonic()
            for room in list(self.rooms.values()):
                if now - room.created > TTL or (not room.leader.ws and now - room.created > 30):
                    await self.close_room(room, "합주 방의 사용 시간이 끝났습니다.")
                    continue
                for token, peer in list(room.peers.items()):
                    if not peer.ws and now - peer.seen > 30:
                        room.peers.pop(token, None)
                        await self.presence(room)
                pending = room.pending
                if pending:
                    left = pending["event"]["at"] - self.now()
                    if left < 350 and pending["acks"] != pending["tokens"]:
                        await self.halt(room, "일부 기기에 예약이 도착하지 않아 취소했습니다.")
                    elif left <= 0:
                        room.pending = None
            for key, bucket in list(self.limits.items()):
                if not bucket or bucket[-1] < now - 60:
                    self.limits.pop(key, None)

    async def lifecycle(self, app):
        self.cleanup_task = asyncio.create_task(self.clean())
        yield
        self.cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.cleanup_task
        for room in list(self.rooms.values()):
            await self.close_room(room, "중계 서버가 다시 시작됩니다.")

    async def static(self, request):
        name = request.match_info.get("name", "index.html")
        if name == "landing":
            path = ROOT / "landing/index.html"
        elif name in STATIC:
            path = ROOT / "web_player" / name
        elif name in {"LICENSE", "THIRD_PARTY_NOTICES.md"}:
            path = ROOT / name
        else:
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "no-cache"})

    def app(self):
        async def health(_):
            return web.json_response({"ok": True, "protocol": 1})

        @web.middleware
        async def boundary(request, handler):
            origin = request.headers.get("Origin")
            if origin and origin not in self.origins:
                raise web.HTTPForbidden(text="허용되지 않은 재생기 주소입니다.")
            try:
                response = web.Response() if request.method == "OPTIONS" else await handler(request)
            except (ValueError, KeyError, TypeError):
                response = web.json_response({"error": "요청 내용을 확인하세요."}, status=400)
            except web.HTTPException as error:
                response = web.json_response({"error": error.text}, status=error.status)
            if not response.prepared:
                response.headers.update({"Cache-Control": "no-store",
                                         "X-Content-Type-Options": "nosniff"})
                if origin:
                    response.headers.update({
                        "Access-Control-Allow-Origin": origin, "Vary": "Origin",
                        "Access-Control-Allow-Headers": "Authorization,Content-Type",
                        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
                    })
            return response

        app = web.Application(middlewares=[boundary], client_max_size=MAX_FILE)
        app.cleanup_ctx.append(self.lifecycle)
        app.add_routes([
            web.get("/api/health", health),
            web.post("/api/rooms", self.create), web.post("/api/join", self.join),
            web.get("/api/ws", self.websocket),
            web.get("/api/rooms/{code}/qr", self.qr),
            web.post("/api/rooms/{code}/file", self.share),
            web.get("/api/rooms/{code}/file", self.download),
            web.get("/", self.static), web.get("/{name}", self.static),
        ])
        return app


def main():
    public_url = os.environ["MAGIC_RELAY_URL"].rstrip("/")
    if urlsplit(public_url).scheme != "https":
        raise SystemExit("MAGIC_RELAY_URL must be a public HTTPS URL behind a TLS proxy.")
    with tempfile.TemporaryDirectory(prefix="magic-relay-") as storage:
        relay = Relay(
            public_url=public_url, player_url=os.getenv("MAGIC_PLAYER_URL", public_url + "/"),
            storage=storage, origins=os.getenv("MAGIC_ALLOWED_ORIGINS", "").split(","),
        )
        web.run_app(relay.app(), host="127.0.0.1", port=int(os.getenv("MAGIC_RELAY_PORT", "13580")),
                    access_log=None, print=None)


if __name__ == "__main__":
    main()
