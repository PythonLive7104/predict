"""
Register (or inspect, or remove) the Telegram webhook.

Production runs the bot by webhook so updates land on Celery instead of holding
a long-polling process open — but Telegram only knows to call us once it has
been told to. `TELEGRAM_WEBHOOK_URL` in settings is a statement of intent;
nothing acts on it until this command runs. Deploying without it leaves a bot
that looks healthy and never receives a message.
"""

import asyncio

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.bot.dispatcher import get_bot


class Command(BaseCommand):
    help = "Register the Telegram webhook with the URL and secret in settings"

    def add_arguments(self, parser):
        parser.add_argument("--url", help="Override TELEGRAM_WEBHOOK_URL.")
        parser.add_argument(
            "--show", action="store_true",
            help="Print what Telegram currently has registered and exit.",
        )
        parser.add_argument(
            "--delete", action="store_true",
            help="Remove the webhook (use before falling back to runbot).",
        )
        parser.add_argument(
            "--drop-pending", action="store_true",
            help="Discard updates queued while the webhook was unset.",
        )

    def handle(self, *args, **options):
        asyncio.run(self._run(options))

    async def _run(self, options):
        bot = get_bot()
        try:
            if options["show"]:
                await self._show(bot)
            elif options["delete"]:
                await bot.delete_webhook(drop_pending_updates=options["drop_pending"])
                self.stdout.write(self.style.SUCCESS("Webhook deleted."))
            else:
                await self._register(bot, options)
        finally:
            # aiogram holds an aiohttp session open; without this the command
            # exits on a warning about an unclosed connector.
            await bot.session.close()

    async def _show(self, bot):
        info = await bot.get_webhook_info()
        self.stdout.write(f"  url            : {info.url or '(none)'}")
        self.stdout.write(f"  pending updates: {info.pending_update_count}")
        self.stdout.write(f"  custom cert    : {info.has_custom_certificate}")
        if info.last_error_message:
            # This is where a bad TLS chain or a 403 from our own view shows up,
            # and it is the single most useful line when a webhook "just doesn't
            # work" — Telegram reports the failure here, not to us.
            self.stdout.write(
                self.style.WARNING(
                    f"  last error     : {info.last_error_message} ({info.last_error_date})"
                )
            )

    async def _register(self, bot, options):
        url = options["url"] or settings.TELEGRAM_WEBHOOK_URL
        if not url:
            raise CommandError(
                "No webhook URL. Set TELEGRAM_WEBHOOK_URL or pass --url. "
                "It must be HTTPS with a publicly valid certificate — Telegram "
                "will not call a self-signed or plain-HTTP endpoint."
            )
        if not url.startswith("https://"):
            raise CommandError(f"Webhook URL must be HTTPS, got {url!r}")
        if not settings.TELEGRAM_WEBHOOK_SECRET:
            raise CommandError(
                "TELEGRAM_WEBHOOK_SECRET is not set. The webhook view rejects "
                "every call without it, so registering now would give you a bot "
                "that receives updates and drops all of them."
            )

        await bot.set_webhook(
            url=url,
            secret_token=settings.TELEGRAM_WEBHOOK_SECRET,
            drop_pending_updates=options["drop_pending"],
        )
        self.stdout.write(self.style.SUCCESS(f"Webhook registered: {url}"))
        await self._show(bot)
