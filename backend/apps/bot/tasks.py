"""
Telegram update processing.

The webhook hands raw updates here so the HTTP response is instant. aiogram is
async and Celery workers are sync, so the dispatcher is driven inside a fresh
event loop per task — fine at this volume, and it keeps the worker model simple.
"""

import asyncio
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def process_update(update: dict) -> None:
    from .dispatcher import feed_update

    asyncio.run(feed_update(update))


@shared_task
def send_broadcast(broadcast_id: int) -> int:
    """
    Fans out one Broadcast. Delivery rows are unique-constrained per (user,
    broadcast), so a retry re-sends to nobody who already received it.
    """
    from .dispatcher import deliver_broadcast

    return asyncio.run(deliver_broadcast(broadcast_id))
