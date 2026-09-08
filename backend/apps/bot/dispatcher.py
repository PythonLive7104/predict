"""
aiogram wiring.

One Bot/Dispatcher pair built lazily so importing this module never requires a
token (management commands and tests import it freely). Every ORM touch goes
through sync_to_async — aiogram runs in an event loop, Django's ORM does not.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update
from asgiref.sync import sync_to_async
from django.conf import settings

logger = logging.getLogger(__name__)

# The Dispatcher is cached; the Bot deliberately is not. See get_bot().
_dispatcher: Dispatcher | None = None


def get_bot() -> Bot:
    """
    A NEW Bot every call. Never cache one across event loops.

    A Bot owns an aiohttp ClientSession, and that session binds to whichever
    event loop first uses it. Celery runs each task inside its own
    `asyncio.run(...)`, which closes its loop when the task ends — so a cached
    Bot's session belongs to a dead loop by the second task. The symptom is
    precise and misleading: the first update after a worker starts is handled
    normally, and every one after it fails with "Event loop is closed" while
    Telegram still gets its 200 and never retries.

    Callers own the session and must close it. Use `bot_session()`.
    """
    if not settings.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    return Bot(
        token=settings.TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


@asynccontextmanager
async def bot_session() -> AsyncIterator[Bot]:
    """A Bot whose aiohttp session is closed with the loop that created it."""
    bot = get_bot()
    try:
        yield bot
    finally:
        await bot.session.close()


def get_dispatcher() -> Dispatcher:
    global _dispatcher
    if _dispatcher is None:
        from .handlers import admin, menu, picks, purchase

        _dispatcher = Dispatcher()
        # Admin first: its callback filter is narrow, and `purchase` ends with a
        # broad hash-shaped message filter that should only see what is left.
        _dispatcher.include_router(admin.router)
        _dispatcher.include_router(menu.router)
        _dispatcher.include_router(picks.router)
        _dispatcher.include_router(purchase.router)
    return _dispatcher


async def feed_update(payload: dict) -> None:
    async with bot_session() as bot:
        await get_dispatcher().feed_update(
            bot, Update.model_validate(payload, context={"bot": bot})
        )


async def deliver_broadcast(broadcast_id: int) -> int:
    """
    Send one broadcast to its audience, recording a Delivery per user. Telegram's
    limit is ~30 messages/second to distinct chats; the sleep keeps us under it,
    because a flood-wait ban is the one failure mode that kills a bot outright.
    """
    import asyncio

    from .models import Broadcast, Delivery

    broadcast = await sync_to_async(Broadcast.objects.get)(pk=broadcast_id)

    from .notifications import audience as build_audience

    recipients = await sync_to_async(build_audience)(broadcast.target_tier)

    sent = failed = 0
    async with bot_session() as bot:
        async for user in recipients.aiterator():
            delivery, created = await Delivery.objects.aget_or_create(
                user=user, broadcast=broadcast
            )
            if not created and delivery.delivered:
                continue
            try:
                message = await bot.send_message(user.telegram_id, broadcast.body)
                delivery.message_id = message.message_id
                delivery.delivered = True
                sent += 1
            except Exception as exc:
                delivery.error = str(exc)
                failed += 1
                # Telegram reports a block/deactivation as a 403. Recording it is
                # the only way is_blocked ever becomes true — never inferred
                # from silence.
                if ("bot was blocked" in str(exc).lower()
                        or "user is deactivated" in str(exc).lower()):
                    user.is_blocked = True
                    await user.asave(update_fields=["is_blocked"])
            await delivery.asave()
            await asyncio.sleep(0.05)

    broadcast.sent_count, broadcast.failed_count = sent, failed
    broadcast.status = Broadcast.Status.SENT
    await broadcast.asave()
    return sent
