"""
Event-loop lifetime.

Celery runs each task in its own `asyncio.run(...)`, which closes its loop when
the task ends. A Bot owns an aiohttp session bound to the loop that first used
it, so caching one across tasks breaks in a way that is easy to miss in review
and brutal in production: the first update after a worker start is handled
normally, every one after it raises "Event loop is closed", and Telegram still
receives its 200 and never retries. The bot simply stops answering.
"""

import asyncio
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from apps.bot import dispatcher


@override_settings(TELEGRAM_BOT_TOKEN="123:ABC")
class BotLifetimeTests(SimpleTestCase):
    def test_each_call_returns_a_new_bot(self):
        """A cached Bot is the bug; distinct instances are the fix."""
        self.assertIsNot(dispatcher.get_bot(), dispatcher.get_bot())

    def test_missing_token_is_a_clean_error(self):
        with override_settings(TELEGRAM_BOT_TOKEN=""):
            with self.assertRaises(RuntimeError):
                dispatcher.get_bot()

    def test_session_is_closed_when_the_context_exits(self):
        closed = []

        async def run():
            async with dispatcher.bot_session() as bot:
                # Patch the real close so the test asserts it was called without
                # depending on aiohttp internals.
                original = bot.session.close

                async def spy():
                    closed.append(True)
                    await original()

                bot.session.close = spy

        asyncio.run(run())
        self.assertEqual(closed, [True], "the aiohttp session must be closed with its loop")

    def test_feed_update_survives_consecutive_event_loops(self):
        """
        Two updates through two `asyncio.run` calls — what two Celery tasks do.

        A smoke test, not a faithful reproduction: `feed_update` is mocked, so
        no aiohttp session ever binds to a loop and this would pass against the
        old cached Bot too. `test_each_call_returns_a_new_bot` is the guard that
        actually fails when the singleton comes back; reproducing the real
        failure needs a live HTTP call, which the suite deliberately never makes.
        """
        seen = []

        async def fake_feed(bot, update):
            # A live loop and a usable session are exactly what the bug removed.
            self.assertIsNotNone(asyncio.get_running_loop())
            self.assertIsNotNone(bot.session)
            seen.append(update.update_id)

        payload = {"update_id": 1, "message": {
            "message_id": 1, "date": 0, "text": "hi",
            "chat": {"id": 1, "type": "private"},
            "from": {"id": 1, "is_bot": False, "first_name": "T"},
        }}

        with patch.object(dispatcher.get_dispatcher(), "feed_update", fake_feed):
            asyncio.run(dispatcher.feed_update(payload))
            asyncio.run(dispatcher.feed_update({**payload, "update_id": 2}))

        self.assertEqual(seen, [1, 2])

    def test_dispatcher_is_cached(self):
        """Routers hold no loop-bound state, so re-registering them each call
        would be waste — only the Bot must be per-loop."""
        self.assertIs(dispatcher.get_dispatcher(), dispatcher.get_dispatcher())
