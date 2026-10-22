"""Capture outgoing JSON request bodies from OpenAI, Anthropic and Gemini.

HTTPX hooks capture the original on-the-wire body without modifying requests.
The commands manage the saved local JSON files and export pretty copies on demand.
"""

import asyncio
import json
import math
import re
import shlex
import tempfile
from datetime import datetime
from itertools import count
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File
from astrbot.api.star import Context, Star, StarTools


class RequestCapture(Star):
    _GEMINI_PATH = re.compile(r"(?:^|/)models/[^/]+:(?:streamGenerateContent|generateContent)$")
    _API_PATHS = (
        "/chat/completions",
        "/messages",
        "/responses",
    )
    _PAGE_SIZE = 15
    _LATEST = {"最新", "latest", "new", "first"}
    _DELETE = {"删除", "delete", "remove", "del", "rm"}

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
            # OpenAI / Anthropic expose the SDK-owned HTTPX client here.
            # Gemini uses Google GenAI, with an explicitly supplied HTTPX client.
            candidates = (
                getattr(sdk_client, "_client", None),
                getattr(provider, "_http_client", None),
            )
            for http_client in candidates:
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
        if not (
            any(path.endswith(suffix) for suffix in self._API_PATHS)
            or self._GEMINI_PATH.search(path)
        ):
            return

        # Reading already-materialized .content doesn't consume the stream.
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
            # Capturing must never interrupt an LLM request.
            logger.warning("[request-capture] Save failed: %s", exc)

    @filter.command("print")
    async def print_control(self, event: AstrMessageEvent, action: str = ""):
        """/print: state; /print on: capture; /print off: stop capture."""
        action = action.lower()
        if action == "":
            state = "已开启 🟢" if self.enabled else "已关闭 ⚪"
            yield event.plain_result(
                f"🐈‍⬛ 请求体记录：{state}\n"
                "开启：/print on\n关闭：/print off"
            )
        elif action == "on":
            self.enabled = True
            attached = self._attach_to_providers()
            logger.info(
                "[request-capture] ON; hooked %d additional clients; dir=%s",
                attached,
                self.output_dir,
            )
            yield event.plain_result(
                "🐈‍⬛ 请求体记录已开启。后续 LLM 请求会保存 JSON。"
                f"\n目录：{self.output_dir}"
            )
        elif action == "off":
            self.enabled = False
            logger.info("[request-capture] OFF")
            yield event.plain_result("🐈‍⬛ 请求体记录已关闭。已有 JSON 文件保留。")
        else:
            yield event.plain_result("🐈‍⬛ 用法：/print、/print on、/print off")

    @staticmethod
    def _arguments(event: AstrMessageEvent) -> str:
        """CommandFilter has already matched the first token and wake prefix."""
        parts = event.get_message_str().strip().split(maxsplit=1)
        return parts[1].strip() if len(parts) == 2 else ""

    def _saved_files(self) -> list[Path]:
        """Sort by real save time: renaming does not move an entry in the list."""
        entries = []
        for path in self.output_dir.glob("*.json"):
            if path.is_file():
                try:
                    entries.append((path.stat().st_mtime_ns, path))
                except OSError:
                    continue
        return [path for _, path in sorted(entries, key=lambda x: (x[0], x[1].name), reverse=True)]

    @classmethod
    def _select_file(cls, token: str, files: list[Path]) -> Path | None:
        token = token.strip()
        if token.lower() in cls._LATEST:
            return files[0] if files else None
        if token.isascii() and token.isdecimal():
            number = int(token)
            return files[number - 1] if 1 <= number <= len(files) else None
        # Only basename matches from the enumerated requests directory are allowed.
        return next((path for path in files if path.name == token), None)

    @staticmethod
    def _new_filename(raw_name: str) -> str | None:
        name = raw_name.strip()
        if not name or name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
            return None
        if name.lower().endswith(".json"):
            name = name[:-5] + ".json"  # The saved-file listing uses this exact suffix.
        else:
            name += ".json"
        if name == ".json":
            return None
        return name

    @staticmethod
    def _help() -> str:
        return (
            "/print         查看开关状态\n"
            "/print on      开启捕获请求体\n"
            "/print off     关闭捕获请求体\n"
            "/请求体 list [n]\n"
            "/请求体 send 3 [raw]\n"
            "/请求体 rename 3 新名字\n"
            "/请求体 delete 3\n\n"
            "编号也可换成完整文件名\n"
            "latest/new/first/最新 发送最新文件。\n"
            "英文指令 /requestbody 与 /请求体 可互换。"
        )

    @filter.command("请求体", alias={"requestbody"})
    async def requestbody(self, event: AstrMessageEvent):
        """Manage saved request bodies. No arguments shows help."""
        argument_text = self._arguments(event)
        if not argument_text:
            yield event.plain_result(self._help())
            return

        action, _, remainder = argument_text.partition(" ")
        action = action.lower()
        remainder = remainder.strip()

        if action in {"help", "帮助"}:
            yield event.plain_result(self._help())
            return

        if action in {"list", "ls"}:
            if remainder and (not remainder.isascii() or not remainder.isdecimal()):
                yield event.plain_result("🐈‍⬛ 页码请填正整数，例如 /请求体 list 2")
                return
            page = int(remainder) if remainder else 1
            files = self._saved_files()
            if not files:
                yield event.plain_result("🐈‍⬛ 暂无请求体文件。发送 /print on 开始记录。")
                return
            total_pages = math.ceil(len(files) / self._PAGE_SIZE)
            if page < 1 or page > total_pages:
                yield event.plain_result(f"🐈‍⬛ 页码超出范围。当前共 {total_pages} 页。")
                return

            start = (page - 1) * self._PAGE_SIZE
            visible = files[start:start + self._PAGE_SIZE]
            lines = [
                f"🐈‍⬛ 请求体列表（共 {len(files)} 个）",
                f"第 {page}/{total_pages} 页 · {start + 1}–{start + len(visible)}",
                "",
            ]
            lines.extend(f"{start + i}. {path.name}" for i, path in enumerate(visible, 1))
            lines.append("\n────────────")
            if page > 1:
                lines.append(f"上一页：/requestbody list {page - 1}")
            if page < total_pages:
                lines.append(f"下一页：/requestbody list {page + 1}")
            lines.extend([
                "发送：/requestbody send 编号",
                "重命名：/requestbody rename 编号 新名字",
                "删除：/requestbody delete 编号",
            ])
            yield event.plain_result("\n".join(lines))
            return

        if action in {"send", "发送"}:
            raw_mode = False
            # Keep filenames containing spaces intact; 'raw' is a trailing option.
            if remainder.lower().endswith(" raw"):
                raw_mode = True
                remainder = remainder[:-4].strip()
            files = self._saved_files()
            selected = self._select_file(remainder, files)
            if not selected:
                yield event.plain_result("🐈‍⬛ 找不到指定文件。用 /请求体 list 查看编号。")
                return

            path_to_send = selected
            if not raw_mode:
                try:
                    raw = await asyncio.to_thread(selected.read_bytes)
                    pretty = json.dumps(json.loads(raw), ensure_ascii=False, indent=2) + "\n"
                except (ValueError, UnicodeError):
                    # Do not change non-JSON capture contents.
                    logger.warning("[request-capture] Invalid JSON; sending raw: %s", selected.name)
                except OSError as exc:
                    logger.warning("[request-capture] Read failed: %s", exc)
                    yield event.plain_result("🐈‍⬛ 读取请求体失败，请查看日志。")
                    return
                else:
                    temp_path = None
                    try:
                        with tempfile.NamedTemporaryFile(
                            mode="w", encoding="utf-8", suffix=".json",
                            prefix="requestbody_", delete=False,
                        ) as tmp:
                            temp_path = Path(tmp.name)
                            tmp.write(pretty)
                        path_to_send = temp_path
                        event.track_temporary_local_file(str(temp_path))
                    except OSError as exc:
                        if temp_path is not None:
                            temp_path.unlink(missing_ok=True)
                        logger.warning("[request-capture] Format export failed: %s", exc)
                        yield event.plain_result("🐈‍⬛ 生成美化文件失败，请查看日志。")
                        return

            # The platform sends this file before event-scoped temporary cleanup.
            yield event.chain_result([File(name=selected.name, file=str(path_to_send.resolve()))])
            return

        if action in {"rename", "重命名"}:
            # Quotes support a current filename containing spaces:
            # /请求体 rename "Old Name.json" 新名字
            try:
                parts = shlex.split(remainder, posix=True)
            except ValueError:
                yield event.plain_result("🐈‍⬛ 文件名引号没有闭合。")
                return
            if len(parts) < 2:
                yield event.plain_result("🐈‍⬛ 用法：/请求体 rename 编号 新名字")
                return
            selector, new_name = parts[0], " ".join(parts[1:])
            destination_name = self._new_filename(new_name)
            if destination_name is None:
                yield event.plain_result("🐈‍⬛ 新名字不能为空，也不能包含路径分隔符。")
                return
            files = self._saved_files()
            selected = self._select_file(selector, files)
            if not selected:
                yield event.plain_result("🐈‍⬛ 找不到要重命名的文件。")
                return
            if selected.name == destination_name:
                yield event.plain_result(f"🐈‍⬛ 文件已经叫 {selected.name}，无需修改。")
                return
            destination = self.output_dir / destination_name
            if destination.exists():
                yield event.plain_result(f"🐈‍⬛ 已存在 {destination_name}，不会覆盖。")
                return
            try:
                await asyncio.to_thread(selected.rename, destination)
            except OSError as exc:
                logger.warning("[request-capture] Rename failed: %s", exc)
                yield event.plain_result("🐈‍⬛ 重命名失败，请查看日志。")
                return
            yield event.plain_result(f"🐈‍⬛ 已重命名：\n{selected.name}\n→ {destination.name}")
            return

        if action in self._DELETE:
            if not remainder:
                yield event.plain_result("🐈‍⬛ 用法：/请求体 delete 编号或文件名")
                return
            files = self._saved_files()
            selected = self._select_file(remainder, files)
            if not selected:
                yield event.plain_result("🐈‍⬛ 找不到要删除的文件。")
                return
            try:
                await asyncio.to_thread(selected.unlink)
            except OSError as exc:
                logger.warning("[request-capture] Delete failed: %s", exc)
                yield event.plain_result("🐈‍⬛ 删除失败，请查看日志。")
                return
            yield event.plain_result(f"🐈‍⬛ 已删除：{selected.name}")
            return

        yield event.plain_result("🐈‍⬛ 未识别的指令。发送 /请求体 查看帮助。")

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        # Handles providers created or replaced after /print on was issued.
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
