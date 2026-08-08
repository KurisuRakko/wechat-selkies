"""Private WeChat direct-message monitor and same-origin Web Push service."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import re
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlparse

from aiohttp import ClientError, ClientSession, web
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush_async

from .attach import AttachPreparer
from .errors import HistoryError
from .reader import HistoryReader, _read_connection
from .sessions import scan_direct_rows


LOG = logging.getLogger("wechat-notifications")

STATE_ROOT = Path("/config/.wechat-notifications")
STATE_FILE = STATE_ROOT / "state.json"
SUBSCRIPTIONS_FILE = STATE_ROOT / "subscriptions.json"
VAPID_PRIVATE_FILE = STATE_ROOT / "vapid-private.pem"

BIND_HOST = "127.0.0.1"
BIND_PORT = 8765
POLL_INTERVAL_SECONDS = 2.0
COALESCE_QUIET_SECONDS = 2.0
COALESCE_MAX_SECONDS = 5.0
PREVIEW_MAX_CHARS = 160
MAX_SUBSCRIPTIONS = 16
MAX_API_BODY_BYTES = 16 * 1024
MAX_MESSAGES_PER_CHANGE = 200
WEB_PUSH_TTL_SECONDS = 300
WEB_PUSH_TIMEOUT_SECONDS = 10
VAPID_SUBJECT = "mailto:wechat-selkies@example.invalid"

_WHITESPACE = re.compile(r"\s+")


class NotificationError(RuntimeError):
    """Base error for notification-specific state and validation failures."""


class MessageSnapshotPending(NotificationError):
    """The session row is newer than the readable message snapshot."""


@dataclass(frozen=True, order=True, slots=True)
class MessageCursor:
    timestamp: int
    local_id: int

    def payload(self) -> dict[str, int]:
        return {"timestamp": self.timestamp, "local_id": self.local_id}

    @classmethod
    def parse(cls, value: object) -> MessageCursor:
        if not isinstance(value, dict):
            raise NotificationError("invalid cursor state")
        try:
            timestamp = int(value["timestamp"])
            local_id = int(value["local_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise NotificationError("invalid cursor state") from exc
        if timestamp < 0 or local_id < 0:
            raise NotificationError("invalid cursor state")
        return cls(timestamp, local_id)


@dataclass(frozen=True, slots=True)
class SessionState:
    cursor: MessageCursor
    unread_count: int

    def payload(self) -> dict[str, object]:
        return {"cursor": self.cursor.payload(), "unread_count": self.unread_count}

    @classmethod
    def parse(cls, value: object) -> SessionState:
        if not isinstance(value, dict):
            raise NotificationError("invalid session state")
        try:
            unread_count = int(value.get("unread_count", 0))
        except (TypeError, ValueError) as exc:
            raise NotificationError("invalid unread count") from exc
        if unread_count < 0:
            raise NotificationError("invalid unread count")
        return cls(MessageCursor.parse(value.get("cursor")), unread_count)


@dataclass(frozen=True, slots=True)
class DirectSession:
    session_id: str
    display_name: str
    cursor: MessageCursor
    unread_count: int
    preview: str


@dataclass(frozen=True, slots=True)
class NewMessage:
    cursor: MessageCursor
    direction: str
    text: str
    kind: str


@dataclass(frozen=True, slots=True)
class NotificationEvent:
    title: str
    body: str
    tag: str
    topic: str
    timestamp_ms: int
    force: bool = False

    def payload(self) -> dict[str, object]:
        return {
            "version": 1,
            "title": self.title,
            "body": self.body,
            "tag": self.tag,
            "timestamp": self.timestamp_ms,
            "url": "/",
            "force": self.force,
        }


@dataclass(slots=True)
class PendingSession:
    first_seen: float
    last_changed: float
    session: DirectSession


def _safe_preview(value: object, fallback: str = "[新消息]") -> str:
    text = _WHITESPACE.sub(" ", str(value or "")).strip()
    if not text:
        text = fallback
    if len(text) > PREVIEW_MAX_CHARS:
        text = text[: PREVIEW_MAX_CHARS - 1] + "…"
    return text


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        if os.name == "posix":
            temporary.chmod(mode)
        os.replace(temporary, path)
        if os.name == "posix":
            path.chmod(mode)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, value: object) -> None:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    _atomic_write(path, encoded)


def _preserve_corrupt(path: Path) -> None:
    if not path.exists():
        return
    target = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
    try:
        os.replace(path, target)
        if os.name == "posix":
            target.chmod(0o600)
    except OSError:
        LOG.exception("failed to preserve corrupt state file %s", path.name)


class CursorStore:
    def __init__(self, path: Path = STATE_FILE):
        self.path = path

    def load(self) -> tuple[bool, dict[str, SessionState]]:
        if not self.path.exists():
            return False, {}
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            if document.get("version") != 1 or not isinstance(
                document.get("sessions"), dict
            ):
                raise NotificationError("unsupported cursor state")
            sessions = {
                str(session_id): SessionState.parse(value)
                for session_id, value in document["sessions"].items()
                if isinstance(session_id, str) and session_id
            }
            return True, sessions
        except (OSError, json.JSONDecodeError, NotificationError) as exc:
            LOG.warning(
                "cursor state is corrupt; preserving it and re-baselining (%s)",
                type(exc).__name__,
            )
            _preserve_corrupt(self.path)
            return False, {}

    def save(self, sessions: dict[str, SessionState]) -> None:
        _atomic_json(
            self.path,
            {
                "version": 1,
                "sessions": {
                    session_id: state.payload()
                    for session_id, state in sorted(sessions.items())
                },
            },
        )


def scan_direct_sessions(reader: HistoryReader) -> dict[str, DirectSession]:
    """在共享的私聊扫描结果上补齐通知需要的游标与预览文本。"""

    return {
        row.session_id: DirectSession(
            session_id=row.session_id,
            display_name=row.display_name,
            cursor=MessageCursor(row.timestamp, row.local_id),
            unread_count=row.unread_count,
            preview=_safe_preview(row.summary),
        )
        for row in scan_direct_rows(reader).values()
    }


def read_new_messages(
    reader: HistoryReader,
    session: DirectSession,
    previous: MessageCursor,
) -> list[NewMessage]:
    table = reader._message_table(session.session_id)
    contacts = reader._load_contacts()
    session_metadata = {
        "session_id": session.session_id,
        "display_name": session.display_name,
        "alias": "",
        "kind": "direct",
    }
    collected: list[tuple[tuple[int, int, str], NewMessage]] = []

    for relative_db in reader._message_database_keys():
        database = reader.cache.get(relative_db)
        with _read_connection(database) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not exists:
                continue
            names = reader._name_map(connection)
            rows = connection.execute(
                f"""
                SELECT local_id, local_type, create_time, real_sender_id,
                       message_content, WCDB_CT_message_content
                FROM [{table}]
                WHERE (create_time > ? OR (create_time = ? AND local_id > ?))
                  AND (create_time < ? OR (create_time = ? AND local_id <= ?))
                ORDER BY create_time ASC, local_id ASC
                LIMIT ?
                """,
                (
                    previous.timestamp,
                    previous.timestamp,
                    previous.local_id,
                    session.cursor.timestamp,
                    session.cursor.timestamp,
                    session.cursor.local_id,
                    MAX_MESSAGES_PER_CHANGE + 1,
                ),
            ).fetchall()
            for row in rows:
                item = reader._message_item(
                    row, relative_db, session_metadata, contacts, names
                )
                cursor = MessageCursor(
                    int(row["create_time"] or 0), int(row["local_id"] or 0)
                )
                collected.append(
                    (
                        (cursor.timestamp, cursor.local_id, relative_db),
                        NewMessage(
                            cursor=cursor,
                            direction=str(item["direction"]),
                            text=_safe_preview(item["text"]),
                            kind=str(item["type"]),
                        ),
                    )
                )

    collected.sort(key=lambda entry: entry[0])
    messages = [entry[1] for entry in collected[-MAX_MESSAGES_PER_CHANGE:]]
    if not messages or messages[-1].cursor < session.cursor:
        raise MessageSnapshotPending("message database has not caught up to session row")
    return messages


def build_notification(
    session: DirectSession,
    previous: SessionState,
    messages: list[NewMessage],
) -> NotificationEvent | None:
    latest = messages[-1] if messages else None
    if latest is not None and latest.direction == "outgoing":
        return None
    if latest is not None and latest.direction == "incoming":
        incoming_count = sum(message.direction == "incoming" for message in messages)
        preview = latest.text
    elif session.unread_count > previous.unread_count:
        incoming_count = max(1, session.unread_count - previous.unread_count)
        preview = session.preview
    else:
        return None

    body = _safe_preview(preview)
    if incoming_count > 1:
        body = f"{incoming_count} 条新消息 · {body}"
    digest = hashlib.sha256(session.session_id.encode("utf-8")).hexdigest()
    return NotificationEvent(
        title=_safe_preview(session.display_name, "微信联系人"),
        body=body,
        tag=f"wechat-{digest[:24]}",
        topic=digest[:32],
        timestamp_ms=session.cursor.timestamp * 1000,
    )


class NotificationMonitor:
    def __init__(
        self,
        reader: HistoryReader | None = None,
        cursor_store: CursorStore | None = None,
    ):
        self.reader = reader or HistoryReader()
        self.cursor_store = cursor_store or CursorStore()
        self.initialized, self.state = self.cursor_store.load()
        self.pending: dict[str, PendingSession] = {}

    def close(self) -> None:
        self.reader.close()

    def poll(self, now: float | None = None) -> list[NotificationEvent]:
        current_time = time.monotonic() if now is None else now
        sessions = scan_direct_sessions(self.reader)
        if not self.initialized:
            self.state = {
                session_id: SessionState(session.cursor, session.unread_count)
                for session_id, session in sessions.items()
            }
            self.cursor_store.save(self.state)
            self.initialized = True
            LOG.info("established first notification baseline for %d direct chats", len(sessions))
            return []

        changed_state = False
        for session_id, session in sessions.items():
            previous = self.state.get(session_id)
            if previous is None:
                self.state[session_id] = SessionState(
                    session.cursor, session.unread_count
                )
                changed_state = True
                continue
            if session.cursor < previous.cursor:
                self.state[session_id] = SessionState(
                    session.cursor, session.unread_count
                )
                self.pending.pop(session_id, None)
                changed_state = True
                continue
            if session.cursor == previous.cursor:
                if session.unread_count != previous.unread_count:
                    self.state[session_id] = SessionState(
                        previous.cursor, session.unread_count
                    )
                    changed_state = True
                continue

            pending = self.pending.get(session_id)
            if pending is None:
                self.pending[session_id] = PendingSession(
                    first_seen=current_time,
                    last_changed=current_time,
                    session=session,
                )
            elif pending.session.cursor != session.cursor:
                pending.last_changed = current_time
                pending.session = session
            else:
                pending.session = session

        events: list[NotificationEvent] = []
        for session_id, pending in list(self.pending.items()):
            quiet = current_time - pending.last_changed
            age = current_time - pending.first_seen
            if quiet < COALESCE_QUIET_SECONDS and age < COALESCE_MAX_SECONDS:
                continue
            previous = self.state.get(session_id)
            current = sessions.get(session_id)
            if previous is None or current is None:
                self.pending.pop(session_id, None)
                continue
            try:
                messages = read_new_messages(self.reader, current, previous.cursor)
            except MessageSnapshotPending:
                if (
                    current.unread_count > previous.unread_count
                    and age >= COALESCE_MAX_SECONDS
                ):
                    # A few WeChat link-style direct sessions have no readable
                    # message table. After giving WAL propagation a full quiet
                    # window, unread growth is the conservative incoming signal.
                    messages = []
                else:
                    LOG.debug("message snapshot still pending for session %s", session_id)
                    continue
            event = build_notification(current, previous, messages)
            if event is not None:
                events.append(event)
            self.state[session_id] = SessionState(
                current.cursor, current.unread_count
            )
            self.pending.pop(session_id, None)
            changed_state = True

        if changed_state:
            self.cursor_store.save(self.state)
        return events


def _decode_base64url(value: object, expected_length: int) -> bytes:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise NotificationError("invalid subscription key")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error) as exc:
        raise NotificationError("invalid subscription key") from exc
    if len(decoded) != expected_length:
        raise NotificationError("invalid subscription key length")
    return decoded


def validate_subscription(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise NotificationError("subscription must be an object")
    endpoint = value.get("endpoint")
    keys = value.get("keys")
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > 2048:
        raise NotificationError("invalid subscription endpoint")
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise NotificationError("subscription endpoint must be HTTPS")
    if not isinstance(keys, dict):
        raise NotificationError("subscription keys are missing")
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    public_key = _decode_base64url(p256dh, 65)
    if public_key[0] != 4:
        raise NotificationError("invalid p256dh point")
    _decode_base64url(auth, 16)
    return {
        "endpoint": endpoint,
        "expirationTime": value.get("expirationTime"),
        "keys": {"p256dh": p256dh, "auth": auth},
    }


def subscription_id(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:24]


class SubscriptionStore:
    def __init__(self, path: Path = SUBSCRIPTIONS_FILE):
        self.path = path
        self.subscriptions: dict[str, dict[str, object]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            if document.get("version") != 1 or not isinstance(
                document.get("subscriptions"), list
            ):
                raise NotificationError("unsupported subscription store")
            for item in document["subscriptions"]:
                if not isinstance(item, dict):
                    raise NotificationError("invalid stored subscription")
                subscription = validate_subscription(item.get("subscription"))
                origin = item.get("origin")
                if not isinstance(origin, str) or not origin:
                    raise NotificationError("invalid stored origin")
                identifier = subscription_id(str(subscription["endpoint"]))
                self.subscriptions[identifier] = {
                    "id": identifier,
                    "origin": origin,
                    "created_at": int(item.get("created_at") or 0),
                    "subscription": subscription,
                }
            if len(self.subscriptions) > MAX_SUBSCRIPTIONS:
                raise NotificationError("too many stored subscriptions")
        except (OSError, json.JSONDecodeError, NotificationError) as exc:
            LOG.warning(
                "subscription store is corrupt; preserving it and starting empty (%s)",
                type(exc).__name__,
            )
            self.subscriptions.clear()
            _preserve_corrupt(self.path)

    def _save(self) -> None:
        _atomic_json(
            self.path,
            {
                "version": 1,
                "subscriptions": [
                    self.subscriptions[key]
                    for key in sorted(self.subscriptions)
                ],
            },
        )

    def all(self) -> list[dict[str, object]]:
        return list(self.subscriptions.values())

    def get_by_endpoint(self, endpoint: str) -> dict[str, object] | None:
        return self.subscriptions.get(subscription_id(endpoint))

    def upsert(self, value: object, origin: str) -> dict[str, object]:
        subscription = validate_subscription(value)
        identifier = subscription_id(str(subscription["endpoint"]))
        if identifier not in self.subscriptions and len(self.subscriptions) >= MAX_SUBSCRIPTIONS:
            raise NotificationError("subscription limit reached")
        item = {
            "id": identifier,
            "origin": origin,
            "created_at": int(time.time()),
            "subscription": subscription,
        }
        self.subscriptions[identifier] = item
        self._save()
        return item

    def remove_endpoint(self, endpoint: str) -> bool:
        removed = self.subscriptions.pop(subscription_id(endpoint), None) is not None
        if removed:
            self._save()
        return removed


class VapidKeys:
    def __init__(self, path: Path = VAPID_PRIVATE_FILE):
        self.path = path
        self.private_key, self.public_key = self._ensure()

    def _ensure(self) -> tuple[Path, str]:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            self.path.parent.chmod(0o700)
        if self.path.exists():
            data = self.path.read_bytes()
            try:
                key = serialization.load_pem_private_key(data, password=None)
            except (TypeError, ValueError) as exc:
                raise NotificationError("stored VAPID private key is invalid") from exc
            if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(
                key.curve, ec.SECP256R1
            ):
                raise NotificationError("stored VAPID private key has wrong curve")
            if os.name == "posix":
                self.path.chmod(0o600)
        else:
            key = ec.generate_private_key(ec.SECP256R1())
            data = key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            _atomic_write(self.path, data)
        public_bytes = key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        public_key = base64.urlsafe_b64encode(public_bytes).rstrip(b"=").decode("ascii")
        return self.path, public_key


WebPushCall = Callable[..., Awaitable[object]]


class PushSender:
    def __init__(
        self,
        subscriptions: SubscriptionStore,
        vapid: VapidKeys,
        session: ClientSession,
        push_call: WebPushCall = webpush_async,
        sleep_call: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.subscriptions = subscriptions
        self.vapid = vapid
        self.session = session
        self.push_call = push_call
        self.sleep_call = sleep_call

    async def _send_item(
        self, item: dict[str, object], event: NotificationEvent
    ) -> bool:
        identifier = str(item["id"])
        delays = (0.0, 1.0, 2.0, 4.0)
        for attempt, delay in enumerate(delays):
            if delay:
                await self.sleep_call(delay)
            try:
                await self.push_call(
                    subscription_info=item["subscription"],
                    data=json.dumps(
                        event.payload(), ensure_ascii=False, separators=(",", ":")
                    ),
                    vapid_private_key=str(self.vapid.private_key),
                    vapid_claims={"sub": VAPID_SUBJECT},
                    ttl=WEB_PUSH_TTL_SECONDS,
                    timeout=WEB_PUSH_TIMEOUT_SECONDS,
                    headers={"Urgency": "high", "Topic": event.topic},
                    aiohttp_session=self.session,
                )
                LOG.info("delivered Web Push to subscription %s", identifier)
                return True
            except WebPushException as exc:
                response = exc.response
                status = getattr(response, "status", None)
                if status is None:
                    status = getattr(response, "status_code", None)
                if status in (404, 410):
                    endpoint = str(item["subscription"]["endpoint"])  # type: ignore[index]
                    self.subscriptions.remove_endpoint(endpoint)
                    LOG.info("removed expired Web Push subscription %s", identifier)
                    return False
                if status == 429 or (isinstance(status, int) and status >= 500):
                    if attempt + 1 < len(delays):
                        continue
                LOG.warning("Web Push failed for subscription %s (status=%s)", identifier, status)
                return False
            except (asyncio.TimeoutError, ClientError, OSError):
                if attempt + 1 < len(delays):
                    continue
                LOG.warning("Web Push timed out for subscription %s", identifier)
                return False
            except Exception as exc:
                LOG.warning(
                    "Web Push internal failure for subscription %s (%s)",
                    identifier,
                    type(exc).__name__,
                )
                return False
        return False

    async def send_all(self, event: NotificationEvent) -> None:
        items = self.subscriptions.all()
        if not items:
            return
        await asyncio.gather(*(self._send_item(item, event) for item in items))

    async def send_one(self, item: dict[str, object], event: NotificationEvent) -> bool:
        return await self._send_item(item, event)


def _error_details(exc: BaseException) -> tuple[str, str]:
    """Classify a monitor failure for the config endpoint.

    Only errors that carry an intentionally safe message expose one; anything
    unexpected reports just its type name so paths and secrets never reach the
    browser.
    """
    code = str(getattr(exc, "code", type(exc).__name__))
    if isinstance(exc, HistoryError):
        return code, exc.safe_message
    if isinstance(exc, NotificationError):
        return code, str(exc)
    return code, ""


class NotificationRuntime:
    def __init__(self, state_root: Path = STATE_ROOT):
        state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            state_root.chmod(0o700)
        self.cursor_store = CursorStore(state_root / STATE_FILE.name)
        self.subscriptions = SubscriptionStore(state_root / SUBSCRIPTIONS_FILE.name)
        self.vapid = VapidKeys(state_root / VAPID_PRIVATE_FILE.name)
        self.session: ClientSession | None = None
        self.sender: PushSender | None = None
        self.monitor: NotificationMonitor | None = None
        self.monitor_task: asyncio.Task[None] | None = None
        self.history_ready = False
        self.last_error = ""
        self.last_error_message = ""
        self._last_error_log = 0.0

    async def start(self) -> None:
        self.session = ClientSession()
        self.sender = PushSender(self.subscriptions, self.vapid, self.session)
        self.monitor_task = asyncio.create_task(self._monitor_loop())

    async def close(self) -> None:
        if self.monitor_task is not None:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
        if self.monitor is not None:
            await asyncio.to_thread(self.monitor.close)
            self.monitor = None
        if self.session is not None:
            await self.session.close()
            self.session = None

    async def _monitor_loop(self) -> None:
        while True:
            started = time.monotonic()
            failed = False
            try:
                if self.monitor is None:
                    self.monitor = await asyncio.to_thread(
                        NotificationMonitor, None, self.cursor_store
                    )
                events = await asyncio.to_thread(self.monitor.poll)
                self.history_ready = True
                self.last_error = ""
                self.last_error_message = ""
                if self.sender is not None:
                    for event in events:
                        await self.sender.send_all(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failed = True
                self.history_ready = False
                error_name, self.last_error_message = _error_details(exc)
                should_log = (
                    error_name != self.last_error
                    or started - self._last_error_log >= 60.0
                )
                self.last_error = error_name
                if should_log:
                    if isinstance(exc, (HistoryError, NotificationError, OSError)):
                        LOG.warning("notification monitor unavailable (%s)", error_name)
                    else:
                        LOG.exception("notification monitor poll failed (%s)", error_name)
                    self._last_error_log = started
                if self.monitor is not None:
                    try:
                        await asyncio.to_thread(self.monitor.close)
                    except Exception:
                        LOG.exception("failed to close notification monitor")
                    self.monitor = None
            elapsed = time.monotonic() - started
            interval = 5.0 if failed else POLL_INTERVAL_SECONDS
            await asyncio.sleep(max(0.25, interval - elapsed))


def _request_origin(request: web.Request) -> str:
    origin = request.headers.get("Origin", "").rstrip("/")
    forwarded_proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    expected = f"{forwarded_proto}://{request.host}".rstrip("/")
    if not origin or origin != expected:
        raise web.HTTPForbidden(text="same-origin request required")
    return origin


async def _json_body(request: web.Request) -> object:
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType(text="application/json required")
    try:
        return await request.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise web.HTTPBadRequest(text="invalid JSON") from exc


def create_app(runtime: NotificationRuntime) -> web.Application:
    app = web.Application(client_max_size=MAX_API_BODY_BYTES)
    # Drag-and-drop attachment. Deliberately independent of the history reader
    # and its monitor, so a stale key or a pending re-decrypt cannot stop a
    # dropped file from reaching the input box.
    attach_preparer = AttachPreparer()
    # Serializes attach requests inside this process; the flock inside
    # AttachPreparer covers the MCP server drafting a reply at the same time.
    attach_lock = asyncio.Lock()

    async def config(_: web.Request) -> web.Response:
        payload: dict[str, object] = {
            "version": 1,
            "ready": runtime.history_ready,
            "vapidPublicKey": runtime.vapid.public_key,
        }
        if not runtime.history_ready and runtime.last_error:
            payload["error"] = {
                "code": runtime.last_error,
                "message": runtime.last_error_message,
            }
        return web.json_response(payload, headers={"Cache-Control": "no-store"})

    async def put_subscription(request: web.Request) -> web.Response:
        origin = _request_origin(request)
        body = await _json_body(request)
        try:
            item = runtime.subscriptions.upsert(body, origin)
        except NotificationError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        return web.json_response({"ok": True, "id": item["id"]})

    async def delete_subscription(request: web.Request) -> web.Response:
        _request_origin(request)
        body = await _json_body(request)
        if not isinstance(body, dict) or not isinstance(body.get("endpoint"), str):
            raise web.HTTPBadRequest(text="endpoint required")
        removed = runtime.subscriptions.remove_endpoint(body["endpoint"])
        return web.json_response({"ok": True, "removed": removed})

    async def test_subscription(request: web.Request) -> web.Response:
        _request_origin(request)
        body = await _json_body(request)
        if not isinstance(body, dict) or not isinstance(body.get("endpoint"), str):
            raise web.HTTPBadRequest(text="endpoint required")
        item = runtime.subscriptions.get_by_endpoint(body["endpoint"])
        if item is None:
            raise web.HTTPNotFound(text="subscription not found")
        if runtime.sender is None:
            raise web.HTTPServiceUnavailable(text="push sender not ready")
        event = NotificationEvent(
            title="微信提醒已启用",
            body="关闭标签页后，新私聊会通过 Chrome 推送到这里。",
            tag="wechat-notifications-test",
            topic="wechat-notifications-test",
            timestamp_ms=int(time.time() * 1000),
            force=True,
        )
        asyncio.create_task(runtime.sender.send_one(item, event))
        return web.json_response({"ok": True}, status=202)

    async def attach(request: web.Request) -> web.Response:
        _request_origin(request)
        body = await _json_body(request)
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="object body required")
        file_name = body.get("fileName")
        size = body.get("size")
        kind = body.get("kind")
        if not isinstance(file_name, str) or not file_name:
            raise web.HTTPBadRequest(text="fileName required")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise web.HTTPBadRequest(text="size must be a non-negative integer")
        if kind not in ("image", "file"):
            raise web.HTTPBadRequest(text="kind must be image or file")
        async with attach_lock:
            try:
                result = await asyncio.to_thread(
                    attach_preparer.attach, file_name, size, kind
                )
            except HistoryError as exc:
                return web.json_response(exc.payload(), status=409)
        return web.json_response(result)

    app.router.add_get("/wechat-notifications/api/config", config)
    app.router.add_put("/wechat-notifications/api/subscription", put_subscription)
    app.router.add_delete("/wechat-notifications/api/subscription", delete_subscription)
    app.router.add_post("/wechat-notifications/api/test", test_subscription)
    app.router.add_post("/wechat-notifications/api/attach", attach)

    async def lifecycle(_: web.Application):
        await runtime.start()
        yield
        await runtime.close()

    app.cleanup_ctx.append(lifecycle)
    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="wechat-notifications %(levelname)s %(message)s",
    )
    runtime = NotificationRuntime()
    app = create_app(runtime)
    web.run_app(
        app,
        host=BIND_HOST,
        port=BIND_PORT,
        print=None,
        access_log=None,
        handle_signals=hasattr(signal, "SIGTERM"),
    )


if __name__ == "__main__":
    main()
