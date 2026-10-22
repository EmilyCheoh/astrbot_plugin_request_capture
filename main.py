"""Capture final JSON request bodies for OpenAI/Anthropic SDK-backed providers.

This plugin attaches an HTTPX request event hook to provider-owned HTTP clients.
It never changes the outgoing request, response, or agent conversation.
"""
import asyncio
from datetime import datetime
from itertools import count

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools


class RequestCapture(Star):
    _API_PATHS = (
        "/chat/completions",
        "/messages",
        "/responses",
    )

    def __init__(self, context: Context):
        super().__init__(context)
        self.enabled = False
        self.output_dir = StarTools.get_data_dir(
            "astrbot_plugin_request_capture"
        ) / "requests"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._sequence = count(1)
        self._hooked_clients = []

    def _attach_to_providers(self):
        """Attach once per live SDK HTTP client, including newly switched models."""
        attached = 0
        for provider in self.context.get_all_providers():
            sdk_client = getattr(provider, "client", None)
            http_client = getattr(sdk_client, "_client", None)
            if http_client is None:
                continue
            hooks = getattr(http_client, "event_hooks", None)
            if not isinstance(hooks, dict) or not isinstance(hooks.get("request"), list):
                continue
            if any(existing is http_client for existing in self._hooked_clients):
                continue
            hooks["request"].append(self._capture)
            self._hooked_clients.append(http_client)
            attached += 1
        return attached

    async def _capture(self, request):
        """Called by HTTPX after SDK JSON serialization, before network send."""
        if not self.enabled:
            return

        path = request.url.path.rstrip("/")
        if request.method.upper() != "POST":
            return
        if not any(path.endswith(suffix) for suffix in self._API_PATHS):
            return

        # JSON payloads created by these SDK methods are already materialized.
        # Reading .content doesn't consume or replace the outgoing stream.
        try:
            body = request.content
        except Exception as exc:
            logger.warning("[request-capture] Could not access body: %s", exc)
            return

        if not body:
            return

        filename = (
            datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
            + f"_{next(self._sequence):04d}.json"
        )
        target = self.output_dir / filename
        try:
            await asyncio.to_thread(target.write_bytes, body)
            logger.info(
                "[request-capture] %s %s -> %s (%d bytes)",
                request.method,
                request.url.copy_with(query=None),
                target,
                len(body),
            )
        except Exception as exc:
            # Logging must never interrupt an actual LLM call.
            logger.warning("[request-capture] Save failed: %s", exc)

    @filter.command("print")
    async def start_print(self, event: AstrMessageEvent):
        self.enabled = True
        attached = self._attach_to_providers()
        logger.info(
            "[request-capture] ON; hooked %d additional clients; dir=%s",
            attached,
            self.output_dir,
        )
        yield event.plain_result(
            "🐈‍⬛ 请求体记录已开启。后续每次 LLM 请求会保存一个 JSON 文件。"
            f"\n目录：{self.output_dir}"
            "\n发送 /stopprint 关闭。"
        )

    @filter.command("stopprint")
    async def stop_print(self, event: AstrMessageEvent):
        self.enabled = False
        logger.info("[request-capture] OFF")
        yield event.plain_result("🐈‍⬛ 请求体记录已关闭。已有 JSON 文件保留。")

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        # Handles providers created or replaced after /print was issued.
        if self.enabled:
            self._attach_to_providers()

    async def terminate(self):
        self.enabled = False
        for http_client in self._hooked_clients:
            hooks = getattr(http_client, "event_hooks", None)
            if isinstance(hooks, dict):
                requests = hooks.get("request")
                if isinstance(requests, list):
                    requests[:] = [fn for fn in requests if fn != self._capture]
        self._hooked_clients.clear()
