"""Scheduled ingest. Every task here is an idempotent upsert keyed on api_id."""

import logging
from datetime import date, timedelta

from celery import shared_task
from django.conf import settings
from django.utils.dateparse import parse_datetime

from .models import Fixture, Injury, League, Odds, Team
from .providers.api_football import ApiFootballClient

logger = logging.getLogger(__name__)

STATUS_MAP = {
    "NS": Fixture.Status.SCHEDULED, "TBD": Fixture.Status.SCHEDULED,
    "1H": Fixture.Status.LIVE, "HT": Fixture.Status.LIVE, "2H": Fixture.Status.LIVE,
    "ET": Fixture.Status.LIVE, "P": Fixture.Status.LIVE, "LIVE": Fixture.Status.LIVE,
    "FT": Fixture.Status.FINISHED, "AET": Fixture.Status.FINISHED, "PEN": Fixture.Status.FINISHED,
    "PST": Fixture.Status.POSTPONED, "CANC": Fixture.Status.CANCELLED,
    "ABD": Fixture.Status.CANCELLED, "AWD": Fixture.Status.FINISHED, "WO": Fixture.Status.FINISHED,
}


def _upsert_team(node: dict) -> Team:
    team, _ = Team.objects.update_or_create(
        api_id=node["id"],
        defaults={"name": node["name"], "logo_url": node.get("logo") or ""},
    )
    return team


class DemoDataPresent(RuntimeError):
    """Raised rather than merging a real league into seeded rows."""


def _has_demo_data() -> bool:
    """
    `seed_demo` writes fixtures with an empty `raw`; every real ingest stores the
    provider payload there. It also squats on real api-ids (39 = the actual
    Premier League), and since every upsert keys on api_id, syncing over it would
    quietly graft real metadata onto simulated fixtures and leave a league that
    is half invented. Cheap, reliable marker — and worth checking before writing.
    """
    return Fixture.objects.filter(raw={}).exists()


def _current_season(node: dict) -> int | None:
    """
    The season flagged `current`, falling back to the latest year offered.

    This matters more than it looks: `sync_fixtures` passes `league.season`
    straight to the provider, and a season the plan cannot see returns an empty
    list rather than an error — a slate that is silently always blank.
    """
    seasons = node.get("seasons") or []
    for season in seasons:
        if season.get("current"):
            return season.get("year")
    years = [s.get("year") for s in seasons if s.get("year")]
    return max(years) if years else None


@shared_task
def sync_leagues(force: bool = False) -> int:
    """
    Create or refresh the `League` rows named by `API_FOOTBALL_LEAGUES`.

    Nothing else creates real leagues, and `sync_fixtures` only walks rows that
    already exist — so until this runs, a perfectly configured key syncs nothing.
    One request per league, run rarely.
    """
    if not settings.API_FOOTBALL_LEAGUES:
        raise ValueError(
            "API_FOOTBALL_LEAGUES is empty. Set the league api-ids to sync — "
            "syncing every league the provider offers would create thousands of "
            "rows and exhaust the daily quota."
        )

    if not force and _has_demo_data():
        raise DemoDataPresent(
            "This database holds seeded demo fixtures, which occupy real "
            "api-ids. Syncing real leagues over them would merge invented and "
            "real rows. Wipe first (`manage.py flush`, or drop the volume), or "
            "pass force=True if you accept the mix."
        )

    client = ApiFootballClient()
    synced = 0

    for api_id in settings.API_FOOTBALL_LEAGUES:
        nodes = client.leagues(api_id)
        if not nodes:
            logger.warning("league %s returned nothing; skipping", api_id)
            continue

        node = nodes[0]
        league, country = node["league"], node.get("country") or {}
        season = _current_season(node)
        if season is None:
            logger.warning("league %s has no usable season; skipping", api_id)
            continue

        League.objects.update_or_create(
            api_id=league["id"],
            defaults={
                "name": league["name"],
                "country": country.get("name") or "",
                "logo_url": league.get("logo") or "",
                "season": season,
                "is_active": True,
                "raw": node,
            },
        )
        synced += 1
        logger.info("league %s: %s (%s) season %s", api_id, league["name"],
                    country.get("name") or "?", season)

    logger.info("synced %s leagues in %s requests", synced, len(settings.API_FOOTBALL_LEAGUES))
    return synced


def leagues_to_sync():
    """
    Active leagues, narrowed by `API_FOOTBALL_LEAGUES` when that is set.

    The setting is the cheap way to cap request volume: one entry removed is
    `days_ahead + 1` requests saved per run, and on the free plan (100/day) that
    is the difference between a working slate and a task that dies on quota
    partway through the morning. An empty setting means "trust the database".
    """
    leagues = League.objects.filter(is_active=True)
    if settings.API_FOOTBALL_LEAGUES:
        leagues = leagues.filter(api_id__in=settings.API_FOOTBALL_LEAGUES)
    return leagues


@shared_task
def sync_fixtures(days_ahead: int = 3) -> int:
    """Pull the fixture list for the next few days across every synced league."""
    client = ApiFootballClient()
    count = 0
    requests = 0

    for league in leagues_to_sync():
        for offset in range(days_ahead + 1):
            requests += 1
            on = date.today() + timedelta(days=offset)
            for node in client.fixtures_by_date(on, league.api_id, league.season):
                fx = node["fixture"]
                goals = node.get("goals", {})
                score = node.get("score", {}).get("halftime", {})

                Fixture.objects.update_or_create(
                    api_id=fx["id"],
                    defaults={
                        "league": league,
                        "home": _upsert_team(node["teams"]["home"]),
                        "away": _upsert_team(node["teams"]["away"]),
                        "kickoff": parse_datetime(fx["date"]),
                        "round": node.get("league", {}).get("round", "") or "",
                        "venue": (fx.get("venue") or {}).get("name") or "",
                        "status": STATUS_MAP.get(fx["status"]["short"], Fixture.Status.SCHEDULED),
                        "home_goals": goals.get("home"),
                        "away_goals": goals.get("away"),
                        "home_goals_ht": score.get("home"),
                        "away_goals_ht": score.get("away"),
                        "raw": node,
                    },
                )
                count += 1

    # Request count is logged because the quota, not runtime, is what this task
    # actually runs out of — and API-Football answers an exhausted quota with a
    # 200 and an `errors` object, so the failure reads as a parameter problem
    # unless you already know how many calls you have spent today.
    logger.info("synced %s fixtures in %s requests", count, requests)
    return count


@shared_task
def sync_odds_and_injuries(fixture_id: int) -> None:
    """Called per fixture shortly before prediction time, when lineups firm up."""
    client = ApiFootballClient()
    fixture = Fixture.objects.get(pk=fixture_id)

    for node in client.odds(fixture.api_id):
        for book in node.get("bookmakers", []):
            for bet in book.get("bets", []):
                market = _map_market(bet.get("name", ""))
                if not market:
                    continue
                for value in bet.get("values", []):
                    selection = _map_selection(market, value.get("value", ""))
                    if not selection:
                        continue
                    Odds.objects.update_or_create(
                        fixture=fixture,
                        bookmaker=book["name"],
                        market=market,
                        selection=selection,
                        defaults={"price": value["odd"]},
                    )

    fixture.injuries.all().delete()
    for node in client.injuries(fixture.api_id):
        Injury.objects.create(
            fixture=fixture,
            team=_upsert_team(node["team"]),
            player_name=node["player"]["name"],
            reason=node["player"].get("reason") or "",
            type=node["player"].get("type") or "",
        )


def _map_market(name: str) -> str | None:
    name = name.lower()
    if name in ("match winner", "1x2"):
        return "1x2"
    if "over/under" in name:
        return "ou_2_5"
    if "both teams" in name:
        return "btts"
    if "double chance" in name:
        return "dc"
    return None


def _map_selection(market: str, value: str) -> str | None:
    value = value.strip().lower()
    if market == "1x2":
        return {"home": "home", "draw": "draw", "away": "away"}.get(value)
    if market == "ou_2_5":
        # Only the 2.5 line matters to us; every other line is dropped.
        return {"over 2.5": "over", "under 2.5": "under"}.get(value)
    if market == "btts":
        return {"yes": "yes", "no": "no"}.get(value)
    if market == "dc":
        return {"home/draw": "home_draw", "home/away": "home_away", "draw/away": "draw_away"}.get(value)
    return None
