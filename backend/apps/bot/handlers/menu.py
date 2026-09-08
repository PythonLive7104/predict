"""/start, the persistent menu, plan status, record, support."""

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message

from apps.bot import keyboards as kb
from apps.bot import services

router = Router(name="menu")


@router.message(CommandStart(deep_link=True))
@router.message(CommandStart())
async def start(message: Message, command: CommandObject | None = None) -> None:
    # Deep link payload is the referrer's code: t.me/<bot>?start=ABC12345
    referral = (command.args or "") if command else ""
    user, created = await services.get_or_create_user(message.from_user, referral)
    summary = await services.plan_summary(user)

    greeting = "Welcome" if created else "Welcome back"
    bonus = (
        f"\n\n🎁 You've got <b>{summary['credits']} free credits</b> to start — "
        "one credit unlocks one pick."
        if created
        else ""
    )

    await message.answer(
        f"<b>{greeting}!</b>\n\n"
        "AI-assisted football analysis: a statistical model prices every fixture, "
        "then writes up what's driving the number.\n\n"
        "Every pick ships with a <b>confidence rating</b>, and every settled pick "
        "goes into a public record you can check under <b>📊 Our Record</b>. "
        "No guarantees — just the maths, shown honestly."
        f"{bonus}",
        reply_markup=kb.MAIN_MENU,
    )


@router.message(F.text == kb.BTN_PLAN)
async def my_plan(message: Message) -> None:
    user, _ = await services.get_or_create_user(message.from_user)
    summary = await services.plan_summary(user)

    lines = [f"<b>Your plan — {summary['plan_name']}</b>", ""]
    if summary["unlimited"]:
        lines.append("Picks          Unlimited")
    else:
        lines.append(f"Credits        {summary['credits']}")
    lines.append(f"VIP slips      {'Yes' if summary['vip'] else 'No'}")
    if summary["expires_at"]:
        lines.append(f"Renews         {summary['expires_at']:%d %b %Y}")

    lines.append(f"Alerts         {'On' if user.notifications_enabled else 'Off'}")
    lines += ["", f"Your referral link:\nt.me/{await _bot_username(message)}?start={user.referral_code}"]

    await message.answer(
        "\n".join(lines),
        reply_markup=kb.notifications_keyboard(user.notifications_enabled),
    )


@router.callback_query(F.data.startswith("notify:"))
async def toggle_notifications(callback: CallbackQuery) -> None:
    user, _ = await services.get_or_create_user(callback.from_user)
    enabled = callback.data.split(":", 1)[1] == "on"
    await services.set_notifications(user, enabled)

    await callback.answer("Alerts on" if enabled else "Alerts off")
    await callback.message.edit_reply_markup(
        reply_markup=kb.notifications_keyboard(enabled)
    )


@router.message(F.text == kb.BTN_RECORD)
async def record(message: Message) -> None:
    stats = await services.record_summary()
    if not stats["settled"]:
        await message.answer("No settled picks yet — the record starts once results come in.")
        return

    roi = f"{stats['roi']:+.1f}%" if stats["roi"] is not None else "—"
    await message.answer(
        f"<b>Last {stats['days']} days</b>\n\n"
        f"Settled picks   {stats['settled']}\n"
        f"Won             {stats['won']}\n"
        f"Strike rate     {stats['win_rate']:.1f}%\n"
        f"ROI (1u level)  {roi}\n\n"
        "<i>Every published pick is counted here, winners and losers alike.</i>"
    )


@router.message(F.text == kb.BTN_SUPPORT)
async def support(message: Message) -> None:
    await message.answer(
        "<b>Support</b>\n\n"
        "Reply here with your question and we'll get back to you.\n\n"
        "Common ones:\n"
        "• Credits didn't arrive after payment — send your transaction hash.\n"
        "• A pick was graded wrong — send the fixture and we'll recheck it."
    )


async def _bot_username(message: Message) -> str:
    me = await message.bot.me()
    return me.username
