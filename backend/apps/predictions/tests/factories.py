"""Minimal builders — enough to exercise the engine without touching the network."""

from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.fixtures.models import Fixture, League, Odds, Team

_counter = {"n": 0}


def _next_id() -> int:
    _counter["n"] += 1
    return _counter["n"]


def make_league(**kwargs) -> League:
    return League.objects.create(
        api_id=_next_id(), name=kwargs.pop("name", "Test League"),
        country="Testland", season=2026, **kwargs,
    )


def make_team(name="Team", **kwargs) -> Team:
    return Team.objects.create(api_id=_next_id(), name=name, **kwargs)


def make_fixture(league=None, home=None, away=None, kickoff=None, **kwargs) -> Fixture:
    """
    Kickoff defaults to midday UTC today. Publishing keys on the UTC calendar
    date, so a naive `now() + 6h` silently rolls onto tomorrow when the suite runs
    in the evening and every date-scoped assertion goes quiet.
    """
    league = league or make_league()
    return Fixture.objects.create(
        api_id=_next_id(),
        league=league,
        home=home or make_team("Home"),
        away=away or make_team("Away"),
        kickoff=kickoff or timezone.now().replace(hour=12, minute=0, second=0, microsecond=0),
        **kwargs,
    )


def add_odds(fixture, market, selection, price) -> Odds:
    return Odds.objects.create(
        fixture=fixture, bookmaker="TestBook", market=market,
        selection=selection, price=Decimal(str(price)),
    )
