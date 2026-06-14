"""Local HTTP API transport: anything that can POST can talk to ALFRED.

iOS Shortcuts, Android Tasker, a cron job, curl in another terminal: one
POST to /message and the reply comes back in the response body. Built on
aiohttp (already in the tree via discord.py), bound to 127.0.0.1 by
default, and refusing to start without a bearer token, because local and
sovereign is the default, not a suggestion.

Replies are buffered per request: the core sends through TransportPort as
usual, the adapter captures sends addressed to the request's channel, and
the HTTP response carries them. Proactive sends to "http:" channels after
the request has completed have nowhere to go and are dropped with a log;
proactive output belongs on a persistent transport (Discord, Telegram).

The body is {"text": "..."} with an optional "provenance": "owner" |
"external". It defaults to owner (direct owner typing); automation that
forwards third-party content (an email body, a calendar invite) must set
"external" so the untrusted-content gate applies and the forwarded text
cannot run a command or auto-execute anything above read-only.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from collections.abc import Awaitable, Callable

from aiohttp import web

from alfred.config import HttpConfig
from alfred.domain.schemas import InboundMessage, new_id
from alfred.ports.transport import OutboundMessage

logger = logging.getLogger(__name__)

CHANNEL_PREFIX = "http:"


def authorised(header_value: str | None, token: str) -> bool:
    """Constant-time bearer check; a missing or malformed header never passes."""
    if not header_value or not header_value.startswith("Bearer "):
        return False
    presented = header_value.removeprefix("Bearer ").strip()
    return hmac.compare_digest(presented, token)


class HttpTransportAdapter:
    """TransportPort plus a tiny aiohttp server for request/response chat."""

    def __init__(
        self,
        config: HttpConfig,
        handler: Callable[[InboundMessage], Awaitable[None]],
    ) -> None:
        self._config = config
        self._handler = handler
        self._token = config.token()  # raises without one; never logged
        self._handler_lock = asyncio.Lock()
        self._buffers: dict[str, list[str]] = {}
        self._runner: web.AppRunner | None = None

    def make_app(self) -> web.Application:
        """The wired aiohttp app; separate from start() so tests can host it."""
        app = web.Application()
        app.router.add_post("/message", self._handle_message)
        app.router.add_get("/health", self._handle_health)
        return app

    async def start(self) -> None:
        self._runner = web.AppRunner(self.make_app())
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._config.host, self._config.port)
        await site.start()
        logger.info(
            "http api listening on %s:%d", self._config.host, self._config.port
        )
        # Match the other transports' contract: start() blocks until cancelled.
        await asyncio.Event().wait()

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def _handle_message(self, request: web.Request) -> web.Response:
        if not authorised(request.headers.get("Authorization"), self._token):
            # 404, not 401: an unauthenticated caller learns nothing, not
            # even that ALFRED lives here.
            return web.json_response({"error": "not found"}, status=404)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "body must be JSON"}, status=400)
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str) or not text.strip():
            return web.json_response(
                {"error": 'body must be {"text": "..."}'}, status=400
            )
        # Direct owner typing is owner-authority, but automation forwarding
        # third-party content (an email body, a calendar invite) MUST mark it
        # external so the untrusted-content gate applies: external content can
        # never run an owner command, approve a proposal, or auto-execute
        # anything above read-only.
        provenance = payload.get("provenance", "owner")
        if provenance not in ("owner", "external"):
            return web.json_response(
                {"error": 'provenance must be "owner" or "external"'}, status=400
            )

        channel = f"{CHANNEL_PREFIX}{new_id()}"
        self._buffers[channel] = []
        try:
            async with self._handler_lock:
                await self._handler(
                    InboundMessage(channel=channel, text=text, provenance=provenance)
                )
            replies = self._buffers.get(channel, [])
        finally:
            self._buffers.pop(channel, None)
        return web.json_response({"replies": replies})

    async def send(self, message: OutboundMessage) -> None:
        buffer = self._buffers.get(message.channel)
        if buffer is None:
            logger.warning(
                "dropping send to finished or unknown http channel %s",
                message.channel,
            )
            return
        buffer.append(message.text)

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
