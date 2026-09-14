"""
Long-polling runner for local dev.

Production uses the webhook (config/urls.py -> hooks/telegram/) so updates land
on Celery instead of holding a process open; polling is only for development,
where no public URL exists.
"""

import asyncio

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Run the Telegram bot with long polling (development only)"

    def handle(self, *args, **options):
        from apps.bot.dispatcher import bot_session, get_dispatcher

        dispatcher = get_dispatcher()
        self.stdout.write(self.style.SUCCESS("Bot polling — Ctrl-C to stop"))

        async def run():
            # One long-lived loop here, unlike the Celery path — but the session
            # is still opened and closed inside it, so the Bot never outlives
            # the loop that owns its connections.
            async with bot_session() as bot:
                # Deleting the webhook is how polling and webhooks are kept from
                # fighting — but on a host that runs by webhook it silently
                # unregisters the bot, and Telegram then queues every update
                # with nowhere to send it. Say so loudly; the symptom otherwise
                # is a bot that answers nothing with no error anywhere.
                existing = await bot.get_webhook_info()
                if existing.url:
                    self.stdout.write(self.style.WARNING(
                        f"\n  Unregistering the webhook at {existing.url}\n"
                        "  This bot will stop receiving updates when polling ends.\n"
                        "  Restore it with:  manage.py set_webhook\n"
                    ))
                # Drop anything queued while the bot was down: replaying stale
                # commands on restart confuses users more than losing them.
                await bot.delete_webhook(drop_pending_updates=True)
                await dispatcher.start_polling(bot)

        try:
            asyncio.run(run())
        except KeyboardInterrupt:
            self.stdout.write("Stopped")
