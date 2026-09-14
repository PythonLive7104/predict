"""
The odds builder: "give me 3 odds for the weekend".

A payout request, not a leg count — the engine picks however many legs it takes
to land in the band, choosing the combination most likely to actually win.

Gated the same way individual picks are: everyone sees what the slip pays and
how likely it is, subscribers see the legs. Showing the legs free would give
away the product; showing nothing would give nobody a reason to want it.
"""

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from apps.bot import keyboards as kb
from apps.bot import services

logger = logging.getLogger(__name__)

router = Router(name="odds")


@router.message(F.text == kb.BTN_ODDS)
async def open_builder(message: Message) -> None:
    await services.get_or_create_user(message.from_user)
    await message.answer(
        "<b>Build odds</b>\n\n"
        "Pick a target payout and a day. We'll find the combination most likely "
        "to land it — however many legs that takes.\n\n"
        "<i>Higher payout means lower chance. The combined confidence is shown "
        "so you can see the trade rather than guess at it.</i>",
        reply_markup=kb.odds_keyboard("today"),
    )


@router.callback_query(F.data.startswith("odds:"))
async def build(callback: CallbackQuery) -> None:
    user, _ = await services.get_or_create_user(callback.from_user)
    _, target, span = callback.data.split(":", 2)

    await callback.answer()
    slip = await services.build_odds_slip(target, span)

    if slip is None:
        await callback.message.answer("That target isn't available.")
        return

    if not slip["found"]:
        # Say which of three very different things happened. Only one of them is
        # something the user can do anything about, and telling someone to "try
        # a lower target" when we simply have no prices sends them round a loop
        # that cannot succeed.
        match slip["reason"]:
            case "no_prices":
                body = (
                    "We don't have bookmaker prices for these matches yet.\n\n"
                    "Prices arrive a few hours before kick-off, so try again "
                    "closer to the day — or check <b>Today</b>, which is usually "
                    "priced first."
                )
            case "too_few_matches":
                n = slip["priced_fixtures"]
                body = (
                    f"Only {n} priced match{'es' if n != 1 else ''} in this window "
                    "— a slip needs at least two.\n\n"
                    "Try <b>Fri–Sun</b> for a fuller slate."
                )
            case "unreachable" if slip.get("best_available"):
                body = (
                    f"This slate tops out around <b>{slip['best_available']}</b>, "
                    f"short of {slip['target']}.\n\n"
                    "Pick a target at or below that, or widen the window to bring "
                    "in more matches."
                )
            case _:
                body = (
                    "No combination reaches that payout without stretching to a "
                    "leg we wouldn't back ourselves.\n\n"
                    "Try a lower target, or a wider window."
                )

        await callback.message.answer(
            f"<b>{slip['target']} · {slip['span']}</b>\n\n{body}",
            reply_markup=kb.odds_keyboard(span, target),
        )
        return

    summary = await services.plan_summary(user)
    header = (
        f"<b>{slip['target']} · {slip['span']}</b>\n\n"
        f"Combined odds: <b>{slip['odds']}</b>\n"
        f"Combined confidence: <b>{slip['confidence']}%</b>\n"
        f"Legs: {len(slip['legs'])}"
    )

    if not summary["vip"]:
        plans = await services.active_plans()
        await callback.message.answer(
            f"{header}\n\n🔒 The selections are part of a paid plan.",
            reply_markup=kb.plans_keyboard(plans),
        )
        await callback.message.answer(
            "Change the target or the day:", reply_markup=kb.odds_keyboard(span, target)
        )
        return

    lines = [header, ""]
    for leg in slip["legs"]:
        lines.append(
            f"• <b>{leg['home']} v {leg['away']}</b>\n"
            f"  {leg['kickoff']:%a %d %b, %H:%M} · {leg['market']}\n"
            f"  <b>{leg['selection']}</b> @ {leg['odds']} ({leg['confidence']}%)"
        )
    lines += [
        "",
        "<i>One leg per match — two picks on the same game are correlated, so "
        "combining them would overstate the payout for the risk. A confidence "
        "rating is not a guarantee. Stake responsibly.</i>",
    ]

    await callback.message.answer("\n".join(lines), reply_markup=kb.odds_keyboard(span, target))
