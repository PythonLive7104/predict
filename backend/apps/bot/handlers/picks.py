"""Today's picks, VIP slips, and the credit-spend unlock flow."""

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from apps.bot import keyboards as kb
from apps.bot import services
from apps.predictions.models import Tier

router = Router(name="picks")


def _teaser(prediction) -> str:
    """
    Locked view: the fixture only. Market, confidence and selection all arrive
    together on unlock.

    Note the trade this makes. Confidence was the hook — "86%" is what decided
    which match was worth a credit — so with it hidden every row reads the same
    and the unlock is a blind purchase. Kept deliberately per the product
    owner's decision; if conversion drops, this is the first thing to revisit.
    """
    fx = prediction.fixture
    # The tier is the one thing a locked card may say about itself: it is the
    # difference between "spend a credit" and "this is what a plan buys", and it
    # reveals nothing about the pick.
    badge = "⭐ VIP" if prediction.tier == Tier.VIP else "Free pick"
    # The day is shown as well as the time: a weekend list spans three dates and
    # "15:00" alone would leave the reader guessing which one.
    return (
        f"<b>{fx.home.name} vs {fx.away.name}</b>\n"
        f"{fx.league.name} · {fx.kickoff:%a %d %b, %H:%M UTC}\n"
        f"{badge} · 🔒 Analysis locked"
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
    await _send_menu(message, "today", 0)


@router.callback_query(F.data.startswith("pk:"))
async def route(callback: CallbackQuery) -> None:
    """
    pk:<span>:<menu|all|league_id>:<page>

    One callback prefix for the whole surface: choosing a day, paging the league
    list and opening a league all land here, so the span travels with every tap
    and a user who switches day keeps the league they were looking at.
    """
    await services.get_or_create_user(callback.from_user)
    _, span, what, page = callback.data.split(":", 3)
    await callback.answer()

    if what == "menu":
        await _send_menu(callback.message, span, int(page))
    elif what == "all":
        await _send_picks(callback.message, span, None)
    else:
        await _send_picks(callback.message, span, int(what))


# Kept so older messages still work: Telegram leaves buttons live on messages
# that have already been sent, and a stale tap should not silently do nothing.
@router.callback_query(F.data.startswith("picks:"))
async def legacy_span(callback: CallbackQuery) -> None:
    await services.get_or_create_user(callback.from_user)
    await callback.answer()
    await _send_menu(callback.message, callback.data.split(":", 1)[1], 0)


async def _send_menu(message: Message, span: str, page: int) -> None:
    leagues, label = await services.leagues_for_span(span)

    if not leagues:
        await message.answer(
            f"<b>{label}</b>\n\nNothing published for this window yet. Picks are "
            "generated a few hours before the first kickoff, once team news lands.",
            reply_markup=kb.league_menu([], span, 0),
        )
        return

    total = sum(item["picks"] for item in leagues)
    await message.answer(
        f"<b>{label}</b> — {total} pick{'s' if total != 1 else ''} across "
        f"{len(leagues)} league{'s' if len(leagues) != 1 else ''}.\n\n"
        "Choose a league, or take the lot:",
        reply_markup=kb.league_menu(leagues, span, page),
    )


async def _send_picks(message: Message, span: str, league_id: int | None) -> None:
    picks, label = await services.picks_for_span(span, league_id=league_id)

    if not picks:
        await message.answer(
            f"<b>{label}</b>\n\nNothing published here yet.",
            reply_markup=kb.back_to_leagues(span),
        )
        return

    where = picks[0].fixture.league.name if league_id else "all leagues"
    vip_count = sum(1 for p in picks if p.tier == Tier.VIP)
    mix = f" · {vip_count} VIP" if vip_count else ""
    await message.answer(
        f"<b>{label} · {where}</b> — {len(picks)} pick"
        f"{'s' if len(picks) != 1 else ''}{mix}",
        reply_markup=kb.back_to_leagues(span),
    )
    for prediction in picks:
        await message.answer(
            _teaser(prediction),
            reply_markup=kb.unlock_keyboard(
                prediction.pk, vip=prediction.tier == Tier.VIP
            ),
        )


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
