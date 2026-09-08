from datetime import timedelta
from decimal import Decimal

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.predictions.models import Market, Outcome, Prediction, Slip, Tier
from apps.predictions.publishing import build_slips, publish_daily, settle_slips

from .factories import make_fixture, make_league, make_team


def make_prediction(fixture, market=Market.MATCH_RESULT, selection="home",
                    confidence=70, odds="1.80"):
    return Prediction.objects.create(
        fixture=fixture, market=market, selection=selection,
        probability=confidence / 100, confidence=confidence,
        market_odds=Decimal(odds),
    )


@override_settings(MIN_PUBLISH_CONFIDENCE=55)
class PublishTests(TestCase):
    def setUp(self):
        self.league = make_league()
        self.today = timezone.now().date()

    def _fixture(self):
        return make_fixture(
            league=self.league,
            home=make_team(f"H{timezone.now().microsecond}"),
            away=make_team(f"A{timezone.now().microsecond}"),
        )

    def test_best_picks_go_free_and_the_rest_go_vip(self):
        # Distinct markets, so this exercises the confidence ranking rather than
        # the one-per-market diversity rule (covered in FreeSlateDiversityTests).
        markets = [
            (Market.DOUBLE_CHANCE, "home_draw"),
            (Market.MATCH_RESULT, "home"),
            (Market.OVER_UNDER_25, "over"),
            (Market.BTTS, "yes"),
            (Market.CORRECT_SCORE, "2-1"),
        ]
        for confidence, (market, selection) in zip((90, 85, 80, 75, 70), markets):
            make_prediction(
                self._fixture(), market=market, selection=selection, confidence=confidence
            )

        result = publish_daily(free_count=3)

        self.assertEqual(result["free"], 3)
        self.assertEqual(result["vip"], 2)

        free = Prediction.objects.filter(
            tier=Tier.FREE, published_at__isnull=False
        ).order_by("-confidence")
        self.assertEqual([p.confidence for p in free], [90, 85, 80])

    def test_low_confidence_picks_are_never_published(self):
        make_prediction(self._fixture(), confidence=40)
        make_prediction(self._fixture(), confidence=80)

        result = publish_daily()

        self.assertEqual(result["total"], 1)
        self.assertEqual(Prediction.objects.filter(published_at__isnull=False).count(), 1)

    def test_free_tier_shows_breadth_not_one_fixture_from_every_angle(self):
        """Multiple markets on one match must not fill the whole free slate."""
        fixture = self._fixture()
        make_prediction(fixture, market=Market.MATCH_RESULT, confidence=95)
        make_prediction(fixture, market=Market.OVER_UNDER_25, selection="over", confidence=94)
        make_prediction(fixture, market=Market.BTTS, selection="yes", confidence=93)
        make_prediction(self._fixture(), confidence=70)

        publish_daily(free_count=3)

        free_fixtures = Prediction.objects.filter(
            tier=Tier.FREE, published_at__isnull=False
        ).values_list("fixture_id", flat=True)
        self.assertEqual(len(set(free_fixtures)), len(free_fixtures))

    def test_republishing_does_not_move_an_already_published_pick(self):
        make_prediction(self._fixture(), confidence=90)
        publish_daily()
        first = publish_daily()
        self.assertEqual(first["total"], 0)


@override_settings(MIN_PUBLISH_CONFIDENCE=55)
class SlipTests(TestCase):
    def setUp(self):
        self.league = make_league()
        self.today = timezone.now().date()

    def _published(self, confidence, odds, market=Market.MATCH_RESULT, selection="home"):
        fixture = make_fixture(
            league=self.league,
            home=make_team(f"H{timezone.now().microsecond}{confidence}"),
            away=make_team(f"A{timezone.now().microsecond}{confidence}"),
        )
        prediction = make_prediction(
            fixture, market=market, selection=selection,
            confidence=confidence, odds=odds,
        )
        prediction.publish(tier=Tier.VIP)
        return prediction

    def test_banker_is_the_single_most_confident_pick(self):
        self._published(72, "1.60")
        best = self._published(88, "1.35")
        self._published(80, "1.45")

        build_slips()
        banker = Slip.objects.get(kind=Slip.Kind.BANKER)

        self.assertEqual(list(banker.predictions.all()), [best])
        self.assertEqual(banker.total_odds, Decimal("1.350"))

    def test_two_odds_slip_clears_the_target(self):
        for _ in range(4):
            self._published(80, "1.40")

        build_slips()
        slip = Slip.objects.get(kind=Slip.Kind.TWO_ODDS)

        self.assertGreaterEqual(slip.total_odds, Decimal("2.0"))
        # 1.4 * 1.4 = 1.96 (short), 1.4^3 = 2.744 -> three legs.
        self.assertEqual(slip.predictions.count(), 3)

    def test_slip_legs_never_come_from_the_same_fixture(self):
        """Correlated legs make the combined odds a lie about the real risk."""
        fixture = make_fixture(league=self.league)
        for market, selection in (
            (Market.MATCH_RESULT, "home"),
            (Market.DOUBLE_CHANCE, "home_draw"),
            (Market.OVER_UNDER_25, "over"),
        ):
            p = make_prediction(fixture, market=market, selection=selection,
                                confidence=85, odds="1.50")
            p.publish(tier=Tier.VIP)

        build_slips()
        banker = Slip.objects.get(kind=Slip.Kind.BANKER)
        self.assertEqual(banker.predictions.count(), 1)

    def test_low_confidence_picks_are_excluded_from_slips(self):
        self._published(58, "2.20")  # published, but below the slip gate
        build_slips()
        self.assertEqual(Slip.objects.count(), 0)

    def test_thin_slate_produces_no_padded_slip(self):
        self.assertEqual(build_slips(), [])
        self.assertEqual(Slip.objects.count(), 0)

    def test_rebuilding_updates_in_place_rather_than_duplicating(self):
        for _ in range(4):
            self._published(80, "1.40")

        build_slips()
        build_slips()

        self.assertEqual(Slip.objects.filter(kind=Slip.Kind.BANKER).count(), 1)


@override_settings(MIN_PUBLISH_CONFIDENCE=55)
class SlipSettlementTests(TestCase):
    def setUp(self):
        self.league = make_league()

    def _leg(self, outcome):
        fixture = make_fixture(league=self.league)
        prediction = make_prediction(fixture, confidence=80)
        prediction.publish(tier=Tier.VIP)
        prediction.outcome = outcome
        prediction.save()
        return prediction

    def _slip(self, legs):
        slip = Slip.objects.create(
            kind=Slip.Kind.ACCUMULATOR, title="Test", for_date=timezone.now().date(),
            published_at=timezone.now(),
        )
        slip.predictions.set(legs)
        return slip

    def test_one_lost_leg_sinks_the_slip(self):
        slip = self._slip([self._leg(Outcome.WON), self._leg(Outcome.LOST)])
        self.assertEqual(settle_slips(), 1)
        slip.refresh_from_db()
        self.assertEqual(slip.outcome, Outcome.LOST)

    def test_all_legs_won_wins_the_slip(self):
        slip = self._slip([self._leg(Outcome.WON), self._leg(Outcome.WON)])
        settle_slips()
        slip.refresh_from_db()
        self.assertEqual(slip.outcome, Outcome.WON)

    def test_slip_stays_pending_while_legs_are_in_play(self):
        slip = self._slip([self._leg(Outcome.WON), self._leg(Outcome.PENDING)])
        self.assertEqual(settle_slips(), 0)
        slip.refresh_from_db()
        self.assertEqual(slip.outcome, Outcome.PENDING)

    def test_a_lost_leg_settles_immediately_even_with_legs_outstanding(self):
        """Nothing later can save it, so don't make the user wait to find out."""
        slip = self._slip([self._leg(Outcome.LOST), self._leg(Outcome.PENDING)])
        self.assertEqual(settle_slips(), 1)
        slip.refresh_from_db()
        self.assertEqual(slip.outcome, Outcome.LOST)


@override_settings(MIN_PUBLISH_CONFIDENCE=55)
class FreeSlateDiversityTests(TestCase):
    """
    Double Chance is ~85-92% by construction, so a pure confidence ranking gives
    it every free slot. The free slate must span markets.
    """

    def setUp(self):
        self.league = make_league()

    def _fixture(self):
        stamp = timezone.now().microsecond
        return make_fixture(
            league=self.league,
            home=make_team(f"H{stamp}{id(self)}"),
            away=make_team(f"A{stamp}{id(self)}"),
        )

    def test_free_slate_does_not_fill_with_one_market(self):
        # Three fixtures, each with a high-confidence DC and a decent 1X2.
        for _ in range(3):
            fixture = self._fixture()
            make_prediction(fixture, market=Market.DOUBLE_CHANCE,
                            selection="home_draw", confidence=91, odds="1.12")
            make_prediction(fixture, market=Market.MATCH_RESULT,
                            selection="home", confidence=74, odds="1.75")
            make_prediction(fixture, market=Market.OVER_UNDER_25,
                            selection="over", confidence=68, odds="1.80")

        publish_daily(free_count=3)

        markets = list(
            Prediction.objects.filter(
                tier=Tier.FREE, published_at__isnull=False
            ).values_list("market", flat=True)
        )
        self.assertEqual(len(markets), 3)
        self.assertEqual(len(set(markets)), 3, f"free slate collapsed onto {markets}")

    def test_everything_not_chosen_for_free_still_goes_vip(self):
        fixture = self._fixture()
        make_prediction(fixture, market=Market.DOUBLE_CHANCE,
                        selection="home_draw", confidence=91, odds="1.12")
        make_prediction(fixture, market=Market.MATCH_RESULT,
                        selection="home", confidence=74, odds="1.75")

        result = publish_daily(free_count=3)

        # Same fixture, so only one can go free — the other must not be dropped.
        self.assertEqual(result["free"], 1)
        self.assertEqual(result["vip"], 1)
        self.assertEqual(Prediction.objects.filter(published_at__isnull=False).count(), 2)
