"""
Persistent reply keyboard + inline keyboards.

The reply keyboard is always visible under the input box (the pattern in the
reference bots): it makes the whole product reachable without the user ever
learning a slash command.
"""

from decimal import Decimal

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

BTN_TODAY = "⚽ Today's Picks"
BTN_ODDS = "🎯 Build Odds"
BTN_VIP = "🔒 VIP Slips"
BTN_RECORD = "📊 Our Record"
BTN_PLAN = "👤 My Plan"
BTN_PURCHASE = "💳 Purchase"
BTN_SUPPORT = "💬 Support"

MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_TODAY), KeyboardButton(text=BTN_ODDS)],
        [KeyboardButton(text=BTN_VIP), KeyboardButton(text=BTN_RECORD)],
        [KeyboardButton(text=BTN_PLAN), KeyboardButton(text=BTN_PURCHASE)],
        [KeyboardButton(text=BTN_SUPPORT)],
    ],
    resize_keyboard=True,
)

# Named spans rather than a date picker: these are the three requests people
# actually make, and every one of them is reachable in a single tap.
SPANS = (("today", "Today"), ("tomorrow", "Tomorrow"), ("weekend", "Fri–Sun"))

# Presets in the vernacular customers use — "3 odds" means a slip paying about
# 3x, not a slip with three legs.
ODDS_TARGETS = (
    ("2", "2 odds", 1.8, 2.5),
    ("3", "3 odds", 2.5, 3.5),
    ("5", "5 odds", 3.5, 6.0),
    ("10", "10 odds", 6.0, 12.0),
)


def money(amount: Decimal) -> str:
    """$25 rather than $25.00, but $9.99 keeps its cents."""
    normalised = amount.normalize()
    return f"{normalised:f}" if normalised == normalised.to_integral() else f"{amount:.2f}"


def plans_keyboard(plans) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"${money(plan.price_usd)} · {plan.name}",
                    callback_data=f"buy:{plan.code}",
                )
            ]
            for plan in plans
        ]
    )


def notifications_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔕 Turn off alerts" if enabled else "🔔 Turn on alerts",
                    callback_data=f"notify:{'off' if enabled else 'on'}",
                )
            ]
        ]
    )


def unlock_keyboard(prediction_id: int, credits: int = 1) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🔓 Unlock ({credits} credit)", callback_data=f"unlock:{prediction_id}"
                )
            ],
            [InlineKeyboardButton(text="💳 Go unlimited", callback_data="menu:purchase")],
        ]
    )


def review_keyboard(payment_id: int) -> InlineKeyboardMarkup:
    """Admin-only. Sent alongside a submitted hash so approval is one tap."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Approve", callback_data=f"pay:ok:{payment_id}"),
                InlineKeyboardButton(text="❌ Reject", callback_data=f"pay:no:{payment_id}"),
            ]
        ]
    )


def span_keyboard(active: str, prefix: str = "picks") -> InlineKeyboardMarkup:
    """Day switcher. The active span is marked so a tap that changes nothing
    still shows the user where they are."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text=f"· {label} ·" if key == active else label,
                callback_data=f"{prefix}:{key}",
            )
            for key, label in SPANS
        ]]
    )


def odds_keyboard(active_span: str = "today", active_target: str = "") -> InlineKeyboardMarkup:
    targets = [
        InlineKeyboardButton(
            text=f"· {label} ·" if key == active_target else label,
            callback_data=f"odds:{key}:{active_span}",
        )
        for key, label, _lo, _hi in ODDS_TARGETS
    ]
    days = [
        InlineKeyboardButton(
            text=f"· {label} ·" if key == active_span else label,
            callback_data=f"odds:{active_target or '3'}:{key}",
        )
        for key, label in SPANS
    ]
    # Two rows of two keeps the target buttons legible on a narrow phone.
    return InlineKeyboardMarkup(inline_keyboard=[targets[:2], targets[2:], days])
