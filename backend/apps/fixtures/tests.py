"""
Quota guards.

On the free API-Football plan the binding constraint is 100 requests/day, and an
exhausted quota comes back as HTTP 200 with an `errors` object — so overspending
does not look like overspending, it looks like a parameter bug. These pin the two
knobs that decide the bill.
"""

from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.fixtures.models import Fixture, League, Team
from apps.fixtures.tasks import (
    sync_odds_for_upcoming,
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


@override_settings(API_FOOTBALL_LEAGUES=[39])
class BackfillTests(TestCase):
    """
    History is what makes the ratings mean anything. Without it every team sits
    at the default and the model prices a coin flip with home advantage.
    """

    def setUp(self):
        self.league = League.objects.create(
            api_id=39, name="Premier League", country="England", season=2026
        )

    def _node(self, fixture_id, home_goals=2, away_goals=1, status="FT"):
        return {
            "fixture": {"id": fixture_id, "date": "2025-08-16T14:00:00+00:00",
                        "status": {"short": status}, "venue": {"name": "Ground"}},
            "league": {"round": "Regular Season - 1"},
            "teams": {"home": {"id": 100, "name": "Home FC"},
                      "away": {"id": 200, "name": "Away FC"}},
            "goals": {"home": home_goals, "away": away_goals},
            "score": {"halftime": {"home": 1, "away": 0}},
        }

    @patch("apps.fixtures.management.commands.backfill.ApiFootballClient")
    def test_one_request_per_league_season(self, client_cls):
        """A season day-by-day would be ~380 requests; by season it is one."""
        client_cls.return_value.fixtures_by_season.return_value = [self._node(1)]

        call_command("backfill", "--seasons", "3", stdout=StringIO())

        self.assertEqual(client_cls.return_value.fixtures_by_season.call_count, 3)
        seasons = [c.args[1] for c in client_cls.return_value.fixtures_by_season.call_args_list]
        self.assertEqual(sorted(seasons), [2023, 2024, 2025])

    @patch("apps.fixtures.management.commands.backfill.ApiFootballClient")
    def test_results_are_stored_so_ratings_can_use_them(self, client_cls):
        client_cls.return_value.fixtures_by_season.return_value = [self._node(1, 3, 0)]

        call_command("backfill", "--seasons", "1", stdout=StringIO())

        fixture = Fixture.objects.get(api_id=1)
        self.assertEqual(fixture.status, Fixture.Status.FINISHED)
        self.assertEqual((fixture.home_goals, fixture.away_goals), (3, 0))
        # `raw` is kept so a feature can be re-derived without paying again.
        self.assertIn("teams", fixture.raw)

    @patch("apps.fixtures.management.commands.backfill.ApiFootballClient")
    def test_rerunning_updates_rather_than_duplicating(self, client_cls):
        client_cls.return_value.fixtures_by_season.return_value = [self._node(1)]
        call_command("backfill", "--seasons", "1", stdout=StringIO())
        call_command("backfill", "--seasons", "1", stdout=StringIO())

        self.assertEqual(Fixture.objects.filter(api_id=1).count(), 1)

    @patch("apps.fixtures.management.commands.backfill.ApiFootballClient")
    def test_a_season_the_plan_cannot_see_does_not_abort_the_run(self, client_cls):
        from apps.fixtures.providers.api_football import ApiFootballError

        client_cls.return_value.fixtures_by_season.side_effect = [
            ApiFootballError("plan does not include this season"),
            [self._node(2)],
        ]
        out = StringIO()
        call_command("backfill", "--seasons", "2", stdout=out)

        self.assertIn("skipped", out.getvalue())
        self.assertEqual(Fixture.objects.count(), 1)

    def test_refuses_without_leagues(self):
        League.objects.all().delete()
        with self.assertRaises(CommandError):
            call_command("backfill", stdout=StringIO())


class OddsSyncTests(TestCase):
    """
    Odds were fetched by nothing for the whole life of the project, which made
    `market_odds`, `edge` and the entire odds-target builder silently inert.
    """

    def setUp(self):
        self.league = League.objects.create(
            api_id=39, name="Premier League", country="England", season=2026
        )
        self.home = Team.objects.create(api_id=1, name="Home")
        self.away = Team.objects.create(api_id=2, name="Away")

    def _fixture(self, hours_ahead, status=Fixture.Status.SCHEDULED, api_id=None):
        return Fixture.objects.create(
            api_id=api_id or (9000 + Fixture.objects.count()),
            league=self.league, home=self.home, away=self.away,
            kickoff=timezone.now() + timedelta(hours=hours_ahead),
            status=status,
        )

    @patch("apps.fixtures.tasks.sync_odds_and_injuries")
    def test_only_upcoming_scheduled_fixtures_are_priced(self, per_fixture):
        wanted = self._fixture(6)
        self._fixture(-6)                                   # already kicked off
        self._fixture(200)                                  # beyond the window
        self._fixture(6, status=Fixture.Status.FINISHED)    # nothing left to price

        result = sync_odds_for_upcoming(hours_ahead=48)

        self.assertEqual(result["fixtures"], 1)
        per_fixture.assert_called_once_with(wanted.pk)

    @patch("apps.fixtures.tasks.sync_odds_and_injuries")
    def test_request_cost_is_reported(self, per_fixture):
        """Two calls per fixture; this is the largest scheduled draw on quota."""
        for _ in range(3):
            self._fixture(4)

        result = sync_odds_for_upcoming()

        self.assertEqual(result["synced"], 3)
        self.assertEqual(result["requests"], 6)

    @patch("apps.fixtures.tasks.sync_odds_and_injuries")
    def test_one_failure_does_not_cost_the_rest(self, per_fixture):
        """A fixture with no market published yet is normal, not fatal."""
        for _ in range(3):
            self._fixture(4)
        per_fixture.side_effect = [Exception("no odds yet"), None, None]

        result = sync_odds_for_upcoming()

        self.assertEqual(result["synced"], 2)
        self.assertEqual(result["failed"], 1)

    @patch("apps.fixtures.tasks.sync_odds_and_injuries")
    def test_an_empty_window_is_not_an_error(self, per_fixture):
        result = sync_odds_for_upcoming()
        self.assertEqual(result, {"fixtures": 0, "synced": 0, "failed": 0, "requests": 0})
        per_fixture.assert_not_called()


class FindLeaguesTests(TestCase):
    """
    Turning fifty league names into ids. The matching has to be forgiving —
    providers write "Liga Profesional Argentina" one season and "Primera
    División" the next — without being so loose it silently returns the wrong
    country's league.
    """

    NODES = [
        {"league": {"id": 39, "name": "Premier League", "type": "League"},
         "country": {"name": "England"}},
        {"league": {"id": 235, "name": "Premier League", "type": "League"},
         "country": {"name": "Russia"}},
        {"league": {"id": 71, "name": "Serie A", "type": "League"},
         "country": {"name": "Brazil"}},
        {"league": {"id": 135, "name": "Serie A", "type": "League"},
         "country": {"name": "Italy"}},
        {"league": {"id": 128, "name": "Liga Profesional Argentina", "type": "League"},
         "country": {"name": "Argentina"}},
        {"league": {"id": 2, "name": "UEFA Champions League", "type": "Cup"},
         "country": {"name": "World"}},
    ]

    def _run(self, *args):
        out = StringIO()
        with patch(
            "apps.fixtures.management.commands.find_leagues.ApiFootballClient"
        ) as client_cls:
            client_cls.return_value.leagues.return_value = self.NODES
            call_command("find_leagues", *args, stdout=out)
        return out.getvalue()

    def test_country_disambiguates_leagues_sharing_a_name(self):
        """Two Premier Leagues and two Serie As — the name alone is not enough."""
        output = self._run("--top50")

        self.assertIn("39", output)     # England
        self.assertIn("235", output)    # Russia
        self.assertIn("71", output)     # Brazil
        self.assertIn("135", output)    # Italy

    def test_emits_a_pasteable_env_line(self):
        output = self._run("--top50")
        self.assertIn("API_FOOTBALL_LEAGUES=", output)

    def test_unmatched_leagues_are_named_not_dropped(self):
        """
        A league the plan cannot see, or one named differently, must be visible —
        silently returning 6 of 50 ids would look like success.
        """
        output = self._run("--top50")
        self.assertIn("Not matched", output)

    def test_cups_are_excluded_by_default(self):
        """A cup has no league table and far thinner rating history."""
        output = self._run("Champions League")
        self.assertNotIn("UEFA Champions League", output)

    def test_cups_can_be_asked_for_explicitly(self):
        output = self._run("--type", "Cup", "Champions League")
        self.assertIn("UEFA Champions League", output)

    def test_request_cost_is_reported(self):
        """Breadth is free in code and not free in requests or tokens."""
        output = self._run("--top50")
        self.assertIn("What this costs per day", output)
        self.assertIn("7,500 on PRO", output)

    def test_refuses_with_no_input(self):
        with self.assertRaises(CommandError):
            call_command("find_leagues", stdout=StringIO())
