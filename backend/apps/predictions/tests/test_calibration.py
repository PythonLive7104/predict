"""
Probability calibration.

Measured over 22,660 graded picks: every confidence band came in 5-13 points
below its claim, linearly. The product is sold on that figure meaning what it
says, so this is the difference between a defensible claim and one the public
record eventually disproves.
"""

from django.test import SimpleTestCase, override_settings

from apps.predictions.engine import calibration, poisson
from apps.predictions.engine.pipeline import candidates_from


@override_settings(CALIBRATION_SLOPE=0.808, CALIBRATION_INTERCEPT=0.0411)
class CalibrateTests(SimpleTestCase):
    def test_it_pulls_confident_claims_down(self):
        """The whole point: an 80% claim that wins 69% should not say 80%."""
        self.assertLess(calibration.calibrate(0.80), 0.80)
        self.assertAlmostEqual(calibration.calibrate(0.80), 0.687, places=2)

    def test_the_measured_bands_come_out_right(self):
        """Fitted against the real backtest, so it should reproduce it."""
        for claimed, actual in ((0.62, 0.542), (0.77, 0.663), (0.919, 0.784)):
            self.assertAlmostEqual(calibration.calibrate(claimed), actual, places=2)

    def test_it_is_monotonic(self):
        """
        A calibration that reordered picks would change which side is favoured,
        which is a different model rather than an honest version of this one.
        """
        values = [calibration.calibrate(p / 100) for p in range(50, 100)]
        self.assertEqual(values, sorted(values))

    def test_nothing_is_ever_certain(self):
        """A model claiming 98% is describing its own arithmetic, not a match."""
        self.assertLessEqual(calibration.calibrate(0.999), calibration.CEILING)
        self.assertGreaterEqual(calibration.calibrate(0.0001), calibration.FLOOR)

    @override_settings(CALIBRATION_SLOPE=1.0, CALIBRATION_INTERCEPT=0.0)
    def test_it_can_be_turned_off(self):
        self.assertEqual(calibration.calibrate(0.77), 0.77)


@override_settings(CALIBRATION_SLOPE=0.808, CALIBRATION_INTERCEPT=0.0411)
class CandidateCalibrationTests(SimpleTestCase):
    def test_published_confidence_is_the_calibrated_one(self):
        probs = poisson.market_probabilities(1.9, 1.0)
        candidates = {c.market: c for c in candidates_from(probs)}

        # Double chance is the market that most overstated itself.
        dc = candidates["dc"]
        self.assertLess(dc.probability, probs.home + probs.draw)

    def test_calibration_does_not_change_which_side_is_picked(self):
        """Monotonic, so the favourite stays the favourite."""
        probs = poisson.market_probabilities(2.2, 0.9)
        picked = {c.market: c.selection for c in candidates_from(probs)}

        self.assertEqual(picked["1x2"], "home")
        self.assertEqual(picked["dc"], "home_draw")

    @override_settings(CALIBRATION_SLOPE=1.0, CALIBRATION_INTERCEPT=0.0)
    def test_disabled_calibration_returns_the_raw_model(self):
        probs = poisson.market_probabilities(1.9, 1.0)
        candidates = {c.market: c for c in candidates_from(probs)}
        self.assertAlmostEqual(candidates["1x2"].probability, probs.home, places=6)

    def test_the_publish_gate_bites_harder_once_calibrated(self):
        """
        Fewer picks clear 55% — correctly. A raw 63% is a calibrated 55%, so the
        gate now admits only what the model genuinely earns.
        """
        probs = poisson.market_probabilities(1.6, 1.3)
        raw = {c.market: c.probability for c in candidates_from(probs)}

        with override_settings(CALIBRATION_SLOPE=1.0, CALIBRATION_INTERCEPT=0.0):
            uncalibrated = {c.market: c.probability for c in candidates_from(probs)}

        for market in raw:
            self.assertLessEqual(raw[market], uncalibrated[market] + 1e-9)
