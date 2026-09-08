"""
Team strength maintenance — the inputs poisson.py reads.

Two independent ratings, kept deliberately separate because they answer different
questions and fail in different ways:

* **Attack / defence strengths** are goal-rate ratios against the league average
  (1.20 attack = scores 20% more than the average side in that league). They feed
  the Poisson lambdas directly. Time-decayed, so a team that fixed its defence in
  October isn't still judged on August. Kept both venue-agnostic and **split by
  home/away**, because a side that is a fortress at home and dreadful away
  averages into mush under one rating — and that is precisely the fixture the
  model most needs to get right.
* **Elo** is a single head-to-head strength number. It reacts faster to results
  against strong opposition and is the stabiliser when goal samples are thin —
  a promoted side with six games played has meaningless goal ratios but a
  perfectly usable Elo.

Both are recomputed from settled fixtures only. Nothing here reads a prediction,
so the ratings can never learn from our own picks.
"""

import logging
import math
from collections import defaultdict

from django.db import transaction
from django.utils import timezone

from apps.fixtures.models import Fixture, League, Team

logger = logging.getLogger(__name__)

# Results older than this stop informing the strengths at all.
LOOKBACK_DAYS = 400
# Weight halves every this many days. Roughly two months — long enough to survive
# an international break, short enough to catch a managerial change.
HALF_LIFE_DAYS = 60
# Elo K-factor. 20 is the standard club-football value; higher chases noise.
ELO_K = 20.0
# Strength ratios are clamped: one 7-0 in a small sample must not produce a team
# the model thinks scores four goals a game.
MIN_STRENGTH, MAX_STRENGTH = 0.35, 2.5
# Fallback when a league has too little settled data to compute its own average.
DEFAULT_LEAGUE_AVG_GOALS = 1.35
# Venue-specific fallbacks. Their mean is DEFAULT_LEAGUE_AVG_GOALS; the gap
# between them is a typical league's home advantage, used only until there are
# enough settled fixtures to measure the real one.
DEFAULT_HOME_GOALS, DEFAULT_AWAY_GOALS = 1.45, 1.25
# Below this many fixtures a league's own averages are too noisy to use.
MIN_FIXTURES_FOR_LEAGUE_AVG = 20
# Below this many games a team's ratios are blended toward 1.0 (league average)
# rather than trusted outright.
MIN_GAMES_FOR_FULL_WEIGHT = 8
# The venue split sees roughly half a team's fixtures, so it earns full trust
# sooner in absolute terms — but it is shrunk toward the team's *own* overall
# rating rather than toward 1.0, which is a much better prior than the league.
MIN_VENUE_GAMES_FOR_FULL_WEIGHT = 5


def _decay(days_ago: float) -> float:
    return 0.5 ** (days_ago / HALF_LIFE_DAYS)


def _settled_fixtures(league: League, as_of):
    """
    Settled fixtures in the lookback window ending at `as_of`.

    The upper bound is what makes a walk-forward backtest honest. In production
    `as_of` is now and nothing later is FINISHED anyway, so it changes nothing —
    but when replaying a season that is *already* settled in the database, an
    unbounded query happily rates a match using results that had not happened
    yet. That is lookahead bias, and it yields a backtest that looks brilliant
    and predicts nothing.
    """
    return Fixture.objects.filter(
        league=league,
        status=Fixture.Status.FINISHED,
        home_goals__isnull=False,
        kickoff__gte=as_of - timezone.timedelta(days=LOOKBACK_DAYS),
        kickoff__lt=as_of,
    )


def _settled_scorelines(league: League, as_of) -> list[tuple[int, int]]:
    return list(_settled_fixtures(league, as_of).values_list("home_goals", "away_goals"))


def league_average_goals(league: League, as_of=None) -> float:
    """Goals per team per game in this league, over the lookback window."""
    rows = _settled_scorelines(league, as_of or timezone.now())
    if len(rows) < MIN_FIXTURES_FOR_LEAGUE_AVG:
        return DEFAULT_LEAGUE_AVG_GOALS
    total = sum(h + a for h, a in rows)
    # Two teams per fixture, so divide by 2N to get per-team-per-game.
    return total / (2 * len(rows))


def league_venue_averages(league: League, as_of=None) -> tuple[float, float]:
    """
    Goals per game scored by the home side and by the away side in this league.

    This is home advantage, measured rather than assumed. Normalising each venue
    strength against its own average is what lets the multiplier drop out of
    `poisson.expected_goals`: a league where home sides score 1.52 and away sides
    1.18 gets that ratio, not a hardcoded 1.15 borrowed from the Premier League.
    """
    rows = _settled_scorelines(league, as_of or timezone.now())
    if len(rows) < MIN_FIXTURES_FOR_LEAGUE_AVG:
        return DEFAULT_HOME_GOALS, DEFAULT_AWAY_GOALS
    n = len(rows)
    return sum(h for h, _ in rows) / n, sum(a for _, a in rows) / n


def _shrink(raw: float, prior: float, weight: float, full_weight: float) -> float:
    """
    Pull `raw` toward `prior` in proportion to how little we have seen. `weight`
    is a decayed game count, so stale fixtures are already discounted.
    """
    trust = min(weight / full_weight, 1.0)
    return prior + trust * (raw - prior)


def _clamp(value: float) -> float:
    return min(max(value, MIN_STRENGTH), MAX_STRENGTH)


@transaction.atomic
def update_strengths(league: League, as_of=None) -> int:
    """
    Recompute attack/defence strengths for every team in `league`, overall and
    split by venue.

    Two stages of shrinkage, because the venue split halves the sample:

    1. The overall ratio is shrunk toward 1.0 (the league average) — the
       standard small-sample fix, and the difference between a model that
       respects a promoted side and one that thinks it's Barcelona after two
       good weeks.
    2. Each venue ratio is then shrunk toward *that team's own overall rating*,
       not toward 1.0. A side with three home games is far better described by
       "this team, roughly" than by "an average team in this league", so the
       split degrades gracefully into the venue-agnostic rating instead of
       collapsing to the league mean.
    """
    now = timezone.now()
    # Everything — the league averages, the window, and the recency decay — is
    # measured from the same instant, so a replayed fixture is rated exactly as
    # it would have been on the day.
    as_of = as_of or now
    avg = league_average_goals(league, as_of)
    home_avg, away_avg = league_venue_averages(league, as_of)

    # venue -> team_id -> total. "home"/"away" is the venue the team played at,
    # so `conceded["home"][t]` is what team t shipped in its own stadium.
    scored: dict[str, dict[int, float]] = {"home": defaultdict(float), "away": defaultdict(float)}
    conceded: dict[str, dict[int, float]] = {"home": defaultdict(float), "away": defaultdict(float)}
    weight: dict[str, dict[int, float]] = {"home": defaultdict(float), "away": defaultdict(float)}

    for fx in _settled_fixtures(league, as_of).select_related("home", "away"):
        w = _decay((as_of - fx.kickoff).days)
        for venue, team_id, gf, ga in (
            ("home", fx.home_id, fx.home_goals, fx.away_goals),
            ("away", fx.away_id, fx.away_goals, fx.home_goals),
        ):
            scored[venue][team_id] += w * gf
            conceded[venue][team_id] += w * ga
            weight[venue][team_id] += w

    seen = set(weight["home"]) | set(weight["away"])
    fields = [
        "attack_strength", "defence_strength",
        "home_attack_strength", "home_defence_strength",
        "away_attack_strength", "away_defence_strength",
        "ratings_updated_at",
    ]

    updated = []
    for team in Team.objects.filter(pk__in=seen):
        hw, aw = weight["home"][team.pk], weight["away"][team.pk]
        total_w = hw + aw
        if total_w <= 0:
            continue

        # Stage 1 — venue-agnostic, against the overall per-team-per-game average.
        total_scored = scored["home"][team.pk] + scored["away"][team.pk]
        total_conceded = conceded["home"][team.pk] + conceded["away"][team.pk]
        attack = _shrink((total_scored / total_w) / avg, 1.0, total_w, MIN_GAMES_FOR_FULL_WEIGHT)
        defence = _shrink((total_conceded / total_w) / avg, 1.0, total_w, MIN_GAMES_FOR_FULL_WEIGHT)

        # Stage 2 — venue splits, against the matching venue average. Goals a
        # team concedes at home were scored by an away side, so they normalise
        # against `away_avg`, and vice versa. Getting that pairing backwards
        # would bake the home advantage into the defensive ratings twice.
        def venue(venue_name: str, totals: dict, denom: float, prior: float) -> float:
            w = weight[venue_name][team.pk]
            if w <= 0:
                return _clamp(prior)
            raw = (totals[venue_name][team.pk] / w) / denom
            return _clamp(_shrink(raw, prior, w, MIN_VENUE_GAMES_FOR_FULL_WEIGHT))

        team.attack_strength = _clamp(attack)
        team.defence_strength = _clamp(defence)
        team.home_attack_strength = venue("home", scored, home_avg, attack)
        team.home_defence_strength = venue("home", conceded, away_avg, defence)
        team.away_attack_strength = venue("away", scored, away_avg, attack)
        team.away_defence_strength = venue("away", conceded, home_avg, defence)
        team.ratings_updated_at = now
        updated.append(team)

    Team.objects.bulk_update(updated, fields)
    logger.info(
        "strengths: %s teams in %s (avg %.2f, home %.2f, away %.2f)",
        len(updated), league, avg, home_avg, away_avg,
    )
    return len(updated)


def _goal_difference_multiplier(margin: int) -> float:
    """
    Dampened margin-of-victory weight. A 4-0 says more than a 1-0, but not four
    times more — without dampening, one thrashing distorts a rating for months.
    """
    return math.log(abs(margin) + 1) if margin else 1.0


@transaction.atomic
def apply_elo(fixture: Fixture) -> None:
    """
    Apply one settled fixture to both teams' Elo. Guarded by `elo_applied` so a
    re-run of the settlement task can't double-count a result.
    """
    if fixture.elo_applied or not fixture.is_settled:
        return

    home, away = fixture.home, fixture.away
    expected_home = 1.0 / (1.0 + 10 ** (-((home.elo + 65.0) - away.elo) / 400))

    margin = fixture.home_goals - fixture.away_goals
    actual_home = 1.0 if margin > 0 else 0.0 if margin < 0 else 0.5
    change = ELO_K * _goal_difference_multiplier(margin) * (actual_home - expected_home)

    home.elo += change
    away.elo -= change
    home.ratings_updated_at = away.ratings_updated_at = timezone.now()

    Team.objects.bulk_update([home, away], ["elo", "ratings_updated_at"])

    fixture.elo_applied = True
    fixture.save(update_fields=["elo_applied", "updated_at"])


def refresh_all(league_ids: list[int] | None = None) -> dict:
    """Entry point for the nightly task: Elo first, then strengths."""
    leagues = League.objects.filter(is_active=True)
    if league_ids:
        leagues = leagues.filter(pk__in=league_ids)

    elo_applied = 0
    pending = Fixture.objects.filter(
        league__in=leagues,
        status=Fixture.Status.FINISHED,
        home_goals__isnull=False,
        elo_applied=False,
    ).select_related("home", "away").order_by("kickoff")

    # Chronological order matters: Elo is path-dependent.
    for fixture in pending:
        apply_elo(fixture)
        elo_applied += 1

    teams_updated = sum(update_strengths(league) for league in leagues)
    return {"elo_applied": elo_applied, "teams_updated": teams_updated}
