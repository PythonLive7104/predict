"""Plan selection → crypto invoice. Credits are only ever granted by the IPN."""

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from asgiref.sync import sync_to_async

from apps.billing.models import Payment
from apps.billing.nowpayments import create_invoice
from apps.bot import keyboards as kb
from apps.bot import services

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

    payment = await sync_to_async(Payment.objects.create)(
        user=user, plan=plan, amount_usd=plan.price_usd
    )
    try:
        payment = await sync_to_async(create_invoice)(payment)
    except Exception:
        await callback.answer()
        await callback.message.answer(
            "Couldn't open a payment window just now. Try again in a moment, or "
            "contact support and we'll sort it manually."
        )
        return

    await callback.answer()
    await callback.message.answer(
        f"<b>{plan.name}</b> — ${kb.money(plan.price_usd)}\n\n"
        f"Pay here: {payment.invoice_url}\n\n"
        "Your plan activates automatically once the payment confirms on-chain "
        "(usually a few minutes). You'll get a message here when it does."
    )
