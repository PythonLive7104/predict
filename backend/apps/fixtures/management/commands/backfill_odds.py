"""
Fetch historical odds so the backtest can report profit, not just strike rate.

Strike rate alone decides nothing: the backtest's own Double Chance line wins
75% of the time and still loses money, because those picks price around 1.15 and
break even at 1.33. Only real historical prices turn that from an estimate into
a measurement.

Costs one request per fixture, which is the whole problem — several thousand
fixtures is close to a day's PRO quota. So:

  * `--sample N` tries a handful first and reports how many actually carry odds.
    API-Football's coverage of *past* odds is thinner than for upcoming games,
    and finding that out after spending 6,000 requests would be expensive.
  * `--limit N` bounds a real run.
  * Fixtures that already have odds are skipped, so it resumes where it stopped.
"""

import time

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Q

from apps.fixtures.models import Fixture
from apps.fixtures.providers.api_football import ApiFootballClient, ApiFootballError
from apps.fixtures.tasks import store_odds


class Command(BaseCommand):
    help = "Backfill bookmaker odds for finished fixtures so the backtest can price them"

    def add_arguments(self, parser):
        parser.add_argument("--sample", type=int, default=0,
                            help="Try this many fixtures and report coverage, then stop.")
        parser.add_argument("--limit", type=int, default=1000,
                            help="Maximum fixtures to fetch in this run (default 1000).")
        parser.add_argument("--league", type=int, action="append",
                            help="Restrict to these league api-ids.")
        parser.add_argument("--pause", type=float, default=0.0,
                            help="Seconds between requests, if the plan rate-limits.")

    def handle(self, *args, **options):
        pending = self._pending(options["league"])
        total = pending.count()

        if not total:
            self.stdout.write(self.style.SUCCESS(
                "Every settled fixture already has odds. Nothing to do."
            ))
            return

        if options["sample"]:
            self._sample(pending, options["sample"], options["pause"], total)
            return

        limit = min(options["limit"], total)
        self.stdout.write(
            f"{total} settled fixtures without odds. Fetching {limit} "
            f"({limit} requests).\n"
        )
        self._fetch(pending[:limit], options["pause"], announce_every=50)

    def _pending(self, league_ids):
        """
        Settled fixtures carrying no odds yet.

        `Count` with a filter rather than `odds__isnull=True`: the latter would
        also match fixtures that have odds for some other market and quietly
        re-fetch them on every run.
        """
        qs = (
            Fixture.objects.filter(
                status=Fixture.Status.FINISHED, home_goals__isnull=False
            )
            .annotate(n_odds=Count("odds"))
            .filter(n_odds=0)
            .order_by("-kickoff")
        )
        return qs.filter(league__api_id__in=league_ids) if league_ids else qs

    def _sample(self, pending, size, pause, total):
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Sampling {size} of {total} fixtures to check coverage"
        ))
        self.stdout.write(
            "  Historical odds coverage is thinner than for upcoming games.\n"
            "  Worth knowing before committing the full run.\n"
        )
        hits, rows = self._fetch(pending[:size], pause, announce_every=5)

        self.stdout.write("")
        pct = hits / size * 100 if size else 0
        self.stdout.write(f"  {hits}/{size} fixtures returned odds ({pct:.0f}%)")
        self.stdout.write(f"  {rows} odds rows written")
        self.stdout.write("")

        if pct >= 50:
            self.stdout.write(self.style.SUCCESS(
                f"  Coverage looks usable. The full run is ~{total} requests:\n"
                f"    manage.py backfill_odds --limit {total}"
            ))
        elif pct > 0:
            self.stdout.write(self.style.WARNING(
                f"  Patchy coverage. A backtest priced on {pct:.0f}% of fixtures\n"
                "  measures the subset that happens to have odds, which is not\n"
                "  the same thing as the model's ROI. Worth considering\n"
                "  football-data.co.uk's free closing-odds CSVs instead."
            ))
        else:
            self.stdout.write(self.style.ERROR(
                "  No historical odds available on this plan. Spending the full\n"
                "  run would return nothing — use football-data.co.uk's free\n"
                "  closing-odds archive instead."
            ))

    def _fetch(self, fixtures, pause, announce_every):
        client = ApiFootballClient()
        hits = rows = 0
        started = time.time()

        for i, fixture in enumerate(fixtures, start=1):
            try:
                nodes = client.odds(fixture.api_id)
            except ApiFootballError as exc:
                # Quota exhaustion arrives here too, and continuing would burn
                # the rest of the run against a wall.
                if "limit" in str(exc).lower() or "quota" in str(exc).lower():
                    self.stdout.write(self.style.ERROR(f"\n  Quota reached: {exc}"))
                    break
                continue

            written = store_odds(fixture, nodes)
            if written:
                hits += 1
                rows += written

            if announce_every and i % announce_every == 0:
                rate = i / max(time.time() - started, 1)
                self.stdout.write(
                    f"  {i} fetched · {hits} with odds · {rows} rows · {rate:.1f}/s"
                )
            if pause:
                time.sleep(pause)

        return hits, rows
