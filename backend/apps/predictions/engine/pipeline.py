"""
Orchestration: fixture in, Prediction rows out.

Deliberately linear and side-effect-light — build the pack, run the numbers,
optionally decorate with prose, persist. The narrative layer is best-effort: if
Claude is down or rate-limited the pick still ships, just without the read.
"""

import logging
from dataclasses import dataclass

from django.conf import settings
from django.db import transaction

from apps.fixtures.models import Fixture
from apps.predictions.models import Market, Prediction, selection_label_for

from . import llm, poisson, ratings

logger = logging.getLogger(__name__)

ENGINE_VERSION = "0.2.0"

# Blend weight for the Elo prior against the Poisson home probability. Early in a
# season the Poisson strengths are noisy, so Elo carries more of the call.
ELO_BLEND = 0.30


def expected_goals_for(fixture: Fixture, as_of=None) -> tuple[float, float]:
    """
    The fixture's Poisson lambdas, from the home side's *home* strengths and the
    away side's *away* strengths.

    The backtest imports this rather than reimplementing it, so the walk-forward
    number measures the same code that prices a live slate. `as_of` bounds the
    league averages to what was known at kickoff; production leaves it None.
    """
    home_avg, away_avg = ratings.league_venue_averages(fixture.league, as_of)
    return poisson.expected_goals(
        fixture.home.home_attack_strength,
        fixture.home.home_defence_strength,
        fixture.away.away_attack_strength,
        fixture.away.away_defence_strength,
        league_home_avg=home_avg,
        league_away_avg=away_avg,
    )


@dataclass
class Candidate:
    market: str
    selection: str
    probability: float

    @property
    def confidence(self) -> int:
        return int(round(self.probability * 100))


def build_stat_pack(fixture: Fixture) -> dict:
    """
    Freeze everything the engine is allowed to reason over. Whatever isn't in
    here didn't influence the pick — which is what makes the record auditable.
    """
    home, away = fixture.home, fixture.away
    league_home_avg, league_away_avg = ratings.league_venue_averages(fixture.league)
    return {
        "fixture": {
            "league": fixture.league.name,
            "kickoff": fixture.kickoff.isoformat(),
            "round": fixture.round,
            "venue": fixture.venue,
        },
        # Both the overall and the venue ratings are frozen, along with the
        # league averages they were normalised against. Without those averages a
        # stored strength of 1.12 is uninterpretable later — the baseline moves
        # as a season goes on, so the ratio alone cannot be re-derived.
        "league_averages": {"home": league_home_avg, "away": league_away_avg},
        "home": {
            "name": home.name,
            "elo": home.elo,
            "attack": home.attack_strength,
            "defence": home.defence_strength,
            # The pair actually used to price this fixture.
            "home_attack": home.home_attack_strength,
            "home_defence": home.home_defence_strength,
        },
        "away": {
            "name": away.name,
            "elo": away.elo,
            "attack": away.attack_strength,
            "defence": away.defence_strength,
            "away_attack": away.away_attack_strength,
            "away_defence": away.away_defence_strength,
        },
        "injuries": [
            {"team": i.team.name, "player": i.player_name, "type": i.type, "reason": i.reason}
            for i in fixture.injuries.select_related("team")
        ],
        "market_odds": [
            {"book": o.bookmaker, "market": o.market, "selection": o.selection, "price": float(o.price)}
            for o in fixture.odds.all()
        ],
    }


def candidates_from(probs: poisson.MarketProbabilities) -> list[Candidate]:
    """Best selection per market. One pick per market, never a spread of hedges."""
    out = []

    best_1x2 = max(
        [("home", probs.home), ("draw", probs.draw), ("away", probs.away)], key=lambda kv: kv[1]
    )
    out.append(Candidate(Market.MATCH_RESULT, *best_1x2))

    over_under = ("over", probs.over_2_5) if probs.over_2_5 >= probs.under_2_5 else ("under", probs.under_2_5)
    out.append(Candidate(Market.OVER_UNDER_25, *over_under))

    btts = ("yes", probs.btts_yes) if probs.btts_yes >= probs.btts_no else ("no", probs.btts_no)
    out.append(Candidate(Market.BTTS, *btts))

    # Double chance is the safety market: back the stronger side *and* the draw.
    #
    # Deliberately never "home or away" (the 12 market). Mechanically that pair
    # wins whenever the draw is the least likely outcome, which for a heavy
    # favourite it often is — but 12 is a bet *against a draw*, not a safer
    # version of backing the favourite. Offering it as the safety pick reads as
    # nonsense ("Man City or Burnley") and is a different product entirely.
    if probs.home >= probs.away:
        dc = ("home_draw", probs.home + probs.draw)
    else:
        dc = ("draw_away", probs.draw + probs.away)
    out.append(Candidate(Market.DOUBLE_CHANCE, *dc))

    return out


def _best_market_odds(fixture: Fixture, market: str, selection: str) -> float | None:
    prices = [
        float(o.price)
        for o in fixture.odds.all()
        if o.market == market and o.selection == selection
    ]
    return max(prices) if prices else None


@transaction.atomic
def generate_for_fixture(fixture: Fixture, with_rationale: bool = True) -> list[Prediction]:
    """Idempotent per (fixture, market, engine_version) — safe to re-run."""
    pack = build_stat_pack(fixture)

    lam_home, lam_away = expected_goals_for(fixture)
    probs = poisson.market_probabilities(lam_home, lam_away)

    # Blend the Elo prior into the home/away split; the draw is left alone since
    # Elo says nothing useful about it.
    elo_home = poisson.elo_win_probability(fixture.home.elo, fixture.away.elo)
    blended_home = (1 - ELO_BLEND) * probs.home + ELO_BLEND * elo_home * (1 - probs.draw)
    probs.home = blended_home
    probs.away = max(1 - probs.draw - blended_home, 0.0)

    pack["model_output"] = probs.as_dict()

    created = []
    for cand in candidates_from(probs):
        market_odds = _best_market_odds(fixture, cand.market, cand.selection)
        fair = poisson.fair_odds(cand.probability)

        defaults = {
            "selection": cand.selection,
            "probability": cand.probability,
            "confidence": cand.confidence,
            "fair_odds": fair,
            "market_odds": market_odds,
            "edge": poisson.edge(market_odds, cand.probability) if market_odds else None,
            "stat_snapshot": pack,
        }

        if with_rationale and cand.confidence >= settings.MIN_PUBLISH_CONFIDENCE:
            try:
                read = llm.write_rationale(
                    pack, probs.as_dict(), cand.selection, cand.market,
                    selection_label_for(
                        cand.market, cand.selection,
                        fixture.home.name, fixture.away.name,
                    ),
                    Market(cand.market).label,
                )
                defaults["rationale"] = read["rationale"]
                defaults["stat_snapshot"] = pack | {"llm": read}
            except Exception:
                # Never let the narrative layer sink a pick.
                logger.exception("rationale failed for fixture %s / %s", fixture.pk, cand.market)

        prediction, _ = Prediction.objects.update_or_create(
            fixture=fixture,
            market=cand.market,
            engine_version=ENGINE_VERSION,
            defaults=defaults,
        )
        created.append(prediction)

    return created
