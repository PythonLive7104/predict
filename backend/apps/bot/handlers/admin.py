"""
Owner-facing controls: approving the manual crypto payments.

Every handler here is gated on TELEGRAM_ADMIN_IDS. The gate is checked against
the Telegram id of whoever *tapped* the button, not against who the message was
sent to — a forwarded message carries its buttons with it, and callback data is
just a string anyone can replay.
"""

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from django.conf import settings

from apps.bot import services

logger = logging.getLogger(__name__)

router = Router(name="admin")


def is_admin(telegram_id: int) -> bool:
    return telegram_id in settings.TELEGRAM_ADMIN_IDS


@router.callback_query(F.data.startswith("pay:"))
async def decide(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        # Deliberately vague: an unauthorised tapper learns nothing about
        # whether the payment exists.
        await callback.answer("Not available.", show_alert=True)
        logger.warning("non-admin %s tapped a review button", callback.from_user.id)
        return

    _, verdict, raw_id = callback.data.split(":", 2)
    payment, status = await services.decide_payment(
        int(raw_id), callback.from_user.id, approved=(verdict == "ok")
    )

    match status:
        case "missing":
            await callback.answer("That payment no longer exists.", show_alert=True)
            return
        case "already_approved":
            await callback.answer("Already approved.", show_alert=True)
            outcome = "✅ Approved (already)"
        case "approved":
            await callback.answer("Approved — plan granted.")
            outcome = f"✅ Approved by {callback.from_user.first_name}"
            await _tell_user(callback.bot, payment, granted=True)
        case _:
            await callback.answer("Rejected.")
            outcome = f"❌ Rejected by {callback.from_user.first_name}"
            await _tell_user(callback.bot, payment, granted=False)

    # Buttons are removed rather than left live, so a second admin opening the
    # same notification cannot tap an already-settled payment.
    await callback.message.edit_text(
        f"{callback.message.html_text}\n\n<b>{outcome}</b>", reply_markup=None
    )


async def _tell_user(bot, payment: dict, granted: bool) -> None:
    """The customer is the one waiting; tell them without being asked."""
    text = (
        f"🎉 <b>Payment confirmed</b>\n\n{payment['plan']} is now active. "
        "Your picks are unlocked — tap ⚽ Today's Picks."
        if granted
        else (
            "We couldn't verify that transaction.\n\n"
            "This usually means the amount didn't match, it hasn't confirmed "
            "yet, or the hash was for a different wallet. Reply here and we'll "
            "sort it out."
        )
    )
    try:
        await bot.send_message(payment["user_id"], text)
    except Exception:
        # Blocked us, most likely. The decision still stands.
        logger.exception("could not notify user %s of payment outcome", payment["user_id"])


@router.message(Command("pending"))
async def pending(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    count = await services.pending_review_count()
    await message.answer(
        f"{count} payment(s) awaiting review."
        if count
        else "Nothing waiting for review."
    )
