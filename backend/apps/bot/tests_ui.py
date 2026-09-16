"""
Date spans and the odds builder, at the service boundary.

These call the same functions the handlers call, so a change that breaks the
bot's two new surfaces fails here rather than in a Telegram chat.
"""

from datetime import date, timedelta
from decimal import Decimal

from asgiref.sync import async_to_sync
from django.test import TestCase
from django.utils import timezone

from apps.bot import keyboards as kb
from apps.bot import services
from apps.predictions.access import span_dates
from apps.predictions.models import Market, Prediction, Tier
from apps.predictions.tests.factories import make_fixture, make_league, make_team


class SpanTests(TestCase):
    def test_weekend_is_friday_to_sunday_from_any_starting_day(self):
        for day, expected_friday in (
            (date(2026, 9, 14), date(2026, 9, 18)),   # Monday -> coming weekend
            (date(2026, 9, 17), date(2026, 9, 18)),   # Thursday -> tomorrow
            (date(2026, 9, 19), date(2026, 9, 18)),   # Saturday -> the one in progress
            (date(2026, 9, 20), date(2026, 9, 18)),   # Sunday -> still this one
        ):
            start, end, label = span_dates("weekend", day)
            self.assertEqual(start, expected_friday, day.strftime("%A"))
            self.assertEqual(end, expected_friday + timedelta(days=2))
            self.assertEqual(label, "This weekend")

    def test_today_and_tomorrow(self):
        day = date(2026, 9, 14)
        self.assertEqual(span_dates("today", day)[:2], (day, day))
        self.assertEqual(span_dates("tomorrow", day)[:2],
                         (day + timedelta(days=1), day + timedelta(days=1)))

    def test_an_unknown_span_falls_back_to_today(self):
        day = date(2026, 9, 14)
        self.assertEqual(span_dates("next-year", day)[:2], (day, day))


class PicksForSpanTests(TestCase):
    def setUp(self):
        self.league = make_league()
        self.n = 0

    def _pick(self, days_ahead, tier=Tier.FREE, published=True):
        self.n += 1
        fixture = make_fixture(
            league=self.league,
            home=make_team(f"H{self.n}"), away=make_team(f"A{self.n}"),
            kickoff=timezone.now().replace(hour=12, minute=0, second=0, microsecond=0)
                    + timedelta(days=days_ahead, minutes=self.n),
        )
        return Prediction.objects.create(
            fixture=fixture, market=Market.MATCH_RESULT, selection="home",
            probability=0.7, confidence=70, tier=tier,
            published_at=timezone.now() if published else None,
        )

    def test_tomorrow_shows_only_tomorrow(self):
        self._pick(0)
        wanted = self._pick(1)

        rows, label = async_to_sync(services.picks_for_span)("tomorrow")

        self.assertEqual([r.pk for r in rows], [wanted.pk])
        self.assertEqual(label, "Tomorrow")

    def test_unpublished_picks_never_appear(self):
        """Tier defaults to FREE on unpublished rows — published_at is the gate."""
        self._pick(0, published=False)
        rows, _ = async_to_sync(services.picks_for_span)("today")
        self.assertEqual(rows, [])

    def test_vip_picks_are_listed_too(self):
        """
        Shown rather than hidden. Both tiers are locked either way, so listing a
        VIP pick gives nothing away — and hiding it means a free user never
        learns what a subscription buys.
        """
        vip = self._pick(0, tier=Tier.VIP)
        free = self._pick(0, tier=Tier.FREE)

        rows, _ = async_to_sync(services.picks_for_span)("today")

        self.assertCountEqual([r.pk for r in rows], [vip.pk, free.pk])

    def test_a_vip_teaser_says_so_and_still_reveals_nothing(self):
        from apps.bot.handlers.picks import _teaser

        text = _teaser(self._pick(0, tier=Tier.VIP))

        self.assertIn("VIP", text)
        self.assertIn("locked", text.lower())
        self.assertNotIn("70", text)          # confidence
        self.assertNotIn("Match Result", text)

    def test_a_vip_pick_leads_with_subscribe(self):
        """Credits still work on a VIP pick; the plan is the offer it exists to make."""
        rows = [r.text for row in kb.unlock_keyboard(1, vip=True).inline_keyboard for r in row]
        self.assertIn("💳 Subscribe for unlimited", rows[0])
        self.assertTrue(any("Unlock" in r for r in rows))

        free_rows = [r.text for row in kb.unlock_keyboard(1).inline_keyboard for r in row]
        self.assertIn("Unlock", free_rows[0])

    def test_an_empty_window_returns_cleanly(self):
        rows, label = async_to_sync(services.picks_for_span)("weekend")
        self.assertEqual(rows, [])
        self.assertEqual(label, "This weekend")


class OddsSlipServiceTests(TestCase):
    def setUp(self):
        self.league = make_league()
        self.n = 0

    def _leg(self, odds, confidence, market=Market.OVER_UNDER_25):
        self.n += 1
        fixture = make_fixture(
            league=self.league,
            home=make_team(f"H{self.n}"), away=make_team(f"A{self.n}"),
            kickoff=timezone.now().replace(hour=12, minute=0, second=0, microsecond=0)
                    + timedelta(minutes=self.n),
        )
        return Prediction.objects.create(
            fixture=fixture, market=market, selection="over",
            probability=confidence / 100, confidence=confidence,
            market_odds=Decimal(str(odds)), tier=Tier.VIP, published_at=timezone.now(),
        )

    def test_returns_plain_data_the_async_handler_can_use(self):
        """No model instances cross the sync_to_async boundary — a lazy relation
        evaluated in the handler would hit the ORM from the event loop."""
        for odds in ("1.80", "1.90"):
            self._leg(odds, 75)

        slip = async_to_sync(services.build_odds_slip)("3", "today")

        self.assertTrue(slip["found"])
        self.assertEqual(slip["target"], "3 odds")
        self.assertEqual(slip["span"], "Today")
        self.assertTrue(all(isinstance(leg["home"], str) for leg in slip["legs"]))
        self.assertGreaterEqual(float(slip["odds"]), 2.5)
        self.assertLessEqual(float(slip["odds"]), 3.5)

    def test_an_unreachable_target_is_reported_not_faked(self):
        self._leg("1.10", 90)
        self._leg("1.12", 90)

        slip = async_to_sync(services.build_odds_slip)("10", "today")

        self.assertFalse(slip["found"])
        self.assertEqual(slip["target"], "10 odds")

    def test_an_unknown_target_returns_none(self):
        self.assertIsNone(async_to_sync(services.build_odds_slip)("999", "today"))

    def test_every_preset_is_wired(self):
        for key, label, low, high in kb.ODDS_TARGETS:
            slip = async_to_sync(services.build_odds_slip)(key, "today")
            self.assertIsNotNone(slip, f"preset {key} is not resolvable")
            self.assertEqual(slip["target"], label)
            self.assertLess(low, high)


class TeaserTests(TestCase):
    """What a locked pick may and may not reveal."""

    def setUp(self):
        self.prediction = Prediction.objects.create(
            fixture=make_fixture(
                league=make_league(), home=make_team("Man City"), away=make_team("Burnley"),
            ),
            market=Market.DOUBLE_CHANCE, selection="home_draw",
            probability=0.86, confidence=86,
            market_odds=Decimal("1.12"), tier=Tier.FREE, published_at=timezone.now(),
        )

    def test_locked_view_reveals_only_the_fixture(self):
        from apps.bot.handlers.picks import _teaser

        text = _teaser(self.prediction)

        self.assertIn("Man City", text)
        self.assertIn("Burnley", text)
        # None of these may appear before a credit is spent.
        self.assertNotIn("86", text)
        self.assertNotIn("Double Chance", text)
        self.assertNotIn("Man City or Draw", text)
        self.assertNotIn("1.12", text)

    def test_unlocked_view_reveals_everything(self):
        from apps.bot.handlers.picks import _full

        text = _full(self.prediction)

        self.assertIn("Man City or Draw", text)
        self.assertIn("Double Chance", text)
        self.assertIn("86", text)


class LeagueFilterTests(TestCase):
    """
    With fifty leagues configured, one flat list is unusable. Only leagues that
    actually have picks are offered — fifty entries where forty-six say "nothing
    published" is worse than four that work.
    """

    def setUp(self):
        self.epl = make_league(name="Premier League")
        self.liga = make_league(name="La Liga")
        self.n = 0

    def _pick(self, league, tier=Tier.FREE):
        self.n += 1
        fixture = make_fixture(
            league=league,
            home=make_team(f"H{self.n}"), away=make_team(f"A{self.n}"),
            kickoff=timezone.now().replace(hour=12, minute=0, second=0, microsecond=0)
                    + timedelta(minutes=self.n),
        )
        return Prediction.objects.create(
            fixture=fixture, market=Market.MATCH_RESULT, selection="home",
            probability=0.7, confidence=70, tier=tier, published_at=timezone.now(),
        )

    def test_only_leagues_with_picks_are_offered(self):
        self._pick(self.epl)
        self._pick(self.epl)
        make_league(name="Serie A")   # configured, nothing published

        leagues, _ = async_to_sync(services.leagues_for_span)("today")

        self.assertEqual([item["name"] for item in leagues], ["Premier League"])
        self.assertEqual(leagues[0]["picks"], 2)

    def test_leagues_are_ordered_by_how_much_they_have(self):
        for _ in range(3):
            self._pick(self.epl)
        self._pick(self.liga)

        leagues, _ = async_to_sync(services.leagues_for_span)("today")

        self.assertEqual([item["name"] for item in leagues], ["Premier League", "La Liga"])

    def test_filtering_returns_only_that_league(self):
        wanted = self._pick(self.epl)
        self._pick(self.liga)

        rows, _ = async_to_sync(services.picks_for_span)("today", league_id=self.epl.api_id)

        self.assertEqual([r.pk for r in rows], [wanted.pk])

    def test_no_filter_returns_everything(self):
        self._pick(self.epl)
        self._pick(self.liga)

        rows, _ = async_to_sync(services.picks_for_span)("today")
        self.assertEqual(len(rows), 2)

    def test_the_menu_pages_rather_than_listing_fifty(self):
        leagues = [
            {"api_id": i, "name": f"League {i}", "country": "X", "picks": 1}
            for i in range(20)
        ]
        first = kb.league_menu(leagues, "today", 0)
        texts = [b.text for row in first.inline_keyboard for b in row]

        self.assertIn("All leagues (20)", texts[0])
        self.assertTrue(any("More" in t for t in texts))
        self.assertFalse(any("Back" in t for t in texts), "no Back on the first page")

        second = kb.league_menu(leagues, "today", 1)
        second_texts = [b.text for row in second.inline_keyboard for b in row]
        self.assertTrue(any("Back" in t for t in second_texts))

    def test_the_day_switcher_stays_on_the_menu(self):
        """So a user can change window before committing to a league."""
        menu = kb.league_menu([{"api_id": 1, "name": "X", "country": "Y", "picks": 1}],
                              "today", 0)
        texts = [b.text for row in menu.inline_keyboard for b in row]
        self.assertTrue(any("Tomorrow" in t for t in texts))


class WebhookShowTests(TestCase):
    """
    `--show` is what deploy.sh asks "is the webhook up?". Grepping its prose
    reported a healthy webhook as missing for a week, so the answer is the exit
    code now.
    """

    def _show(self, url, last_error=None):
        from io import StringIO
        from unittest.mock import AsyncMock, patch

        from django.core.management import call_command

        info = type("Info", (), {
            "url": url, "pending_update_count": 0, "has_custom_certificate": False,
            "last_error_message": last_error, "last_error_date": "2026-09-14",
        })()
        bot = AsyncMock()
        bot.get_webhook_info = AsyncMock(return_value=info)
        bot.session.close = AsyncMock()

        out = StringIO()
        with patch("apps.bot.management.commands.set_webhook.get_bot", return_value=bot):
            call_command("set_webhook", "--show", stdout=out)
        return out.getvalue()

    def test_a_registered_webhook_succeeds(self):
        output = self._show("https://example.com/hooks/telegram/")
        self.assertIn("https://example.com/hooks/telegram/", output)

    def test_no_webhook_raises(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            self._show("")

    def test_a_stale_error_does_not_fail_the_check(self):
        """
        Telegram keeps the last error until a delivery succeeds, so one from days
        ago says nothing about now — and must not be read as an outage.
        """
        output = self._show(
            "https://example.com/hooks/telegram/",
            last_error="Wrong response from the webhook: 502 Bad Gateway",
        )
        self.assertIn("sticky", output)
        self.assertIn("502", output)
