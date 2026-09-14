"""
Pull historical seasons so the ratings have something real to learn from.

Without this the engine has fixtures but no past: every team sits at the default
rating, and the model prices a coin flip with home advantage dressed up as a
prediction. It is also the only way the backtest can mean anything — replaying a
simulated season grades the model against its own assumptions.

Cheap: one request per league-season, because a whole season arrives in a single
call. Three seasons across seven leagues is 21 requests.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_datetime

from apps.fixtures.models import Fixture, League
from apps.fixtures.tasks import STATUS_MAP, _upsert_team
from apps.fixtures.providers.api_football import ApiFootballClient, ApiFootballError


class Command(BaseCommand):
    help = "Backfill past seasons of fixtures for the configured leagues"

    def add_arguments(self, parser):
        parser.add_argument("--seasons", type=int, default=3,
                            help="How many past seasons to pull (default 3).")
        parser.add_argument("--league", type=int, action="append",
                            help="Limit to these league api-ids. Defaults to every synced league.")

    def handle(self, *args, **options):
        leagues = League.objects.filter(is_active=True)
        if options["league"]:
            leagues = leagues.filter(api_id__in=options["league"])
        leagues = list(leagues)

        if not leagues:
            raise CommandError(
                "No leagues to backfill. Run `manage.py sync_leagues` first — "
                "nothing else creates them."
            )

        client = ApiFootballClient()
        total_requests = total_rows = 0

        for league in leagues:
            # Seasons run backwards from the current one. A league's `season` is
            # the year its current campaign started, so the previous seasons are
            # simply that minus one, two, three.
            for offset in range(1, options["seasons"] + 1):
                season = league.season - offset
                self.stdout.write(f"  {league.name} {season} … ", ending="")
                self.stdout.flush()
                try:
                    nodes = client.fixtures_by_season(league.api_id, season)
                except ApiFootballError as exc:
                    # A season the plan cannot see fails loudly per season rather
                    # than aborting the run — the other leagues are still worth it.
                    self.stdout.write(self.style.WARNING(f"skipped ({exc})"))
                    total_requests += 1
                    continue

                total_requests += 1
                rows = self._store(league, nodes)
                total_rows += rows
                self.stdout.write(self.style.SUCCESS(f"{rows} fixtures"))

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Backfilled {total_rows} fixtures in {total_requests} requests."
        ))
        self.stdout.write(
            "Next: manage.py shell -c "
            "\"from apps.predictions.tasks import refresh_ratings; print(refresh_ratings())\""
        )

    def _store(self, league: League, nodes: list) -> int:
        count = 0
        for node in nodes:
            fx = node["fixture"]
            goals = node.get("goals") or {}
            halftime = (node.get("score") or {}).get("halftime") or {}
            status = STATUS_MAP.get(fx["status"]["short"], Fixture.Status.SCHEDULED)

            _, created = Fixture.objects.update_or_create(
                api_id=fx["id"],
                defaults={
                    "league": league,
                    "home": _upsert_team(node["teams"]["home"]),
                    "away": _upsert_team(node["teams"]["away"]),
                    "kickoff": parse_datetime(fx["date"]),
                    "round": (node.get("league") or {}).get("round") or "",
                    "venue": (fx.get("venue") or {}).get("name") or "",
                    "status": status,
                    "home_goals": goals.get("home"),
                    "away_goals": goals.get("away"),
                    "home_goals_ht": halftime.get("home"),
                    "away_goals_ht": halftime.get("away"),
                    "raw": node,
                },
            )
            count += 1
        return count
