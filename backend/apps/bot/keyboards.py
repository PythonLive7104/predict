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
BTN_VIP = "🔒 VIP Slips"
BTN_RECORD = "📊 Our Record"
BTN_PLAN = "👤 My Plan"
BTN_PURCHASE = "💳 Purchase"
BTN_SUPPORT = "💬 Support"

MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_TODAY), KeyboardButton(text=BTN_VIP)],
        [KeyboardButton(text=BTN_RECORD), KeyboardButton(text=BTN_PLAN)],
        [KeyboardButton(text=BTN_PURCHASE), KeyboardButton(text=BTN_SUPPORT)],
    ],
    resize_keyboard=True,
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
