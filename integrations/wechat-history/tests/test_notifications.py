from __future__ import annotations

import base64
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# 身份通过环境变量注入（与生产同一机制）；这里固定合成值，保证测试确定性。
os.environ["WECHAT_HISTORY_ACCOUNT_DIR"] = "wxid_testaccount_0000"
os.environ["WECHAT_HISTORY_USERNAME"] = "wxid_testaccount"
os.environ["WECHAT_HISTORY_IDENTITY_TOKENS"] = "测试身份,testidentity"

from aiohttp import ClientSession
from aiohttp.test_utils import TestClient, TestServer
from pywebpush import WebPushException

from wechat_history.errors import fail
from wechat_history.notifications import (
    NotificationError,
    _error_details,
    _raise_wechat_window,
    MAX_SUBSCRIPTIONS,
    CursorStore,
    DirectSession,
    MessageCursor,
    MessageSnapshotPending,
    NewMessage,
    NotificationEvent,
    NotificationMonitor,
    PushSender,
    SessionState,
    SubscriptionStore,
    VapidKeys,
    build_notification,
    create_app,
    read_new_messages,
    scan_direct_sessions,
    validate_subscription,
)
from wechat_history.reply import CommandRunner


def encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def subscription(number: int = 1) -> dict:
    return {
        "endpoint": f"https://push.example.invalid/send/{number}",
        "expirationTime": None,
        "keys": {
            "p256dh": encoded(b"\x04" + bytes([number % 251 + 1]) * 64),
            "auth": encoded(bytes([number % 251 + 1]) * 16),
        },
    }


def direct(
    local_id: int,
    *,
    timestamp: int = 100,
    unread: int = 0,
    session_id: str = "friend",
) -> DirectSession:
    return DirectSession(
        session_id=session_id,
        display_name="Alice",
        cursor=MessageCursor(timestamp, local_id),
        unread_count=unread,
        preview="session preview",
    )


class FakeReader:
    def close(self) -> None:
        pass


class FakeWindowRunner(CommandRunner):
    def __init__(self, window_id: str = "123", found: bool = True):
        self.commands: list[list[str]] = []
        self.window_id = window_id
        self.found = found

    def output(self, arguments, *, input_data=None) -> bytes:
        self.commands.append(arguments)
        if arguments[:4] == ["xdotool", "search", "--onlyvisible", "--class"]:
            return (self.window_id + "\n").encode() if self.found else b""
        if arguments[:2] == ["xdotool", "getwindowgeometry"]:
            return b"WIDTH=1280\nHEIGHT=800\n"
        return b""

    def action(self, arguments, *, input_data=None) -> None:
        self.commands.append(arguments)


class MonitorTests(unittest.TestCase):
    def test_first_run_baselines_without_historical_notifications(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CursorStore(Path(temporary) / "state.json")
            sessions = {"friend": direct(7, unread=344)}
            with patch(
                "wechat_history.notifications.scan_direct_sessions",
                return_value=sessions,
            ):
                monitor = NotificationMonitor(FakeReader(), store)
                self.assertEqual(monitor.poll(now=0), [])
            exists, state = store.load()
            self.assertTrue(exists)
            self.assertEqual(state["friend"].cursor, MessageCursor(100, 7))
            self.assertEqual(state["friend"].unread_count, 344)

    def test_incoming_message_notifies_even_when_unread_does_not_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CursorStore(Path(temporary) / "state.json")
            current = {"friend": direct(1)}
            messages = [NewMessage(MessageCursor(101, 2), "incoming", "hello", "text")]
            with patch(
                "wechat_history.notifications.scan_direct_sessions",
                side_effect=lambda _: dict(current),
            ), patch(
                "wechat_history.notifications.read_new_messages",
                return_value=messages,
            ):
                monitor = NotificationMonitor(FakeReader(), store)
                self.assertEqual(monitor.poll(now=0), [])
                current["friend"] = direct(2, timestamp=101)
                self.assertEqual(monitor.poll(now=1), [])
                events = monitor.poll(now=3.1)
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0].title, "Alice")
                self.assertEqual(events[0].body, "hello")

    def test_outgoing_message_advances_state_without_notification_or_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CursorStore(Path(temporary) / "state.json")
            current = {"friend": direct(1)}
            messages = [NewMessage(MessageCursor(101, 2), "outgoing", "sent", "text")]
            with patch(
                "wechat_history.notifications.scan_direct_sessions",
                side_effect=lambda _: dict(current),
            ), patch(
                "wechat_history.notifications.read_new_messages",
                return_value=messages,
            ) as read:
                monitor = NotificationMonitor(FakeReader(), store)
                monitor.poll(now=0)
                current["friend"] = direct(2, timestamp=101)
                monitor.poll(now=1)
                self.assertEqual(monitor.poll(now=4), [])
                self.assertEqual(monitor.poll(now=8), [])
                self.assertEqual(read.call_count, 1)

    def test_unknown_direction_falls_back_only_when_unread_increases(self) -> None:
        previous = SessionState(MessageCursor(100, 1), 3)
        session = direct(2, timestamp=101, unread=5)
        messages = [NewMessage(MessageCursor(101, 2), "unknown", "", "unknown")]
        event = build_notification(session, previous, messages)
        self.assertIsNotNone(event)
        self.assertTrue(event.body.startswith("2 条新消息"))
        self.assertIsNone(
            build_notification(direct(2, timestamp=101, unread=3), previous, messages)
        )

    def test_snapshot_race_retries_without_advancing_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CursorStore(Path(temporary) / "state.json")
            current = {"friend": direct(1)}
            results = [
                MessageSnapshotPending("busy"),
                [NewMessage(MessageCursor(101, 2), "incoming", "later", "text")],
            ]

            def read(*_):
                value = results.pop(0)
                if isinstance(value, Exception):
                    raise value
                return value

            with patch(
                "wechat_history.notifications.scan_direct_sessions",
                side_effect=lambda _: dict(current),
            ), patch(
                "wechat_history.notifications.read_new_messages",
                side_effect=read,
            ):
                monitor = NotificationMonitor(FakeReader(), store)
                monitor.poll(now=0)
                current["friend"] = direct(2, timestamp=101)
                monitor.poll(now=1)
                self.assertEqual(monitor.poll(now=4), [])
                events = monitor.poll(now=6)
                self.assertEqual([event.body for event in events], ["later"])

    def test_unread_growth_falls_back_after_snapshot_stays_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CursorStore(Path(temporary) / "state.json")
            current = {"friend": direct(1, unread=0)}
            with patch(
                "wechat_history.notifications.scan_direct_sessions",
                side_effect=lambda _: dict(current),
            ), patch(
                "wechat_history.notifications.read_new_messages",
                side_effect=MessageSnapshotPending("missing table"),
            ):
                monitor = NotificationMonitor(FakeReader(), store)
                monitor.poll(now=0)
                current["friend"] = direct(2, timestamp=101, unread=2)
                monitor.poll(now=1)
                events = monitor.poll(now=6.1)
            self.assertEqual(len(events), 1)
            self.assertTrue(events[0].body.startswith("2 条新消息"))

    def test_restart_uses_persisted_cursor_and_coalesces_messages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CursorStore(Path(temporary) / "state.json")
            store.save({"friend": SessionState(MessageCursor(100, 1), 0)})
            sessions = {"friend": direct(3, timestamp=102)}
            messages = [
                NewMessage(MessageCursor(101, 2), "incoming", "first", "text"),
                NewMessage(MessageCursor(102, 3), "incoming", "second", "text"),
            ]
            with patch(
                "wechat_history.notifications.scan_direct_sessions",
                return_value=sessions,
            ), patch(
                "wechat_history.notifications.read_new_messages",
                return_value=messages,
            ):
                monitor = NotificationMonitor(FakeReader(), store)
                monitor.poll(now=0)
                events = monitor.poll(now=3)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].body, "2 条新消息 · second")


class DatabaseSelectionTests(unittest.TestCase):
    def test_only_contact_backed_direct_visible_sessions_are_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "session.db"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """CREATE TABLE SessionTable (
                        username TEXT, unread_count INTEGER, summary TEXT,
                        last_timestamp INTEGER, last_msg_locald_id INTEGER,
                        is_hidden INTEGER
                    )"""
                )
                rows = [
                    ("friend", 0, "hello", 100, 1, 0),
                    ("group@chatroom", 1, "group", 101, 2, 0),
                    ("gh_news", 1, "news", 102, 3, 0),
                    ("filehelper", 1, "file", 103, 4, 0),
                    ("hidden", 1, "hidden", 104, 5, 1),
                    ("not-a-contact", 1, "other", 105, 6, 0),
                ]
                connection.executemany("INSERT INTO SessionTable VALUES (?,?,?,?,?,?)", rows)

            reader = SimpleNamespace()
            reader.ensure_account_validated = lambda: None
            reader._load_contacts = lambda: {
                "friend": {"display_name": "Alice"},
                "group@chatroom": {"display_name": "Group"},
                "gh_news": {"display_name": "News"},
                "filehelper": {"display_name": "Files"},
                "hidden": {"display_name": "Hidden"},
            }
            reader.cache = SimpleNamespace(get=lambda _: database)
            sessions = scan_direct_sessions(reader)
            self.assertEqual(list(sessions), ["friend"])

    def test_message_cursor_distinguishes_same_second_local_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "message.db"
            table = "Msg_" + __import__("hashlib").md5(b"friend").hexdigest()
            with sqlite3.connect(database) as connection:
                connection.execute(
                    f"""CREATE TABLE [{table}] (
                        local_id INTEGER, local_type INTEGER, create_time INTEGER,
                        real_sender_id INTEGER, message_content BLOB,
                        WCDB_CT_message_content INTEGER
                    )"""
                )
                connection.execute("CREATE TABLE Name2Id (user_name TEXT)")
                connection.execute("INSERT INTO Name2Id(rowid, user_name) VALUES (2, 'friend')")
                connection.executemany(
                    f"INSERT INTO [{table}] VALUES (?, 1, 100, 2, ?, 0)",
                    [(1, b"old"), (2, b"new"), (3, b"future")],
                )
            reader = SimpleNamespace()
            reader._message_table = lambda _: table
            reader._load_contacts = lambda: {"friend": {"display_name": "Alice"}}
            reader._message_database_keys = lambda: ["message/message_0.db"]
            reader.cache = SimpleNamespace(get=lambda _: database)
            reader._name_map = lambda connection: {2: "friend"}
            from wechat_history.reader import HistoryReader

            reader._message_identifier = HistoryReader._message_identifier
            reader._message_item = HistoryReader._message_item.__get__(reader)
            messages = read_new_messages(
                reader,
                direct(2, timestamp=100),
                MessageCursor(100, 1),
            )
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0].text, "new")
            self.assertEqual(messages[0].direction, "incoming")


class PersistentStoreTests(unittest.TestCase):
    def test_subscription_validation_and_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "subscriptions.json"
            store = SubscriptionStore(path)
            item = store.upsert(subscription(), "https://wechat.example")
            self.assertEqual(len(store.all()), 1)
            reloaded = SubscriptionStore(path)
            self.assertEqual(reloaded.get_by_endpoint(subscription()["endpoint"])["id"], item["id"])
            self.assertTrue(reloaded.remove_endpoint(subscription()["endpoint"]))
            self.assertEqual(reloaded.all(), [])

    def test_rejects_non_https_or_malformed_subscription(self) -> None:
        invalid = subscription()
        invalid["endpoint"] = "http://push.example/send"
        with self.assertRaises(Exception):
            validate_subscription(invalid)
        invalid = subscription()
        invalid["keys"]["auth"] = encoded(b"short")
        with self.assertRaises(Exception):
            validate_subscription(invalid)

    def test_subscription_limit_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SubscriptionStore(Path(temporary) / "subscriptions.json")
            for number in range(MAX_SUBSCRIPTIONS):
                store.upsert(subscription(number + 1), "https://wechat.example")
            with self.assertRaises(Exception):
                store.upsert(subscription(MAX_SUBSCRIPTIONS + 1), "https://wechat.example")

    def test_corrupt_state_is_preserved_and_rebaselined(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "state.json"
            path.write_text("not-json", encoding="utf-8")
            exists, state = CursorStore(path).load()
            self.assertFalse(exists)
            self.assertEqual(state, {})
            self.assertEqual(len(list(root.glob("state.json.corrupt-*"))), 1)

    def test_vapid_key_persists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "vapid.pem"
            first = VapidKeys(path)
            second = VapidKeys(path)
            self.assertEqual(first.public_key, second.public_key)
            self.assertTrue(first.public_key.startswith("B"))
            from py_vapid import Vapid

            self.assertIsNotNone(Vapid.from_file(private_key_file=str(path)))
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class PushSenderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = SubscriptionStore(root / "subscriptions.json")
        self.item = self.store.upsert(subscription(), "https://wechat.example")
        self.vapid = VapidKeys(root / "vapid.pem")
        self.session = ClientSession()
        self.event = NotificationEvent("Alice", "hello", "tag", "topic", 1000)

    async def asyncTearDown(self) -> None:
        await self.session.close()
        self.temporary.cleanup()

    async def test_expired_subscription_is_removed(self) -> None:
        class Response:
            status = 410

        async def push(**_):
            raise WebPushException("gone", response=Response())

        sender = PushSender(self.store, self.vapid, self.session, push_call=push)
        self.assertFalse(await sender.send_one(self.item, self.event))
        self.assertEqual(self.store.all(), [])

    async def test_transient_failure_retries_then_succeeds(self) -> None:
        calls = []

        class Response:
            status = 500

        async def push(**_):
            calls.append(1)
            if len(calls) < 3:
                raise WebPushException("retry", response=Response())
            return object()

        async def no_sleep(_):
            return None

        sender = PushSender(
            self.store,
            self.vapid,
            self.session,
            push_call=push,
            sleep_call=no_sleep,
        )
        self.assertTrue(await sender.send_one(self.item, self.event))
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(self.store.all()), 1)


class WindowRaiseTests(unittest.TestCase):
    def test_raise_activates_found_window(self) -> None:
        runner = FakeWindowRunner()
        self.assertTrue(_raise_wechat_window(runner))
        self.assertIn(["xdotool", "windowactivate", "--sync", "123"], runner.commands)

    def test_raise_gives_up_when_window_missing(self) -> None:
        runner = FakeWindowRunner(found=False)
        self.assertFalse(_raise_wechat_window(runner))
        self.assertFalse(
            any("windowactivate" in command for command in runner.commands)
        )


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)

        class Sender:
            async def send_one(_, item, event):
                self.sent = (item, event)
                return True

        class Runtime:
            history_ready = True
            last_error = ""
            last_error_message = ""
            vapid = SimpleNamespace(public_key="public-key")
            subscriptions = SubscriptionStore(root / "subscriptions.json")
            sender = Sender()
            window_runner = FakeWindowRunner()

            async def start(_):
                return None

            async def close(_):
                return None

        self.runtime = Runtime()
        self.server = TestServer(create_app(self.runtime))
        self.client = TestClient(self.server)
        await self.client.start_server()
        self.origin = str(self.client.make_url("/")).rstrip("/")

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.temporary.cleanup()

    async def test_same_origin_subscription_and_test(self) -> None:
        response = await self.client.put(
            "/wechat-notifications/api/subscription",
            json=subscription(),
            headers={"Origin": self.origin},
        )
        self.assertEqual(response.status, 200)
        response = await self.client.post(
            "/wechat-notifications/api/test",
            json={"endpoint": subscription()["endpoint"]},
            headers={"Origin": self.origin},
        )
        self.assertEqual(response.status, 202)

    async def test_cross_origin_write_is_rejected(self) -> None:
        response = await self.client.put(
            "/wechat-notifications/api/subscription",
            json=subscription(),
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(response.status, 403)

    async def test_ready_config_carries_no_error(self) -> None:
        response = await self.client.get("/wechat-notifications/api/config")
        payload = await response.json()
        self.assertTrue(payload["ready"])
        self.assertNotIn("error", payload)

    async def test_config_reports_monitor_failure_reason(self) -> None:
        self.runtime.history_ready = False
        self.runtime.last_error = "KEY_STALE"
        self.runtime.last_error_message = "保存的密钥已失效；请显式重新扫描"
        response = await self.client.get("/wechat-notifications/api/config")
        payload = await response.json()
        self.assertFalse(payload["ready"])
        self.assertEqual(
            payload["error"],
            {"code": "KEY_STALE", "message": "保存的密钥已失效；请显式重新扫描"},
        )

    async def test_raise_activates_window_for_same_origin_request(self) -> None:
        response = await self.client.post(
            "/wechat-notifications/api/raise",
            json={"tag": "wechat-abc"},
            headers={"Origin": self.origin},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"ok": True, "raised": True})

    async def test_raise_reports_not_raised_when_window_missing(self) -> None:
        self.runtime.window_runner = FakeWindowRunner(found=False)
        response = await self.client.post(
            "/wechat-notifications/api/raise",
            json={"tag": "wechat-abc"},
            headers={"Origin": self.origin},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"ok": True, "raised": False})

    async def test_raise_rejects_cross_origin(self) -> None:
        response = await self.client.post(
            "/wechat-notifications/api/raise",
            json={"tag": "wechat-abc"},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(response.status, 403)

    async def test_raise_rejects_non_string_tag(self) -> None:
        response = await self.client.post(
            "/wechat-notifications/api/raise",
            json={"tag": 123},
            headers={"Origin": self.origin},
        )
        self.assertEqual(response.status, 400)


class ErrorDetailTests(unittest.TestCase):
    def test_history_error_exposes_code_and_safe_message(self) -> None:
        code, message = _error_details(fail("KEY_STALE", "保存的密钥已失效"))
        self.assertEqual(code, "KEY_STALE")
        self.assertEqual(message, "保存的密钥已失效")

    def test_notification_error_exposes_its_message(self) -> None:
        code, message = _error_details(NotificationError("invalid cursor state"))
        self.assertEqual(code, "NotificationError")
        self.assertEqual(message, "invalid cursor state")

    def test_unexpected_error_exposes_type_name_only(self) -> None:
        code, message = _error_details(FileNotFoundError("/config/secret/path"))
        self.assertEqual(code, "FileNotFoundError")
        self.assertEqual(message, "")


if __name__ == "__main__":
    unittest.main()
