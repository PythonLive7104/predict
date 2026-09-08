"""
Batch lifecycle.

Prose is bought asynchronously, which introduces a failure mode the synchronous
path never had: a pick can be claimed by a batch that never returns, and then be
skipped by every future run. These pin the claim/release cycle that prevents it.
"""

from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.fixtures.models import Fixture
from apps.predictions.engine.pipeline import ENGINE_VERSION
from apps.predictions.models import Market, Prediction, RationaleBatch
from apps.predictions.tasks import (
    collect_rationale_batches,
    predictions_awaiting_prose,
    submit_rationale_batch,
)

from .factories import make_fixture

READ = {
    "rationale": "They score, the visitors leak.",
    "key_factors": ["form"],
    "caveats": [],
    "context_risk": "low",
}


def _prediction(confidence=70, rationale="", **kw):
    fixture = make_fixture(
        kickoff=timezone.now() + timezone.timedelta(hours=6),
        status=Fixture.Status.SCHEDULED,
    )
    return Prediction.objects.create(
        fixture=fixture, market=Market.MATCH_RESULT, selection="home",
        probability=confidence / 100, confidence=confidence, rationale=rationale,
        engine_version=ENGINE_VERSION, stat_snapshot={"model_output": {}}, **kw,
    )


@override_settings(MIN_PUBLISH_CONFIDENCE=55, OPENAI_API_KEY="sk-test")
class SelectionTests(TestCase):
    def test_only_publishable_prose_less_picks_are_queued(self):
        wanted = _prediction(confidence=70)
        _prediction(confidence=40)                      # below the publish gate
        _prediction(confidence=70, rationale="already") # already has a read

        self.assertEqual(list(predictions_awaiting_prose()), [wanted])

    def test_a_pick_already_in_a_batch_is_not_queued_again(self):
        batch = RationaleBatch.objects.create(
            provider_batch_id="b-1", requested=1, for_date=timezone.now().date()
        )
        _prediction(confidence=70, rationale_batch=batch)
        self.assertEqual(list(predictions_awaiting_prose()), [])


@override_settings(MIN_PUBLISH_CONFIDENCE=55, OPENAI_API_KEY="sk-test")
class SubmitTests(TestCase):
    @patch("apps.predictions.tasks.llm.submit_batch")
    def test_submission_claims_the_picks(self, submit):
        submit.return_value = {"id": "b-9", "status": "validating", "input_file_id": "f-1"}
        prediction = _prediction()

        self.assertEqual(submit_rationale_batch(), 1)

        prediction.refresh_from_db()
        self.assertEqual(prediction.rationale_batch.provider_batch_id, "b-9")
        self.assertEqual(RationaleBatch.objects.get().requested, 1)

    @patch("apps.predictions.tasks.llm.submit_batch")
    def test_a_failed_submission_leaves_the_picks_free_to_retry(self, submit):
        """Claiming before the provider accepts would strand them silently."""
        submit.side_effect = RuntimeError("provider down")
        prediction = _prediction()

        self.assertEqual(submit_rationale_batch(), 0)

        prediction.refresh_from_db()
        self.assertIsNone(prediction.rationale_batch)
        self.assertFalse(RationaleBatch.objects.exists())

    @patch("apps.predictions.tasks.llm.submit_batch")
    def test_nothing_to_do_is_not_an_empty_submission(self, submit):
        self.assertEqual(submit_rationale_batch(), 0)
        submit.assert_not_called()


@override_settings(MIN_PUBLISH_CONFIDENCE=55, OPENAI_API_KEY="sk-test")
class CollectTests(TestCase):
    def setUp(self):
        self.batch = RationaleBatch.objects.create(
            provider_batch_id="b-1", requested=1, for_date=timezone.now().date(),
            status=RationaleBatch.Status.IN_PROGRESS,
        )
        self.prediction = _prediction(rationale_batch=self.batch)

    @patch("apps.predictions.tasks.llm.fetch_batch_results")
    @patch("apps.predictions.tasks.llm.poll_batch")
    def test_completed_batch_backfills_prose_onto_the_pick(self, poll, fetch):
        poll.return_value = {"status": "completed", "output_file_id": "out-1"}
        fetch.return_value = {f"pred-{self.prediction.pk}": READ}

        self.assertEqual(collect_rationale_batches(), 1)

        self.prediction.refresh_from_db()
        self.assertEqual(self.prediction.rationale, READ["rationale"])
        # The frozen pack survives; the read is layered on top.
        self.assertEqual(self.prediction.stat_snapshot["llm"], READ)
        self.assertIn("model_output", self.prediction.stat_snapshot)

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.collected, 1)
        self.assertIsNotNone(self.batch.collected_at)

    @patch("apps.predictions.tasks.llm.poll_batch")
    def test_a_dead_batch_releases_its_picks_for_retry(self, poll):
        """
        Otherwise the FK stays set, `predictions_awaiting_prose` keeps skipping
        them, and those picks are mute forever.
        """
        poll.return_value = {"status": "expired"}

        collect_rationale_batches()

        self.prediction.refresh_from_db()
        self.assertIsNone(self.prediction.rationale_batch)
        self.assertIn(self.prediction, predictions_awaiting_prose())

    @patch("apps.predictions.tasks.llm.poll_batch")
    def test_an_open_batch_is_left_alone(self, poll):
        poll.return_value = {"status": "in_progress"}

        self.assertEqual(collect_rationale_batches(), 0)

        self.prediction.refresh_from_db()
        self.assertEqual(self.prediction.rationale, "")
        self.assertEqual(self.prediction.rationale_batch, self.batch)

    @patch("apps.predictions.tasks.llm.poll_batch")
    def test_a_polling_failure_does_not_disturb_the_batch(self, poll):
        poll.side_effect = RuntimeError("network")

        self.assertEqual(collect_rationale_batches(), 0)

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, RationaleBatch.Status.IN_PROGRESS)
        self.prediction.refresh_from_db()
        self.assertEqual(self.prediction.rationale_batch, self.batch)

    @patch("apps.predictions.tasks.llm.fetch_batch_results")
    @patch("apps.predictions.tasks.llm.poll_batch")
    def test_a_missing_result_leaves_that_pick_prose_less_not_broken(self, poll, fetch):
        poll.return_value = {"status": "completed", "output_file_id": "out-1"}
        fetch.return_value = {}

        self.assertEqual(collect_rationale_batches(), 0)

        self.prediction.refresh_from_db()
        self.assertEqual(self.prediction.rationale, "")


@override_settings(MIN_PUBLISH_CONFIDENCE=55, LLM_BATCH_ENABLED=True)
class GenerationHandoffTests(TestCase):
    @patch("apps.predictions.tasks.submit_rationale_batch.delay")
    @patch("apps.predictions.tasks.generate_for_fixture")
    def test_generation_makes_no_api_calls_in_batch_mode(self, generate, delay):
        generate.return_value = []
        make_fixture(
            kickoff=timezone.now() + timezone.timedelta(hours=6),
            status=Fixture.Status.SCHEDULED,
        )
        from apps.predictions.tasks import generate_daily_predictions

        generate_daily_predictions()

        self.assertFalse(generate.call_args.kwargs["with_rationale"])
        delay.assert_called_once()

    @patch("apps.predictions.tasks.submit_rationale_batch.delay")
    @patch("apps.predictions.tasks.generate_for_fixture")
    def test_a_broker_outage_does_not_sink_a_priced_slate(self, generate, delay):
        generate.return_value = []
        delay.side_effect = RuntimeError("broker unreachable")
        make_fixture(
            kickoff=timezone.now() + timezone.timedelta(hours=6),
            status=Fixture.Status.SCHEDULED,
        )
        from apps.predictions.tasks import generate_daily_predictions

        generate_daily_predictions()  # must not raise
