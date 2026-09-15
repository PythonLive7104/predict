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

        # `staked`/`returned` are only accumulated for picks that had a real
        # historical price. Strike rate decides nothing on its own: Double Chance
        # wins three times in four and still loses money at 1.15, because it
        # breaks even at 1.33.
        tally = defaultdict(lambda: {"n": 0, "won": 0, "priced": 0,
                                     "staked": 0.0, "returned": 0.0})
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

                        price = self._best_price(prediction)
                        if price:
                            row["priced"] += 1
                            row["staked"] += 1.0
                            if outcome == Outcome.WON:
                                row["returned"] += price

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

    @staticmethod
    def _best_price(prediction) -> float | None:
        """
        Best available historical price for this selection.

        Named apart from `_price`, which prices a *fixture* into predictions —
        one is the model's job, the other the bookmaker's.

        Best rather than average: a bettor shops for the best line, and grading
        against a worse one understates a real strategy. Returns None when the
        fixture has no stored odds — those picks are counted in the strike rate
        but excluded from ROI, because a guessed price would make the headline
        number fiction.
        """
        prices = [
            float(o.price)
            for o in prediction.fixture.odds.all()
            if o.market == prediction.market and o.selection == prediction.selection
        ]
        return max(prices) if prices else None

    def _report(self, tally, scored, warmup, min_conf):
        labels = dict(Market.choices)
        self.stdout.write("")
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"Walk-forward backtest — {scored} graded picks "
                f"(warmup {warmup}, min confidence {min_conf}%)"
            )
        )
        self.stdout.write(
            f"  {'MARKET':<24}{'PICKS':>7}{'WON':>7}{'RATE':>8}{'PRICED':>8}{'ROI':>9}"
        )

        total_n = total_won = 0
        for market, row in sorted(tally.items(), key=lambda kv: -kv[1]["n"]):
            rate = row["won"] / row["n"] * 100 if row["n"] else 0
            total_n += row["n"]
            total_won += row["won"]
            roi = (
                f"{(row['returned'] - row['staked']) / row['staked'] * 100:>+8.1f}%"
                if row["staked"] else f"{'—':>9}"
            )
            self.stdout.write(
                f"  {labels.get(market, market):<24}{row['n']:>7}{row['won']:>7}"
                f"{rate:>7.1f}%{row['priced']:>8}{roi}"
            )

        staked = sum(r["staked"] for r in tally.values())
        returned = sum(r["returned"] for r in tally.values())
        priced = sum(r["priced"] for r in tally.values())
        overall = total_won / total_n * 100 if total_n else 0
        self.stdout.write(f"  {'—' * 44}")
        overall_roi = (
            f"{(returned - staked) / staked * 100:>+8.1f}%" if staked else f"{'—':>9}"
        )
        self.stdout.write(self.style.SUCCESS(
            f"  {'ALL MARKETS':<24}{total_n:>7}{total_won:>7}{overall:>7.1f}%"
            f"{priced:>8}{overall_roi}"
        ))
        self.stdout.write("")
        if not staked:
            self.stdout.write(self.style.WARNING(
                "  No ROI: none of these fixtures has stored odds, so profitability\n"
                "  is unmeasured. Strike rate alone decides nothing — a market can\n"
                "  win three times in four and still lose money at short prices.\n"
                "  Fetch prices first:  manage.py backfill_odds --sample 20"
            ))
        else:
            covered = priced / total_n * 100 if total_n else 0
            self.stdout.write(
                f"  ROI is level stakes at the best available price, over the\n"
                f"  {covered:.0f}% of picks that have one. A market can win most of\n"
                f"  the time and still lose money: break-even at 75% is 1.33, and\n"
                f"  double chance rarely pays that."
            )
            if covered < 50:
                self.stdout.write(self.style.WARNING(
                    f"\n  Only {covered:.0f}% of picks are priced, so this ROI describes\n"
                    "  that subset rather than the strategy as a whole."
                ))

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
