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
        from apps.bot.dispatcher import get_bot, get_dispatcher

        bot, dispatcher = get_bot(), get_dispatcher()
        self.stdout.write(self.style.SUCCESS("Bot polling — Ctrl-C to stop"))

        async def run():
            # Drop anything queued while the bot was down: replaying stale
            # commands on restart confuses users more than losing them.
            await bot.delete_webhook(drop_pending_updates=True)
            await dispatcher.start_polling(bot)

        try:
            asyncio.run(run())
        except KeyboardInterrupt:
            self.stdout.write("Stopped")
