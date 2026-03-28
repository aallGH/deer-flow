from __future__ import annotations

import asyncio
import json
import logging
import random
import threading
import uuid
from typing import Any

from app.channels.base import Channel
from app.channels.message_bus import InboundMessage, InboundMessageType, MessageBus, OutboundMessage

logger = logging.getLogger(__name__)


class WeComChannel(Channel):
    """WeCom (企业微信) IM channel using WebSocket.

    Configuration keys (in ``config.yaml`` under ``channels.wecom``):
        - ``bot_id``: WeCom bot ID.
        - ``bot_secret``: WeCom bot secret.
        - ``ws_url``: (optional) WebSocket URL, default: wss://openws.work.weixin.qq.com
        - ``heartbeat_interval``: (optional) Heartbeat interval in **seconds** (e.g., 30 for 30 seconds), default: 30 seconds.
           **Backward compatibility**: If value is >= 100, it's treated as milliseconds (old behavior) and converted to seconds.
           **Recommended**: Use seconds (e.g., 30) for clarity.

    The channel uses WebSocket long-connection mode so no public IP is required.
    """

    def __init__(self, bus: MessageBus, config: dict[str, Any]) -> None:
        super().__init__(name="wecom", bus=bus, config=config)
        self._thread: threading.Thread | None = None
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._ws: Any = None
        self._background_tasks: set[asyncio.Task] = set()
        self._running_card_ids: dict[str, str] = {}
        self._running_card_tasks: dict[str, asyncio.Task] = {}
        self._heartbeat_task: asyncio.Task | None = None
        self._heartbeat_interval: float = 30.0
        self._authenticated = False
        self._msgid_to_req_id: dict[str, str] = {}
        self._chatid_to_req_id: dict[str, str] = {}  # Maps chatid to req_id for replies

    async def start(self) -> None:
        if self._running:
            return
        try:
            import websockets
        except ImportError:
            logger.error("websockets is not installed. Install it with: uv add websockets")
            return

        self._websockets = websockets

        bot_id = self.config.get("bot_id", "")
        bot_secret = self.config.get("bot_secret", "")

        if not bot_id or not bot_secret:
            logger.error("WeCom channel requires bot_id and bot_secret")
            return

        self._main_loop = asyncio.get_event_loop()

        self._running = True
        self.bus.subscribe_outbound(self._on_outbound)

        self._thread = threading.Thread(
            target=self._run_ws,
            args=(bot_id, bot_secret),
            daemon=True,
        )
        self._thread.start()
        logger.info("WeCom channel started")

    def _run_ws(self, bot_id: str, bot_secret: str) -> None:
        """Run the WebSocket client in a dedicated thread."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        ws_url = self.config.get("ws_url", "wss://openws.work.weixin.qq.com")
        heartbeat_interval_config = self.config.get("heartbeat_interval", 30)

        logger.debug("WeCom configured heartbeat interval: %s", heartbeat_interval_config)

        # Handle backward compatibility: if value is less than 100, assume it's in seconds
        # and convert to seconds (previously was milliseconds)
        if isinstance(heartbeat_interval_config, (int, float)):
            if heartbeat_interval_config < 100:
                # Likely configured as seconds (e.g., 30), convert to seconds
                heartbeat_interval = float(heartbeat_interval_config)
                logger.warning(
                    "WeCom heartbeat interval %s is less than 100, assuming it's in seconds (not milliseconds). If you intended milliseconds, use a value >= 100. Using %.1f seconds.", heartbeat_interval_config, heartbeat_interval
                )
            else:
                # Likely configured as milliseconds (old behavior), convert to seconds
                heartbeat_interval = heartbeat_interval_config / 1000.0
                logger.warning(
                    "WeCom heartbeat interval %s is >= 100, assuming it's in milliseconds (old behavior). Converting to %.1f seconds. Consider updating config to use seconds instead of milliseconds.",
                    heartbeat_interval_config,
                    heartbeat_interval,
                )
        else:
            heartbeat_interval = 30.0  # Default 30 seconds

        # Validate heartbeat interval to prevent configuration errors
        if heartbeat_interval < 10.0:  # Less than 10 seconds
            logger.warning("WeCom heartbeat interval %.1f seconds is too short, using minimum 10 seconds", heartbeat_interval)
            heartbeat_interval = 10.0
        elif heartbeat_interval > 120.0:  # More than 2 minutes
            logger.warning("WeCom heartbeat interval %.1f seconds is too long, using maximum 120 seconds (2 minutes)", heartbeat_interval)
            heartbeat_interval = 120.0

        logger.debug("WeCom heartbeat interval after validation: %.2f seconds", heartbeat_interval)

        try:
            loop.run_until_complete(self._ws_loop(ws_url, bot_id, bot_secret, heartbeat_interval))
        except Exception:
            if self._running:
                logger.exception("WeCom WebSocket error")

    async def _ws_loop(self, ws_url: str, bot_id: str, bot_secret: str, heartbeat_interval: float) -> None:
        """Main WebSocket connection loop."""
        self._heartbeat_interval = heartbeat_interval
        while self._running:
            try:
                logger.info("Connecting to WeCom WebSocket: %s", ws_url)
                async with self._websockets.connect(ws_url, ping_interval=None, ping_timeout=None) as ws:
                    self._ws = ws
                    self._authenticated = False
                    self._msgid_to_req_id.clear()
                    self._chatid_to_req_id.clear()

                    await self._send_subscribe(bot_id, bot_secret)
                    logger.info("Subscribe frame sent to WeCom")

                    async for message in ws:
                        try:
                            frame = json.loads(message)
                            await self._handle_frame(frame)
                        except json.JSONDecodeError:
                            logger.warning("Invalid JSON from WeCom: %s", message)
                        except Exception:
                            logger.exception("Error handling WeCom message")
            except self._websockets.exceptions.ConnectionClosed:
                if self._running:
                    logger.warning("WeCom WebSocket disconnected, reconnecting...")
                    await self._stop_heartbeat()
                    await asyncio.sleep(3)
            except Exception:
                if self._running:
                    logger.exception("WeCom WebSocket error, reconnecting...")
                    await self._stop_heartbeat()
                    await asyncio.sleep(3)

    async def _send_subscribe(self, bot_id: str, bot_secret: str) -> None:
        """Send subscribe (authentication) frame."""
        frame = {
            "cmd": "aibot_subscribe",
            "headers": {
                "req_id": str(uuid.uuid4()),
            },
            "body": {
                "bot_id": bot_id,
                "secret": bot_secret,
            },
        }
        await self._ws.send(json.dumps(frame))

    async def _heartbeat_loop(self, interval: float) -> None:
        """Send periodic heartbeat frames to keep connection alive."""
        logger.debug("WeCom heartbeat loop started with interval: %.3f seconds", interval)
        try:
            while self._running and self._ws:
                # Add jitter: wait interval ± 10% to avoid synchronized heartbeats
                jitter = interval * 0.1
                wait_time = interval + random.uniform(-jitter, jitter)
                actual_wait = max(wait_time, interval * 0.9)  # Ensure at least 90% of interval
                logger.debug("WeCom heartbeat waiting: %.3f seconds", actual_wait)
                await asyncio.sleep(actual_wait)
                if self._ws:
                    try:
                        await self._send_heartbeat()
                    except Exception:
                        logger.debug("Heartbeat failed, connection might be closed")
                        break
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Heartbeat loop error")

    async def _send_heartbeat(self) -> None:
        """Send heartbeat frame."""
        frame = {
            "cmd": "ping",
        }
        await self._ws.send(json.dumps(frame))
        logger.debug("Sent heartbeat to WeCom")

    async def _stop_heartbeat(self) -> None:
        """Stop the heartbeat task."""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

    async def _handle_frame(self, frame: dict) -> None:
        """Handle incoming WebSocket frame."""
        cmd = frame.get("cmd", "")
        headers = frame.get("headers", {})
        body = frame.get("body", {})
        errcode = frame.get("errcode", 0)
        errmsg = frame.get("errmsg", "")

        if errcode != 0:
            logger.error("WeCom error frame: errcode=%d, errmsg=%s, frame=%s", errcode, errmsg, frame)
            return

        if cmd == "ping":
            await self._handle_ping(headers)
        elif cmd == "aibot_msg_callback":
            await self._handle_message_callback(headers, body)
        elif cmd == "aibot_event_callback":
            await self._handle_event_callback(headers, body)
        elif cmd == "aibot_subscribe":
            await self._handle_subscribe_response(headers, body)
        elif errcode != 0:
            # Error frames without cmd field
            logger.error("WeCom error frame: errcode=%d, errmsg=%s, frame=%s", errcode, errmsg, frame)
        else:
            logger.debug("Unhandled WeCom cmd: %s, frame=%s", cmd, frame)

    async def _handle_ping(self, headers: dict) -> None:
        """Handle ping frame from server."""
        frame = {
            "cmd": "pong",
            "headers": headers,
        }
        await self._ws.send(json.dumps(frame))
        logger.debug("Received ping, sent pong to WeCom")

    async def _handle_subscribe_response(self, headers: dict, body: dict) -> None:
        """Handle subscribe response frame."""
        if self._authenticated:
            logger.debug("WeCom authentication response received, already authenticated")
            return
        self._authenticated = True
        logger.info("WeCom authentication successful")
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(self._heartbeat_interval))

    async def _handle_message_callback(self, headers: dict, body: dict) -> None:
        """Handle message callback frame."""
        try:
            req_id = headers.get("req_id", "")
            msgid = body.get("msgid", "")
            chatid = body.get("chatid", "")
            chattype = body.get("chattype", "single")
            from_user = body.get("from", {})
            sender_id = from_user.get("userid", "")
            msgtype = body.get("msgtype", "")

            text = ""
            if msgtype == "text":
                text_content = body.get("text", {})
                text = text_content.get("content", "")
            elif msgtype == "mixed":
                mixed_content = body.get("mixed", {})
                msg_item = mixed_content.get("msg_item", mixed_content.get("elements", []))
                text_parts = []
                for item in msg_item:
                    if item.get("msgtype") == "text":
                        text_part = item.get("text", {})
                        text_parts.append(text_part.get("content", ""))
                text = "".join(text_parts)

            text = text.strip()

            logger.info(
                "[WeCom] parsed message: req_id=%s, msgid=%s, chatid=%s, chattype=%s, sender=%s, msgtype=%s, text=%r",
                req_id,
                msgid,
                chatid,
                chattype,
                sender_id,
                msgtype,
                text[:100] if text else "",
            )

            if not text:
                logger.info("[WeCom] empty text, ignoring message")
                return

            if text.startswith("/"):
                msg_type = InboundMessageType.COMMAND
            else:
                msg_type = InboundMessageType.CHAT

            # Use chatid as topic_id for grouping messages in the same conversation
            # This ensures all messages in the same chat appear in the same thread/window
            topic_id = chatid or sender_id

            self._msgid_to_req_id[msgid] = req_id
            # Also store chatid -> req_id mapping for reply messages
            # This is needed because replies use chatid (thread_ts) as the lookup key
            self._chatid_to_req_id[chatid or sender_id] = req_id

            inbound = self._make_inbound(
                chat_id=chatid or sender_id,
                user_id=sender_id,
                text=text,
                msg_type=msg_type,
                thread_ts=chatid or sender_id,  # Use chatid as thread identifier for conversation continuity
                metadata={"req_id": req_id, "message_id": msgid, "chatid": chatid, "headers": headers, "body": body},
            )
            inbound.topic_id = topic_id

            if self._running and self._main_loop and self._main_loop.is_running():
                logger.info("[WeCom] publishing inbound message to bus (type=%s, msgid=%s)", msg_type.value, msgid)
                fut = asyncio.run_coroutine_threadsafe(self._prepare_inbound(msgid, inbound), self._main_loop)
                fut.add_done_callback(lambda f, mid=msgid: self._log_future_error(f, "prepare_inbound", mid))
            else:
                logger.warning("[WeCom] channel not running, cannot publish inbound message")
        except Exception:
            logger.exception("[WeCom] error processing message callback")

    async def _handle_event_callback(self, headers: dict, body: dict) -> None:
        """Handle event callback frame."""
        try:
            req_id = headers.get("req_id", "")
            msgtype = body.get("msgtype", "")
            event_content = body.get("event", {})
            event_type = event_content.get("eventtype", event_content.get("event_type", ""))

            logger.info(
                "[WeCom] event callback: req_id=%s, msgtype=%s, event_type=%s",
                req_id,
                msgtype,
                event_type,
            )

            if event_type == "enter_chat":
                logger.info("[WeCom] User entered chat")
        except Exception:
            logger.exception("[WeCom] error processing event callback")

    async def _prepare_inbound(self, msg_id: str, inbound: InboundMessage) -> None:
        """Kick off WeCom side effects without delaying inbound dispatch."""
        self._ensure_running_card_started(msg_id)
        await self.bus.publish_inbound(inbound)

    async def stop(self) -> None:
        self._running = False
        self.bus.unsubscribe_outbound(self._on_outbound)
        for task in list(self._background_tasks):
            task.cancel()
        self._background_tasks.clear()
        for task in list(self._running_card_tasks.values()):
            task.cancel()
        self._running_card_tasks.clear()
        await self._stop_heartbeat()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("WeCom channel stopped")

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message back to WeCom."""
        await self._send_card_message(msg)

    async def _send_card_message(self, msg: OutboundMessage) -> None:
        """Send or update WeCom message."""
        source_message_id = msg.thread_ts
        if source_message_id:
            running_card_id = self._running_card_ids.get(source_message_id)
            awaited_running_card_task = False

            if not running_card_id:
                running_card_task = self._running_card_tasks.get(source_message_id)
                if running_card_task:
                    awaited_running_card_task = True
                    running_card_id = await running_card_task

            if running_card_id:
                try:
                    await self._update_card(running_card_id, msg.text, msg.is_final)
                except Exception:
                    if not msg.is_final:
                        raise
                    logger.exception(
                        "[WeCom] failed to update message %s, falling back to new reply",
                        running_card_id,
                    )
                    await self._reply_message(source_message_id, msg.text)
                else:
                    logger.info("[WeCom] message updated: source=%s", source_message_id)
            elif msg.is_final:
                await self._reply_message(source_message_id, msg.text)
            elif awaited_running_card_task:
                logger.warning(
                    "[WeCom] running card task finished without message_id for source=%s, skipping duplicate non-final creation",
                    source_message_id,
                )
            else:
                await self._ensure_running_card(source_message_id, msg.text)

            if msg.is_final:
                self._running_card_ids.pop(source_message_id, None)
            return

        await self._create_message(msg.chat_id, msg.text)

    async def _send_frame(self, frame: dict) -> None:
        """Send a frame via WebSocket."""
        if not self._ws:
            raise RuntimeError("WebSocket not connected")

        await self._ws.send(json.dumps(frame))

    async def _reply_message(self, message_id: str, text: str) -> str | None:
        """Reply to a message using aibot_respond_msg."""
        if not self._ws:
            return None

        # Use chatid -> req_id mapping for replies (since thread_ts is now chatid)
        req_id = self._chatid_to_req_id.get(message_id, str(uuid.uuid4()))

        frame = {
            "cmd": "aibot_respond_msg",
            "headers": {
                "req_id": req_id,
            },
            "body": {
                "msgtype": "markdown",
                "markdown": {
                    "content": text,
                },
            },
        }

        await self._send_frame(frame)
        return message_id

    async def _create_message(self, chat_id: str, text: str) -> None:
        """Create a new message using aibot_send_msg."""
        if not self._ws:
            return

        frame = {
            "cmd": "aibot_send_msg",
            "headers": {
                "req_id": str(uuid.uuid4()),
            },
            "body": {
                "chatid": chat_id,
                "msgtype": "markdown",
                "markdown": {
                    "content": text,
                },
            },
        }

        await self._send_frame(frame)

    async def _update_card(self, message_id: str, text: str, is_final: bool) -> None:
        """Update an existing message."""
        if not self._ws:
            return

        await self._reply_message(message_id, text)

    async def _create_running_card(self, source_message_id: str, text: str) -> str | None:
        """Create the running message and cache its message ID when available."""
        card_id = await self._reply_message(source_message_id, text)
        if card_id:
            self._running_card_ids[source_message_id] = card_id
            logger.info("[WeCom] running message created: source=%s", source_message_id)
        else:
            logger.warning("[WeCom] running message creation returned no message_id for source=%s, subsequent updates will fall back to new replies", source_message_id)
        return card_id

    def _ensure_running_card_started(self, source_message_id: str, text: str = "Working on it...") -> asyncio.Task | None:
        """Start running-message creation once per source message."""
        running_card_id = self._running_card_ids.get(source_message_id)
        if running_card_id:
            return None

        running_card_task = self._running_card_tasks.get(source_message_id)
        if running_card_task:
            return running_card_task

        running_card_task = asyncio.create_task(self._create_running_card(source_message_id, text))
        self._running_card_tasks[source_message_id] = running_card_task
        running_card_task.add_done_callback(lambda done_task, mid=source_message_id: self._finalize_running_card_task(mid, done_task))
        return running_card_task

    def _finalize_running_card_task(self, source_message_id: str, task: asyncio.Task) -> None:
        if self._running_card_tasks.get(source_message_id) is task:
            self._running_card_tasks.pop(source_message_id, None)
        self._log_task_error(task, "create_running_card", source_message_id)

    async def _ensure_running_card(self, source_message_id: str, text: str = "Working on it...") -> str | None:
        """Ensure the in-thread running message exists and track its message ID."""
        running_card_id = self._running_card_ids.get(source_message_id)
        if running_card_id:
            return running_card_id

        running_card_task = self._ensure_running_card_started(source_message_id, text)
        if running_card_task is None:
            return self._running_card_ids.get(source_message_id)
        return await running_card_task

    @staticmethod
    def _log_future_error(fut, name: str, msg_id: str) -> None:
        """Callback for run_coroutine_threadsafe futures to surface errors."""
        try:
            exc = fut.exception()
            if exc:
                logger.error("[WeCom] %s failed for msg_id=%s: %s", name, msg_id, exc)
        except Exception:
            pass

    @staticmethod
    def _log_task_error(task: asyncio.Task, name: str, msg_id: str) -> None:
        """Callback for background asyncio tasks to surface errors."""
        try:
            exc = task.exception()
            if exc:
                logger.error("[WeCom] %s failed for msg_id=%s: %s", name, msg_id, exc)
        except asyncio.CancelledError:
            logger.info("[WeCom] %s cancelled for msg_id=%s", name, msg_id)
        except Exception:
            pass
