"""Today's picks, VIP slips, and the credit-spend unlock flow."""

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from apps.bot import keyboards as kb
from apps.bot import services

router = Router(name="picks")


def _teaser(prediction) -> str:
    """Free view: the fixture and the confidence, but not the selection."""
    fx = prediction.fixture
    return (
        f"<b>{fx.home.name} vs {fx.away.name}</b>\n"
        f"{fx.league.name} · {fx.kickoff:%H:%M UTC}\n"
        f"Market: {prediction.get_market_display()}\n"
        f"Confidence: <b>{prediction.confidence}%</b>"
    )


def _full(prediction) -> str:
    fx = prediction.fixture
    odds = f"\nBest price: {prediction.market_odds}" if prediction.market_odds else ""
    edge = f" (edge {prediction.edge:+.1%})" if prediction.edge else ""
    rationale = f"\n\n{prediction.rationale}" if prediction.rationale else ""
    return (
        f"<b>{fx.home.name} vs {fx.away.name}</b>\n"
        f"{fx.league.name} · {fx.kickoff:%H:%M UTC}\n\n"
        f"<b>{prediction.get_market_display()}: {prediction.selection_label}</b>\n"
        f"Confidence: {prediction.confidence}%{odds}{edge}"
        f"{rationale}\n\n"
        "<i>A confidence rating is not a guarantee. Stake responsibly.</i>"
    )


@router.message(F.text == kb.BTN_TODAY)
async def todays_picks(message: Message) -> None:
    await services.get_or_create_user(message.from_user)
    picks = await services.todays_free_picks()

    if not picks:
        await message.answer(
            "No picks published yet for today — the model runs a few hours before "
            "the first kickoff, once team news lands."
        )
        return

    await message.answer(f"<b>Today's picks</b> — {len(picks)} fixtures analysed")
    for prediction in picks:
        await message.answer(_teaser(prediction), reply_markup=kb.unlock_keyboard(prediction.pk))


@router.message(F.text == kb.BTN_VIP)
async def vip_slips(message: Message) -> None:
    user, _ = await services.get_or_create_user(message.from_user)
    summary = await services.plan_summary(user)

    if not summary["vip"]:
        plans = await services.active_plans()
        await message.answer(
            "<b>VIP slips</b> are curated accumulators — the day's strongest reads "
            "combined into one slip.\n\nAvailable on a paid plan:",
            reply_markup=kb.plans_keyboard(plans),
        )
        return

    slip = await services.vip_slip_for_today()
    if slip is None:
        await message.answer("Today's VIP slip isn't out yet — you'll get a ping when it drops.")
        return

    body = [f"<b>{slip.title}</b>", ""]
    for prediction in slip.predictions.all():
        fx = prediction.fixture
        body.append(
            f"• {fx.home.name} v {fx.away.name} — "
            f"<b>{prediction.selection_label}</b> ({prediction.confidence}%)"
        )
    if slip.total_odds:
        body += ["", f"Combined odds: <b>{slip.total_odds}</b>"]
    await message.answer("\n".join(body))


@router.callback_query(F.data.startswith("unlock:"))
async def unlock(callback: CallbackQuery) -> None:
    user, _ = await services.get_or_create_user(callback.from_user)
    prediction_id = int(callback.data.split(":", 1)[1])
    prediction, status = await services.unlock_prediction(user, prediction_id)

    match status:
        case "missing":
            await callback.answer("That pick is no longer available.", show_alert=True)
        case "insufficient":
            plans = await services.active_plans()
            await callback.answer("Out of credits.", show_alert=True)
            await callback.message.answer(
                "You're out of credits. Pick a plan to keep going:",
                reply_markup=kb.plans_keyboard(plans),
            )
        case _:
            await callback.answer()
            await callback.message.edit_text(_full(prediction))
