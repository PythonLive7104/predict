"""
Run the whole daily chain by hand, in order.

The scheduler does this every day — odds at 08:00, generation at 08:30,
publishing at 09:30 — but after changing the league list, restoring a database
or a first deploy you need it now rather than tomorrow. Each step feeds the next
and skipping one leaves the rest quietly wrong:

    leagues    nothing else creates them; sync_fixtures only walks rows that exist
    backfill   ratings built with no history leave every team at the default,
               and the engine then prices a coin flip with home advantage
    ratings    what poisson.py actually reads
    fixtures   today's and tomorrow's matches
    odds       without these, market_odds and edge are null and the odds
               builder has nothing to combine
    generate   prices every upcoming fixture
    publish    decides what ships free, what is VIP, and builds the slips

Every step is an idempotent upsert, so re-running is safe.
"""

import time

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Sync, price and publish the current slate end to end"

    def add_arguments(self, parser):
        parser.add_argument("--backfill", type=int, default=0,
                            help="Seasons of history to pull first. Slow, and only "
                                 "needed after adding leagues (try 3).")
        parser.add_argument("--skip-leagues", action="store_true",
                            help="Leave the League rows alone.")
        parser.add_argument("--days", type=int, default=None,
                            help="How far ahead to price. Defaults to "
                                 "PREDICTION_HORIZON_DAYS (7), which is what "
                                 "'this weekend' needs from a Monday.")

    def handle(self, *args, **options):
        from apps.fixtures.tasks import (
            leagues_to_sync, sync_fixtures, sync_leagues, sync_odds_for_upcoming,
        )
        from apps.predictions.tasks import (
            generate_daily_predictions, publish_and_build_slips, refresh_ratings,
        )

        started = time.time()

        if not options["skip_leagues"]:
            self._step("Leagues", lambda: f"{sync_leagues(force=True)} synced")

        self.stdout.write(f"  configured: {leagues_to_sync().count()} active league(s)")

        if options["backfill"]:
            self._step(
                f"History ({options['backfill']} seasons)",
                lambda: self._backfill(options["backfill"]),
            )

        self._step("Ratings", lambda: str(refresh_ratings()))
        from django.conf import settings

        days = options["days"] or settings.PREDICTION_HORIZON_DAYS
        # One day past the pricing horizon: a fixture cannot be priced before it
        # has been synced.
        self._step("Fixtures", lambda: f"{sync_fixtures(days_ahead=days + 1)} synced")
        self._step("Odds and injuries", lambda: str(sync_odds_for_upcoming()))
        self._step("Predictions", lambda: f"{generate_daily_predictions(days)} priced")
        self._step("Publishing", lambda: str(publish_and_build_slips(days_ahead=days)))

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Slate refreshed in {time.time() - started:.0f}s."
        ))
        self._summary()

    def _step(self, label, run):
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"==> {label}"))
        try:
            self.stdout.write(f"  {run()}")
        except Exception as exc:
            # One failing step must not hide the others: a league with no odds
            # published yet is normal, and the steps after it still do useful work.
            self.stdout.write(self.style.ERROR(f"  failed: {exc}"))

    def _backfill(self, seasons: int) -> str:
        from django.core.management import call_command

        call_command("backfill", "--seasons", str(seasons))
        return "done"

    def _summary(self):
        from django.utils import timezone

        from apps.fixtures.models import Fixture, Odds
        from apps.predictions.models import Prediction

        today = timezone.now().date()
        published = Prediction.objects.filter(
            published_at__isnull=False, fixture__kickoff__date__gte=today
        )
        self.stdout.write("")
        self.stdout.write(f"  Fixtures ahead   {Fixture.objects.filter(kickoff__date__gte=today).count()}")
        self.stdout.write(f"  Odds rows        {Odds.objects.count()}")
        self.stdout.write(f"  Published picks  {published.count()}")
        self.stdout.write(f"  …with a price    {published.filter(market_odds__isnull=False).count()}")
        if not published.filter(market_odds__isnull=False).exists():
            self.stdout.write(self.style.WARNING(
                "\n  No priced published picks — Build Odds will report no_prices.\n"
                "  Usually means bookmakers have not posted markets yet; try again\n"
                "  nearer kick-off."
            ))
