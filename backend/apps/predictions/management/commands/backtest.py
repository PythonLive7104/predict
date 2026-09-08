"""
Walk-forward backtest.

Replays settled fixtures in chronological order: for each one, prices it using
only ratings derived from *earlier* results, then grades the pick against what
actually happened. That ordering is the whole point — rating a match with
strengths that already contain its result is lookahead bias, and it produces a
backtest that looks brilliant and predicts nothing.

The output is the honest ceiling on what the engine can claim.
"""

from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.fixtures.models import Fixture, Team
from apps.predictions.engine import poisson, ratings
from apps.predictions.engine.pipeline import ENGINE_VERSION, candidates_from, expected_goals_for
from apps.predictions.models import Market, Outcome, Prediction, Tier
from apps.predictions.tasks import grade


class Command(BaseCommand):
    help = "Replay settled fixtures through the engine and report its real accuracy"

    def add_arguments(self, parser):
        parser.add_argument("--min-confidence", type=int, default=55)
        parser.add_argument(
            "--warmup", type=int, default=30,
            help="Fixtures used to build initial ratings before scoring begins.",
        )
        parser.add_argument("--persist", action="store_true",
                            help="Save graded predictions so the record page has data.")

    @transaction.atomic
    def handle(self, *args, **options):
        min_conf = options["min_confidence"]
        warmup = options["warmup"]

        fixtures = list(
            Fixture.objects.filter(status=Fixture.Status.FINISHED, home_goals__isnull=False)
            .select_related("home", "away", "league")
            .order_by("kickoff")
        )
        if len(fixtures) <= warmup:
            self.stderr.write(f"Need more than {warmup} settled fixtures, have {len(fixtures)}.")
            return

        # Reset ratings so the walk-forward starts from a clean slate.
        Team.objects.update(
            elo=1500.0,
            attack_strength=1.0, defence_strength=1.0,
            home_attack_strength=1.0, home_defence_strength=1.0,
            away_attack_strength=1.0, away_defence_strength=1.0,
        )
        Fixture.objects.update(elo_applied=False)

        tally = defaultdict(lambda: {"n": 0, "won": 0})
        scored = 0

        for index, fixture in enumerate(fixtures):
            # Drop the Team objects cached on this row by `select_related`. They
            # were loaded *before* the reset above, so without this the pricing
            # below reads whatever ratings happened to be in the database when
            # the list was built — on any re-run, the previous replay's
            # end-of-season values, which contain every result including this
            # one. That is total lookahead, and it survives every other guard in
            # this command because the numbers never come from a query at all.
            fixture.refresh_from_db()

            if index >= warmup:
                # Price it with what was known beforehand.
                for prediction in self._price(fixture, min_conf, options["persist"]):
                    outcome = grade(prediction)
                    if outcome in (Outcome.WON, Outcome.LOST):
                        row = tally[prediction.market]
                        row["n"] += 1
                        row["won"] += outcome == Outcome.WON
                        scored += 1

            # Only now may this result inform the ratings.
            ratings.apply_elo(fixture)
            if index % 10 == 0:
                # Bounded at this kickoff. Every fixture in the table is already
                # FINISHED during a replay, so an unbounded refresh would rate a
                # match using results that had not happened yet.
                ratings.update_strengths(fixture.league, as_of=fixture.kickoff)

        self._report(tally, scored, warmup, min_conf)

    def _price(self, fixture, min_conf, persist):
        lam_h, lam_a = expected_goals_for(fixture, as_of=fixture.kickoff)
        probs = poisson.market_probabilities(lam_h, lam_a)

        out = []
        for candidate in candidates_from(probs):
            if candidate.confidence < min_conf:
                continue
            prediction = Prediction(
                fixture=fixture, market=candidate.market, selection=candidate.selection,
                probability=candidate.probability, confidence=candidate.confidence,
                fair_odds=poisson.fair_odds(candidate.probability),
                engine_version=f"{ENGINE_VERSION}-backtest",
            )
            if persist:
                prediction.tier = Tier.FREE
                prediction.published_at = fixture.kickoff
                prediction.outcome = grade(prediction) or Outcome.PENDING
                prediction.settled_at = timezone.now()
                prediction.save()
            out.append(prediction)
        return out

    def _report(self, tally, scored, warmup, min_conf):
        labels = dict(Market.choices)
        self.stdout.write("")
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"Walk-forward backtest — {scored} graded picks "
                f"(warmup {warmup}, min confidence {min_conf}%)"
            )
        )
        self.stdout.write(f"  {'MARKET':<24}{'PICKS':>7}{'WON':>7}{'RATE':>8}")

        total_n = total_won = 0
        for market, row in sorted(tally.items(), key=lambda kv: -kv[1]["n"]):
            rate = row["won"] / row["n"] * 100 if row["n"] else 0
            total_n += row["n"]
            total_won += row["won"]
            self.stdout.write(
                f"  {labels.get(market, market):<24}{row['n']:>7}{row['won']:>7}{rate:>7.1f}%"
            )

        overall = total_won / total_n * 100 if total_n else 0
        self.stdout.write(f"  {'—' * 44}")
        self.stdout.write(
            self.style.SUCCESS(f"  {'ALL MARKETS':<24}{total_n:>7}{total_won:>7}{overall:>7.1f}%")
        )
        self.stdout.write("")
        self.stdout.write(
            "  Strike rate alone does not mean profit — a market with a high rate at\n"
            "  short prices can still lose money. Compare against the book price\n"
            "  before treating any of this as an edge."
        )

        # A result this good almost always means the fixtures were simulated from
        # the same Poisson process the model assumes, so it is scoring itself
        # against its own assumptions. Real 1X2 tops out around 55-60%.
        if overall >= 65:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    f"  ⚠  {overall:.1f}% overall is implausibly high for football.\n"
                    "     If these fixtures were seeded or simulated rather than pulled\n"
                    "     from API-Football, this number measures nothing — the model is\n"
                    "     being graded against data generated by its own assumptions.\n"
                    "     Treat only backtests over real historical fixtures as evidence."
                )
            )
