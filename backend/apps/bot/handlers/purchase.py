"""Plan selection → wallet → transaction hash → admin approval."""

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from django.conf import settings

from apps.bot import keyboards as kb
from apps.bot import services

logger = logging.getLogger(__name__)

router = Router(name="purchase")


@router.message(F.text == kb.BTN_PURCHASE)
async def show_plans(message: Message) -> None:
    await services.get_or_create_user(message.from_user)
    plans = await services.active_plans()

    if not plans:
        await message.answer("Plans aren't configured yet — check back shortly.")
        return

    lines = ["<b>Plans</b>", ""]
    for plan in plans:
        detail = "Unlimited picks" if plan.unlimited else f"{plan.credits_granted} credits"
        lines.append(f"• <b>${kb.money(plan.price_usd)}</b> · {plan.name} — {detail}")
    lines += ["", "Paid in crypto (USDT, BTC, and others). Choose a plan:"]

    await message.answer("\n".join(lines), reply_markup=kb.plans_keyboard(plans))


@router.callback_query(F.data == "menu:purchase")
async def show_plans_inline(callback: CallbackQuery) -> None:
    await callback.answer()
    await show_plans(callback.message)


@router.callback_query(F.data.startswith("buy:"))
async def buy(callback: CallbackQuery) -> None:
    user, _ = await services.get_or_create_user(callback.from_user)
    plan = await services.get_plan(callback.data.split(":", 1)[1])

    if plan is None:
        await callback.answer("That plan is no longer available.", show_alert=True)
        return

    wallets = await services.receiving_wallets()
    if not wallets:
        await callback.answer()
        await callback.message.answer(
            "Payment isn't set up yet — contact support and we'll sort it manually."
        )
        return

    await services.open_manual_payment(user, plan)
    await callback.answer()

    lines = [
        f"<b>{plan.name} — ${kb.money(plan.price_usd)}</b>",
        "",
        f"Send exactly <b>${kb.money(plan.price_usd)}</b> to one of these:",
        "",
    ]
    for w in wallets:
        lines += [f"<b>{w.label}</b>", f"<code>{w.address}</code>", ""]
    lines += [
        "Then send me the <b>transaction hash</b> as a message — just paste it here.",
        "",
        "<i>Your plan is activated once we've checked the transaction on-chain. "
        "That's usually quick, but it is a manual check, so allow a little time.</i>",
    ]
    await callback.message.answer("\n".join(lines))


@router.message(F.text.regexp(r"^\s*(0x)?[A-Za-z0-9]{16,128}\s*$"))
async def submit_hash(message: Message) -> None:
    """
    A bare hash-shaped message. Only treated as a submission when the user
    actually has a plan awaiting payment — otherwise it falls through silently,
    so a stray code or username in chat never becomes a payment.
    """
    user, _ = await services.get_or_create_user(message.from_user)
    payment, status = await services.submit_tx_hash(user, message.text.strip())

    match status:
        case "no_payment":
            return  # not a payment attempt; stay quiet
        case "duplicate":
            await message.answer(
                "That transaction hash has already been submitted. If you believe "
                "this is a mistake, contact support and we'll look into it."
            )
        case "resubmit":
            await message.answer(
                "We already have that one — it's in the queue. You'll get a "
                "message the moment it's approved."
            )
        case "submitted":
            await message.answer(
                "✅ <b>Received.</b>\n\n"
                f"Plan: {payment['plan']}\n"
                f"Amount: ${payment['amount']}\n"
                f"Hash: <code>{payment['tx_hash'][:20]}…</code>\n\n"
                "We'll verify it on-chain and activate your plan. You'll get a "
                "message here when it's done."
            )
            await notify_admins_of_submission(message.bot, payment)


async def notify_admins_of_submission(bot, payment: dict) -> None:
    """
    Push the review to whoever owns the wallet. Without this the queue is
    invisible and a paying customer waits on someone remembering to look.
    """
    if not settings.TELEGRAM_ADMIN_IDS:
        logger.warning("payment %s needs review but TELEGRAM_ADMIN_IDS is empty", payment["id"])
        return

    who = f"@{payment['username']}" if payment["username"] else f"id {payment['user_id']}"
    text = (
        "🔔 <b>Payment submitted</b>\n\n"
        f"User: {who}\n"
        f"Plan: {payment['plan']} — ${payment['amount']}\n"
        f"Hash: <code>{payment['tx_hash']}</code>\n\n"
        "<b>Before approving, check on-chain:</b>\n"
        "• paid to your address\n"
        f"• amount is ${payment['amount']}\n"
        "• transaction is confirmed"
    )
    for admin_id in settings.TELEGRAM_ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id, text, reply_markup=kb.review_keyboard(payment["id"])
            )
        except Exception:
            # One unreachable admin must not stop the others being told.
            logger.exception("could not notify admin %s", admin_id)
