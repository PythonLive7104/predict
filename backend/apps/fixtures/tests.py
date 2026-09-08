"""
Quota guards.

On the free API-Football plan the binding constraint is 100 requests/day, and an
exhausted quota comes back as HTTP 200 with an `errors` object — so overspending
does not look like overspending, it looks like a parameter bug. These pin the two
knobs that decide the bill.
"""

from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.fixtures.models import Fixture, League, Team
from apps.fixtures.tasks import (
    DemoDataPresent,
    leagues_to_sync,
    sync_fixtures,
    sync_leagues,
)


def _league(api_id, name, is_active=True):
    return League.objects.create(
        api_id=api_id, name=name, country="X", season=2026, is_active=is_active
    )


class LeagueSelectionTests(TestCase):
    def setUp(self):
        self.epl = _league(39, "Premier League")
        self.liga = _league(140, "La Liga")
        self.seriea = _league(135, "Serie A")
        self.dormant = _league(61, "Ligue 1", is_active=False)

    @override_settings(API_FOOTBALL_LEAGUES=[39, 140])
    def test_setting_narrows_the_active_leagues(self):
        self.assertCountEqual(leagues_to_sync(), [self.epl, self.liga])

    @override_settings(API_FOOTBALL_LEAGUES=[])
    def test_empty_setting_falls_back_to_every_active_league(self):
        self.assertCountEqual(leagues_to_sync(), [self.epl, self.liga, self.seriea])

    @override_settings(API_FOOTBALL_LEAGUES=[39, 61])
    def test_setting_cannot_reactivate_a_dormant_league(self):
        """The setting narrows the active set; it is not an override of it."""
        self.assertCountEqual(leagues_to_sync(), [self.epl])


class RequestBudgetTests(TestCase):
    def setUp(self):
        _league(39, "Premier League")
        _league(140, "La Liga")
        _league(135, "Serie A")

    @override_settings(API_FOOTBALL_LEAGUES=[39, 140])
    @patch("apps.fixtures.tasks.ApiFootballClient")
    def test_one_request_per_league_per_day(self, client_cls):
        client_cls.return_value.fixtures_by_date.return_value = []

        sync_fixtures(days_ahead=3)

        # 2 leagues x (3 + 1) days. The third league is configured out, and each
        # league removed is days_ahead+1 requests saved per run.
        self.assertEqual(client_cls.return_value.fixtures_by_date.call_count, 8)

    @override_settings(API_FOOTBALL_LEAGUES=[39, 140])
    @patch("apps.fixtures.tasks.ApiFootballClient")
    def test_shorter_horizon_costs_proportionally_less(self, client_cls):
        client_cls.return_value.fixtures_by_date.return_value = []

        sync_fixtures(days_ahead=1)

        self.assertEqual(client_cls.return_value.fixtures_by_date.call_count, 4)


def _league_node(api_id=39, name="Premier League", country="England", seasons=None):
    return {
        "league": {"id": api_id, "name": name, "type": "League",
                   "logo": f"https://media.api-sports.io/football/leagues/{api_id}.png"},
        "country": {"name": country, "code": "GB-ENG"},
        "seasons": seasons if seasons is not None else [
            {"year": 2024, "current": False},
            {"year": 2025, "current": False},
            {"year": 2026, "current": True},
        ],
    }


@override_settings(API_FOOTBALL_LEAGUES=[39])
class SyncLeaguesTests(TestCase):
    """
    Nothing else creates real leagues, and `sync_fixtures` only walks rows that
    already exist — so a bug here shows up as an empty slate, not an error.
    """

    @patch("apps.fixtures.tasks.ApiFootballClient")
    def test_creates_the_league_with_its_current_season(self, client_cls):
        client_cls.return_value.leagues.return_value = [_league_node()]

        self.assertEqual(sync_leagues(), 1)

        league = League.objects.get(api_id=39)
        self.assertEqual(league.name, "Premier League")
        self.assertEqual(league.country, "England")
        self.assertEqual(league.season, 2026)
        self.assertTrue(league.is_active)
        # Coverage flags come along, so we never re-pay to learn whether this
        # league has odds or injuries on this plan.
        self.assertIn("seasons", league.raw)

    @patch("apps.fixtures.tasks.ApiFootballClient")
    def test_rerunning_updates_rather_than_duplicating(self, client_cls):
        client_cls.return_value.leagues.return_value = [_league_node()]
        sync_leagues()
        client_cls.return_value.leagues.return_value = [
            _league_node(name="Premier League", seasons=[{"year": 2027, "current": True}])
        ]
        sync_leagues()

        self.assertEqual(League.objects.filter(api_id=39).count(), 1)
        self.assertEqual(League.objects.get(api_id=39).season, 2027)

    @patch("apps.fixtures.tasks.ApiFootballClient")
    def test_falls_back_to_the_latest_year_when_none_is_flagged_current(self, client_cls):
        """
        A season the plan cannot see returns an empty fixture list rather than an
        error, so guessing wrong here means a permanently blank slate.
        """
        client_cls.return_value.leagues.return_value = [
            _league_node(seasons=[{"year": 2024, "current": False},
                                  {"year": 2025, "current": False}])
        ]
        sync_leagues()
        self.assertEqual(League.objects.get(api_id=39).season, 2025)

    @patch("apps.fixtures.tasks.ApiFootballClient")
    def test_a_league_with_no_seasons_is_skipped_not_stored_broken(self, client_cls):
        client_cls.return_value.leagues.return_value = [_league_node(seasons=[])]
        self.assertEqual(sync_leagues(), 0)
        self.assertFalse(League.objects.filter(api_id=39).exists())

    @patch("apps.fixtures.tasks.ApiFootballClient")
    def test_an_empty_response_is_skipped(self, client_cls):
        client_cls.return_value.leagues.return_value = []
        self.assertEqual(sync_leagues(), 0)
        self.assertFalse(League.objects.exists())

    @override_settings(API_FOOTBALL_LEAGUES=[])
    @patch("apps.fixtures.tasks.ApiFootballClient")
    def test_refuses_to_sync_every_league_in_the_world(self, client_cls):
        with self.assertRaises(ValueError):
            sync_leagues()
        client_cls.assert_not_called()

    @patch("apps.fixtures.tasks.ApiFootballClient")
    def test_refuses_to_merge_real_leagues_into_seeded_demo_data(self, client_cls):
        """
        `seed_demo` squats on real api-ids (39 is the actual Premier League) and
        every upsert keys on api_id, so syncing over it grafts real metadata onto
        simulated fixtures.
        """
        demo_league = _league(39, "Premier League")
        team = Team.objects.create(api_id=1, name="Seeded")
        Fixture.objects.create(
            api_id=1, league=demo_league, home=team, away=team,
            kickoff=timezone.now(), raw={},   # the demo marker
        )

        with self.assertRaises(DemoDataPresent):
            sync_leagues()
        client_cls.assert_not_called()

    @patch("apps.fixtures.tasks.ApiFootballClient")
    def test_force_overrides_the_demo_guard(self, client_cls):
        client_cls.return_value.leagues.return_value = [_league_node()]
        demo_league = _league(39, "Premier League")
        team = Team.objects.create(api_id=1, name="Seeded")
        Fixture.objects.create(
            api_id=1, league=demo_league, home=team, away=team,
            kickoff=timezone.now(), raw={},
        )

        self.assertEqual(sync_leagues(force=True), 1)
