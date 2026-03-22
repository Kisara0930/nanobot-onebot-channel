"""QQ channel implementation via OneBot11/NapCat HTTP webhook + send API.

This replaces the previous official QQ botpy-based implementation while keeping
channel name/config section as ``qq`` for compatibility with nanobot's built-in
channel discovery and outbound routing.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import mimetypes
import re
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from aiohttp import web
from loguru import logger
from pydantic import Field

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.paths import get_media_dir
from nanobot.config.schema import Base


class QQConfig(Base):
    """QQ channel configuration backed by OneBot11 HTTP API."""

    enabled: bool = False

    # OneBot HTTP API
    base_url: str = "http://127.0.0.1:6098"
    token: str = ""
    trust_env: bool = False

    # Webhook listener for inbound events from OneBot/NapCat
    listen_host: str = "0.0.0.0"
    listen_port: int = 8092
    event_path: str = "/onebot/event"

    # Bot account QQ number (optional, auto-learn from events)
    self_id: str = ""

    # nanobot allowlist: if empty, ChannelManager/BaseChannel will deny all
    allow_from: list[str] = Field(default_factory=list)

    # Outbound progress filtering parity with standalone onebot channel
    send_progress: bool = True
    send_tool_hints: bool = False


class QQChannel(BaseChannel):
    """QQ channel using OneBot11 webhook + HTTP send API."""

    name = "qq"
    display_name = "QQ"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return QQConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = QQConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: QQConfig = config

        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

        self._dup_cache: OrderedDict[str, float] = OrderedDict()
        self._dup_ttl_seconds = 120
        self._dup_max_items = 1024

    async def start(self) -> None:
        if not self.config.base_url:
            logger.error("QQ OneBot base_url not configured")
            return
        if not self.config.token:
            logger.error("QQ OneBot token not configured")
            return

        self._running = True
        self._app = web.Application(client_max_size=2 * 1024 * 1024)
        self._app["channel"] = self

        async def health(_: web.Request) -> web.Response:
            return web.json_response({"ok": True, "channel": self.name})

        async def onebot_event(request: web.Request) -> web.Response:
            try:
                payload = await request.json()
            except Exception:
                return web.Response(status=400, text="invalid json")

            # Return fast; handle asynchronously to avoid duplicate/quick-operation behavior.
            asyncio.create_task(self._handle_event_async(payload))
            return web.Response(status=204)

        self._app.router.add_get("/health", health)
        self._app.router.add_post(self.config.event_path, onebot_event)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(
            self._runner,
            host=self.config.listen_host,
            port=self.config.listen_port,
        )
        await self._site.start()

        logger.info(
            "QQ OneBot channel listening on http://{}:{}{}",
            self.config.listen_host,
            self.config.listen_port,
            self.config.event_path,
        )

        try:
            while self._running:
                await asyncio.sleep(3600)
        finally:
            await self.stop()

    async def stop(self) -> None:
        if not self._running and not self._runner:
            return

        self._running = False

        if self._runner:
            try:
                await self._runner.cleanup()
            except Exception:
                logger.exception("Error cleaning up QQ OneBot runner")
            finally:
                self._runner = None

        self._site = None
        self._app = None
        logger.info("QQ OneBot channel stopped")

    async def send(self, msg: OutboundMessage) -> None:
        content = (msg.content or "").strip()
        media = list(msg.media or [])
        if not content and not media:
            return

        meta = msg.metadata or {}
        message_type = str(meta.get("message_type") or "").strip()
        user_id = meta.get("user_id")
        group_id = meta.get("group_id")

        target_kind: str
        target_id: str

        if message_type == "group" and group_id is not None:
            target_kind, target_id = "group", str(group_id)
        elif message_type == "private" and user_id is not None:
            target_kind, target_id = "private", str(user_id)
        else:
            chat_id = str(msg.chat_id or "")
            if chat_id.startswith("group:"):
                target_kind, target_id = "group", chat_id.split(":", 1)[1]
            elif chat_id.startswith("private:"):
                target_kind, target_id = "private", chat_id.split(":", 1)[1]
            elif chat_id.startswith("qq:"):
                target_kind, target_id = "private", chat_id.split(":", 1)[1]
            else:
                target_kind, target_id = "private", chat_id

        image_media: list[str] = []
        file_media: list[str] = []
        for item in media:
            if self._is_image_ref(str(item)):
                image_media.append(str(item))
            else:
                file_media.append(str(item))

        message_payload = self._build_outbound_message(content=content, media=image_media)
        if message_payload is not None:
            if target_kind == "group":
                await self._send_group(target_id, message_payload)
            else:
                await self._send_private(target_id, message_payload)

        for item in file_media:
            if target_kind == "group":
                await self._send_group_file(target_id, item)
            else:
                await self._send_private_file(target_id, item)

    async def _send_private(self, user_id: str, message: Any) -> None:
        await self._post_onebot("/send_private_msg", {"user_id": int(user_id), "message": message})

    async def _send_group(self, group_id: str, message: Any) -> None:
        await self._post_onebot("/send_group_msg", {"group_id": int(group_id), "message": message})

    async def _send_private_file(self, user_id: str, file_path: str) -> None:
        await self._upload_file("private", user_id, file_path)

    async def _send_group_file(self, group_id: str, file_path: str) -> None:
        await self._upload_file("group", group_id, file_path)

    async def _upload_file(self, target_kind: str, target_id: str, file_path: str) -> None:
        local_path = self._resolve_outbound_file_path(file_path)
        if local_path is None:
            logger.warning("QQ OneBot skips outbound file; local path unavailable: {}", file_path)
            return

        file_value = self._build_upload_file_value(local_path)
        if not file_value:
            logger.warning("QQ OneBot skips outbound file; cannot encode file: {}", file_path)
            return

        payload = {
            ("user_id" if target_kind == "private" else "group_id"): int(target_id),
            "file": file_value,
            "name": local_path.name,
        }
        endpoint = "/upload_private_file" if target_kind == "private" else "/upload_group_file"
        await self._post_onebot(endpoint, payload)

    @staticmethod
    def _build_upload_file_value(path: Path) -> str | None:
        try:
            data = path.read_bytes()
        except Exception:
            return None
        return "base64://" + base64.b64encode(data).decode("ascii")

    @staticmethod
    def _resolve_outbound_file_path(file_path: str) -> Path | None:
        value = str(file_path or "").strip()
        if not value:
            return None
        if value.lower().startswith("file://"):
            value = value[7:]
        path = Path(value)
        if path.exists() and path.is_file():
            return path
        return None

    async def _post_onebot(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.config.token:
            raise RuntimeError("QQ OneBot token not configured")

        import httpx

        base = self.config.base_url.rstrip("/")
        url = f"{base}{endpoint}"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.token}",
        }

        async with httpx.AsyncClient(trust_env=self.config.trust_env, timeout=30) as client:
            r = await client.post(url, json=payload, headers=headers)
            if r.status_code in (401, 403):
                r = await client.post(
                    f"{url}?access_token={self.config.token}",
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
            r.raise_for_status()
            try:
                return r.json()
            except Exception:
                return {"ok": True, "status_code": r.status_code, "text": r.text}

    def _build_outbound_message(self, content: str, media: list[str]) -> str | list[dict[str, Any]] | None:
        segments: list[dict[str, Any]] = []

        if content:
            segments.append({
                "type": "text",
                "data": {"text": content},
            })

        for item in media:
            seg = self._build_media_segment(item)
            if seg is not None:
                segments.append(seg)

        if not segments:
            return None
        if len(segments) == 1 and segments[0].get("type") == "text":
            return str(segments[0].get("data", {}).get("text", ""))
        return segments

    def _build_media_segment(self, media_path: str) -> dict[str, Any] | None:
        item = str(media_path or "").strip()
        if not item:
            return None

        if self._is_image_ref(item):
            return {
                "type": "image",
                "data": {"file": item},
            }

        logger.warning("QQ OneBot currently skips unsupported media attachment: {}", item)
        return None

    @staticmethod
    def _is_image_ref(value: str) -> bool:
        lower = value.lower()
        if lower.startswith("base64://"):
            return True

        candidate = value
        if lower.startswith("file://"):
            candidate = value[7:]
        elif lower.startswith(("http://", "https://")):
            candidate = value.split("?", 1)[0]

        mime, _ = mimetypes.guess_type(candidate)
        return bool(mime and mime.startswith("image/"))

    async def _handle_event_async(self, payload: dict[str, Any]) -> None:
        try:
            await self._handle_event(payload)
        except Exception:
            logger.exception("QQ OneBot event handling error")

    async def _handle_event(self, payload: dict[str, Any]) -> None:
        if payload.get("post_type") != "message":
            return

        message_type = payload.get("message_type")
        if message_type != "private":
            return

        self_id = payload.get("self_id")
        if self_id is not None and not self.config.self_id:
            self.config.self_id = str(self_id)

        if self._is_self_message(payload):
            return

        if self._is_duplicate_event(payload):
            return

        raw_message = payload.get("raw_message")
        message = payload.get("message")
        context = {
            "message_type": message_type,
            "user_id": payload.get("user_id"),
            "group_id": payload.get("group_id"),
            "message_id": payload.get("message_id"),
        }
        text, media = await self._extract_inbound_content_and_media(message, raw_message, context=context)
        if not text and not media:
            return

        user_id = payload.get("user_id")
        if user_id is None:
            return

        metadata = {
            "message_type": message_type,
            "user_id": user_id,
            "group_id": payload.get("group_id"),
            "message_id": payload.get("message_id"),
            "time": payload.get("time"),
            "raw_event": payload,
        }

        await self._handle_message(
            sender_id=str(user_id),
            chat_id=f"qq:{user_id}",
            content=text or "[image]",
            media=media,
            metadata=metadata,
        )

    def _is_self_message(self, payload: dict[str, Any]) -> bool:
        self_id = str(payload.get("self_id") or self.config.self_id or "")
        if not self_id:
            return False
        user_id = str(payload.get("user_id") or "")
        sender = payload.get("sender") or {}
        sender_user_id = str(sender.get("user_id") or "")
        return user_id == self_id or sender_user_id == self_id

    def _event_dedupe_key(self, payload: dict[str, Any]) -> str:
        mid = str(payload.get("message_id") or "")
        if mid:
            return f"msgid:{mid}"
        uid = str(payload.get("user_id") or "")
        raw = str(payload.get("raw_message") or "")
        t = str(payload.get("time") or "")
        return f"fallback:{uid}:{t}:{raw}"

    def _is_duplicate_event(self, payload: dict[str, Any]) -> bool:
        now = time.time()
        key = self._event_dedupe_key(payload)

        expired = [k for k, ts in self._dup_cache.items() if now - ts > self._dup_ttl_seconds]
        for k in expired:
            self._dup_cache.pop(k, None)

        if key in self._dup_cache:
            self._dup_cache.move_to_end(key)
            self._dup_cache[key] = now
            return True

        self._dup_cache[key] = now
        self._dup_cache.move_to_end(key)
        while len(self._dup_cache) > self._dup_max_items:
            self._dup_cache.popitem(last=False)
        return False

    async def _extract_inbound_content_and_media(
        self,
        message: Any,
        raw_message: Any,
        *,
        context: dict[str, Any] | None = None,
    ) -> tuple[str, list[str]]:
        if isinstance(message, str):
            text, refs = self._extract_cq_content_and_attachment_refs(message)
            media, content_parts = await self._download_attachments(refs, context=context)
            content = "\n".join(part for part in [text.strip()] + content_parts if part).strip()
            return content, media

        refs: list[dict[str, str]] = []
        text_parts: list[str] = []
        if isinstance(message, list):
            for seg in message:
                if not isinstance(seg, dict):
                    continue
                seg_type = seg.get("type")
                data = seg.get("data") or {}

                if seg_type == "text":
                    txt = data.get("text")
                    if txt:
                        text_parts.append(str(txt))
                    continue

                if seg_type in ("image", "file"):
                    tag = "image" if seg_type == "image" else "file"
                    ref_info = self._extract_attachment_info(data, kind_hint=tag)
                    refs.append(ref_info)
                    continue

            media, content_parts = await self._download_attachments(refs, context=context)
            content = "\n".join(part for part in ["".join(text_parts).strip()] + content_parts if part).strip()
            if content or media:
                return content, media

        fallback = str(raw_message or "").strip()
        text, refs = self._extract_cq_content_and_attachment_refs(fallback)
        media, content_parts = await self._download_attachments(refs, context=context)
        content = "\n".join(part for part in [text.strip()] + content_parts if part).strip()
        return content, media

    @classmethod
    def _extract_attachment_info(cls, data: dict[str, Any], kind_hint: str = "file") -> dict[str, str]:
        url = str(data.get("url") or "").strip()
        path = str(data.get("path") or "").strip()
        file_value = str(data.get("file") or "").strip()
        file_id = str(data.get("file_id") or data.get("fileId") or "").strip()

        ref = url or path
        if not ref and file_value:
            if file_value.lower().startswith(("http://", "https://", "file://", "base64://")):
                ref = file_value
            else:
                p = Path(file_value)
                if p.is_absolute():
                    ref = file_value

        display = file_value or Path(path).name or Path(url.split("?", 1)[0]).name or file_id or kind_hint
        display_ext = Path(display).suffix if display else ""
        info = {"kind": kind_hint, "display": display}
        if display_ext:
            info["name_ext"] = display_ext
        if ref:
            info["ref"] = ref
        if file_id:
            info["file_id"] = file_id
        return info

    @staticmethod
    def _parse_cq_params(param_text: str) -> dict[str, str]:
        params: dict[str, str] = {}
        for part in param_text.split(","):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            params[k.strip()] = v.strip()
        return params

    @classmethod
    def _extract_cq_content_and_attachment_refs(cls, text: str) -> tuple[str, list[dict[str, str]]]:
        src = str(text or "")
        if not src:
            return "", []

        refs: list[dict[str, str]] = []

        def repl(match: re.Match) -> str:
            cq_type = match.group(1).strip().lower()
            params = cls._parse_cq_params(match.group(2) or "")
            if cq_type == "image":
                info = cls._extract_attachment_info(params, kind_hint="image")
                if info.get("ref") or info.get("file_id"):
                    refs.append(info)
                display = info.get("display") or "image"
                return f"[image: {display}]"
            if cq_type == "file":
                info = cls._extract_attachment_info(params, kind_hint="file")
                display = info.get("display") or "file"
                kind = "image" if cls._is_image_ref(display) else "file"
                info["kind"] = kind
                if info.get("ref") or info.get("file_id"):
                    refs.append(info)
                return f"[{kind}: {display}]"
            return ""

        out = re.sub(r"\[CQ:([^,\]]+)(?:,([^\]]*))?\]", repl, src)
        return out.strip(), refs

    async def _download_attachments(
        self,
        refs: list[dict[str, str]],
        context: dict[str, Any] | None = None,
    ) -> tuple[list[str], list[str]]:
        downloaded: list[str] = []
        content_parts: list[str] = []
        for item in refs:
            kind = str(item.get("kind") or "file")
            resolved_ref = await self._resolve_attachment_ref(item, context=context)
            path = await self._download_attachment(
                resolved_ref or "",
                kind=kind,
                preferred_ext=str(item.get("name_ext") or "").strip(),
            )
            if path:
                downloaded.append(path)
                content_parts.append(f"[{kind}: {path}]")
            else:
                display = str(item.get("display") or item.get("file_id") or kind)
                content_parts.append(f"[{kind}: {display}]")
        return list(dict.fromkeys(downloaded)), content_parts

    async def _resolve_attachment_ref(self, item: dict[str, str], context: dict[str, Any] | None = None) -> str | None:
        ref = str(item.get("ref") or "").strip()
        if ref:
            return ref

        file_id = str(item.get("file_id") or "").strip()
        if not file_id:
            return None

        message_type = str((context or {}).get("message_type") or "").strip().lower()
        group_id = (context or {}).get("group_id")

        try:
            if message_type == "group" and group_id:
                resp = await self._post_onebot("/get_group_file_url", {"group": str(group_id), "file_id": file_id})
                resolved = self._extract_downloadable_ref_from_response(resp)
                if resolved:
                    return resolved
            elif message_type == "private":
                resp = await self._post_onebot("/get_private_file_url", {"file_id": file_id})
                resolved = self._extract_downloadable_ref_from_response(resp)
                if resolved:
                    return resolved
        except Exception as e:
            logger.warning("QQ OneBot failed to resolve file URL via direct API for {}: {}", file_id, e)

        try:
            resp = await self._post_onebot("/get_file", {"file_id": file_id})
            resolved = self._extract_downloadable_ref_from_response(resp)
            if resolved:
                return resolved
        except Exception as e:
            logger.warning("QQ OneBot failed to resolve file via get_file for {}: {}", file_id, e)

        return None

    @staticmethod
    def _extract_downloadable_ref_from_response(resp: dict[str, Any]) -> str | None:
        data = resp.get("data") if isinstance(resp, dict) else None
        candidates: list[str] = []
        if isinstance(data, dict):
            for key in ("url", "file", "path"):
                value = data.get(key)
                if value:
                    candidates.append(str(value).strip())
        elif isinstance(data, str):
            candidates.append(data.strip())

        for value in candidates:
            lower = value.lower()
            if lower.startswith(("http://", "https://", "file://", "base64://")):
                return value
            p = Path(value)
            if p.is_absolute():
                return value
        return None

    async def _download_attachment(self, ref: str, kind: str = "file", preferred_ext: str = "") -> str | None:
        value = str(ref or "").strip()
        if not value:
            return None

        lower = value.lower()
        if lower.startswith("file://"):
            local = value[7:]
            p = Path(local)
            return str(p) if p.exists() and p.is_file() else None

        p = Path(value)
        if p.exists() and p.is_file():
            return str(p)

        if not lower.startswith(("http://", "https://")):
            return None

        try:
            import httpx

            media_dir = get_media_dir("qq")
            digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]
            raw_suffix = Path(value.split("?", 1)[0]).suffix
            suffix = preferred_ext or raw_suffix or (".img" if kind == "image" else ".bin")
            tmp_path = media_dir / f"qq_{digest}{suffix}"

            async with httpx.AsyncClient(trust_env=self.config.trust_env, timeout=30, follow_redirects=True) as client:
                resp = await client.get(value)
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "").split(";", 1)[0].strip()
                if content_type:
                    guessed_ext = mimetypes.guess_extension(content_type)
                    if guessed_ext and tmp_path.suffix in (".img", ".bin"):
                        tmp_path = media_dir / f"qq_{digest}{guessed_ext}"
                tmp_path.write_bytes(resp.content)
                return str(tmp_path)
        except Exception as e:
            logger.warning("QQ OneBot attachment download failed: {} ({})", value, e)
            return None
