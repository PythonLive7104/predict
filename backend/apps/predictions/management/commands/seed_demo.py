"""
Seed a demo dataset: a simulated season, derived ratings, and today's slate.

For local development and screenshots only. The team strengths are NOT set by
hand — a season of results is simulated and the real ratings engine derives
attack/defence and Elo from them, so what you see is genuine pipeline output.

WARNING: the simulated results come from a Poisson process, which is exactly what
the model assumes. Any accuracy measured against this data is meaningless (see
the warning in `manage.py backtest`). Use it to exercise the UI, never to judge
the engine.
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Wipe fixture data and seed a demo season + today's slate (DEV ONLY)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes", action="store_true",
            help="Required: confirms deleting all League/Team/Fixture rows.",
        )

    def handle(self, *args, **options):
        if not options["yes"]:
            self.stderr.write(
                "This deletes every League, Team and Fixture row. Re-run with --yes."
            )
            return

        """
        Seed a realistic slate so the bot has something real to serve.

        Team strengths are NOT hand-set — a mini-season of results is generated from
        hidden 'true' strengths and then the real ratings engine derives attack/defence
        and Elo from those results. So what the bot shows is the actual pipeline output.
        """
        import math
        import random


        def _p(lam):
            """Poisson sample without numpy — Knuth."""
            l, k, prob = math.exp(-lam), 0, 1.0
            while True:
                k += 1
                prob *= random.random()
                if prob <= l:
                    return k - 1
        from datetime import timedelta
        from decimal import Decimal

        from django.utils import timezone

        from apps.fixtures.models import Fixture, League, Odds, Team
        from apps.predictions.engine import ratings
        from apps.predictions.tasks import generate_daily_predictions
        from apps.predictions.publishing import build_slips, publish_daily

        random.seed(7)

        Fixture.objects.all().delete(); Team.objects.all().delete(); League.objects.all().delete()

        epl = League.objects.create(api_id=39, name="Premier League", country="England", season=2026)

        # (name, hidden attack, hidden defence) — only used to simulate past results.
        SQUADS = [
            ("Man City", 2.15, 0.85), ("Arsenal", 1.95, 0.80), ("Liverpool", 2.00, 1.00),
            ("Chelsea", 1.60, 1.10), ("Newcastle", 1.55, 1.15), ("Brighton", 1.45, 1.25),
            ("Aston Villa", 1.50, 1.20), ("West Ham", 1.20, 1.45), ("Everton", 1.00, 1.35),
            ("Burnley", 0.85, 1.75),
        ]
        teams = {}
        for i, (name, atk, dfc) in enumerate(SQUADS):
            teams[name] = (Team.objects.create(api_id=100 + i, name=name), atk, dfc)

        # --- a mini-season of settled results -------------------------------------
        fid = 1000
        names = list(teams)
        for round_no in range(2):
            for i, home_name in enumerate(names):
                for away_name in names[i + 1:]:
                    h, h_atk, h_def = teams[home_name]
                    a, a_atk, a_def = teams[away_name]
                    if round_no:
                        h, a = a, h
                        h_atk, h_def, a_atk, a_def = a_atk, a_def, h_atk, h_def
                    hg = min(_p(h_atk * a_def * 0.95 * 1.15), 7)
                    ag = min(_p(a_atk * h_def * 0.95), 7)
                    fid += 1
                    Fixture.objects.create(
                        api_id=fid, league=epl, home=h, away=a,
                        kickoff=timezone.now() - timedelta(days=random.randint(3, 110)),
                        status=Fixture.Status.FINISHED, home_goals=hg, away_goals=ag,
                    )

        stats = ratings.refresh_all()
        self.stdout.write(f"ratings: applied {stats['elo_applied']} results, rated {stats['teams_updated']} teams\n")
        self.stdout.write(f"{'TEAM':<14}{'ELO':>7}{'ATT':>7}{'DEF':>7}")
        for t in Team.objects.order_by("-elo"):
            self.stdout.write(f"{t.name:<14}{t.elo:>7.0f}{t.attack_strength:>7.2f}{t.defence_strength:>7.2f}")

        # --- today's slate --------------------------------------------------------
        TODAY = [
            ("Man City", "Burnley", {"1x2": ("home", "1.14"), "ou_2_5": ("over", "1.44"), "btts": ("yes", "2.30")}),
            ("Arsenal", "Everton", {"1x2": ("home", "1.40"), "ou_2_5": ("over", "1.85"), "btts": ("yes", "2.05")}),
            ("Liverpool", "West Ham", {"1x2": ("home", "1.36"), "ou_2_5": ("over", "1.62"), "btts": ("yes", "1.75")}),
            ("Brighton", "Chelsea", {"1x2": ("away", "2.20"), "ou_2_5": ("over", "1.80"), "btts": ("yes", "1.62")}),
            ("Newcastle", "Aston Villa", {"1x2": ("home", "2.05"), "ou_2_5": ("over", "1.90"), "btts": ("yes", "1.70")}),
        ]
        noon = timezone.now() + timedelta(hours=3)
        for i, (home, away, markets) in enumerate(TODAY):
            fx = Fixture.objects.create(
                api_id=9000 + i, league=epl, home=teams[home][0], away=teams[away][0],
                kickoff=noon + timedelta(minutes=30 * i), venue="Home ground",
                status=Fixture.Status.SCHEDULED,
            )
            for market, (selection, price) in markets.items():
                Odds.objects.create(fixture=fx, bookmaker="Bet365", market=market,
                                    selection=selection, price=Decimal(price))
            # A plausible double-chance price so slips have safe legs to pick from.
            Odds.objects.create(fixture=fx, bookmaker="Bet365", market="dc",
                                selection="home_draw", price=Decimal("1.12"))

        generated = generate_daily_predictions(days_ahead=1)
        published = publish_daily()
        slips = build_slips()
        self.stdout.write(f"\ngenerated {generated} predictions -> {published} -> slips: {[s.title for s in slips]}")


        self.stdout.write(self.style.SUCCESS("Demo data seeded."))
