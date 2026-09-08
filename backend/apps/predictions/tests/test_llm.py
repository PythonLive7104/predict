"""
Narrative layer contract.

The engine publishes picks with or without prose, so every failure path here has
to surface as a clean exception the pipeline can swallow — never a stack trace
from json.loads, and never a half-built dict reaching a user.

Payloads below are plain dicts because that is what both paths actually parse:
the sync call via `model_dump()`, the batch via raw JSON from the output file.
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from apps.predictions.engine import llm

GOOD = {
    "rationale": "Home side score freely and the visitors leak on the road.",
    "key_factors": ["1.45 home attack", "away defence 1.31"],
    "caveats": [],
    "context_risk": "low",
}


def _payload(status="completed", block=None, incomplete_reason=None):
    block = block or {"type": "output_text", "text": json.dumps(GOOD)}
    return {
        "status": status,
        "incomplete_details": {"reason": incomplete_reason},
        "output": [{"type": "message", "content": [block]}],
    }


@override_settings(OPENAI_API_KEY="sk-test", PREDICTION_MODEL="gpt-5.6-sol")
class RequestBodyTests(SimpleTestCase):
    def _body(self):
        return llm.request_body({"home": {}}, {"1x2": {}}, "home", "1x2")

    def test_schema_is_strict_enough_for_the_api_to_accept(self):
        """Strict mode rejects a schema missing either of these, at request time."""
        fmt = self._body()["text"]["format"]
        self.assertEqual(fmt["type"], "json_schema")
        self.assertTrue(fmt["strict"])
        self.assertFalse(fmt["schema"]["additionalProperties"])
        self.assertCountEqual(
            fmt["schema"]["required"],
            ["rationale", "key_factors", "caveats", "context_risk"],
        )

    def test_selection_is_handed_over_as_settled_not_asked(self):
        system, user = self._body()["input"]
        self.assertEqual(system["role"], "system")
        self.assertIn("The model owns the numbers; you own the explanation.", system["content"])
        self.assertEqual(json.loads(user["content"])["model_selection"], "home")

    def test_display_labels_are_sent_alongside_the_storage_keys(self):
        """
        Sending only the raw key makes the model write the key — observed as
        "Home draw is supported by..." where the bot says "Man City or Draw".
        Same pick, two voices.
        """
        body = llm.request_body(
            {"home": {}}, {"dc": {}}, "home_draw", "dc",
            "Man City or Draw", "Double Chance",
        )
        payload = json.loads(body["input"][1]["content"])

        self.assertEqual(payload["model_selection"], "Man City or Draw")
        self.assertEqual(payload["market"], "Double Chance")
        # The keys stay available so the model can still reason about the market.
        self.assertEqual(payload["model_selection_key"], "home_draw")
        self.assertEqual(payload["market_key"], "dc")

    def test_falls_back_to_the_raw_key_when_no_label_is_supplied(self):
        payload = json.loads(self._body()["input"][1]["content"])
        self.assertEqual(payload["model_selection"], "home")

    def test_sync_and_batch_send_an_identical_body(self):
        """
        Two request builders would let the prose drift between a manual re-run
        and the nightly batch, and the public record would have two voices.
        """
        with patch.object(llm, "_client") as client:
            client.return_value.responses.create.return_value = SimpleNamespace(
                model_dump=lambda: _payload()
            )
            llm.write_rationale({"home": {}}, {"1x2": {}}, "home", "1x2")
            sync_body = client.return_value.responses.create.call_args.kwargs

        self.assertEqual(sync_body, self._body())


@override_settings(OPENAI_API_KEY="sk-test", PREDICTION_MODEL="gpt-5.6-sol")
class ParseResponseTests(SimpleTestCase):
    def test_returns_the_parsed_object(self):
        self.assertEqual(llm.parse_response(_payload()), GOOD)

    def test_refusal_raises_rather_than_returning_prose(self):
        refusal = {"type": "refusal", "refusal": "declined"}
        with self.assertRaises(RuntimeError):
            llm.parse_response(_payload(block=refusal))

    def test_truncation_raises_instead_of_a_json_error(self):
        """
        Reasoning tokens count against max_output_tokens, so a long think can eat
        the budget before the JSON is written.
        """
        with self.assertRaises(RuntimeError) as ctx:
            llm.parse_response(_payload(status="incomplete", incomplete_reason="max_output_tokens"))
        self.assertIn("max_output_tokens", str(ctx.exception))

    def test_empty_output_raises(self):
        with self.assertRaises(RuntimeError):
            llm.parse_response({"status": "completed", "output": []})

    def test_missing_key_is_a_clean_error(self):
        with override_settings(OPENAI_API_KEY=""):
            with self.assertRaises(RuntimeError):
                llm.write_rationale({}, {}, "home", "1x2")


@override_settings(OPENAI_API_KEY="sk-test", PREDICTION_MODEL="gpt-5.6-sol")
class BatchTests(SimpleTestCase):
    def test_jsonl_is_one_line_per_request_keyed_for_matching(self):
        with patch.object(llm, "_client") as client:
            client.return_value.files.create.return_value = SimpleNamespace(id="file-1")
            client.return_value.batches.create.return_value = SimpleNamespace(
                id="batch-1",
                model_dump=lambda: {"id": "batch-1", "status": "validating"},
            )
            body = llm.request_body({}, {}, "home", "1x2")
            llm.submit_batch([("pred-7", body), ("pred-9", body)])
            blob = client.return_value.files.create.call_args.kwargs["file"]

        _, raw, _ = blob
        lines = [json.loads(l) for l in raw.decode().strip().splitlines()]
        self.assertEqual([l["custom_id"] for l in lines], ["pred-7", "pred-9"])
        self.assertEqual({l["url"] for l in lines}, {"/v1/responses"})
        self.assertEqual(lines[0]["body"], body)

    def test_empty_batch_is_refused_rather_than_submitted(self):
        with self.assertRaises(ValueError):
            llm.submit_batch([])

    def test_results_are_keyed_by_custom_id_not_position(self):
        """Output line order does not match input order."""
        lines = [
            {"custom_id": "pred-9", "error": None,
             "response": {"status_code": 200, "body": _payload()}},
            {"custom_id": "pred-7", "error": None,
             "response": {"status_code": 200, "body": _payload()}},
        ]
        with patch.object(llm, "_client") as client:
            client.return_value.files.content.return_value = SimpleNamespace(
                text="\n".join(json.dumps(l) for l in lines)
            )
            out = llm.fetch_batch_results("file-out")

        self.assertEqual(sorted(out), ["pred-7", "pred-9"])
        self.assertEqual(out["pred-7"], GOOD)

    def test_one_bad_line_does_not_cost_the_whole_batch(self):
        lines = [
            {"custom_id": "pred-1", "error": {"message": "boom"}, "response": None},
            {"custom_id": "pred-2", "error": None, "response": {"status_code": 500, "body": {}}},
            {"custom_id": "pred-3", "error": None,
             "response": {"status_code": 200,
                          "body": _payload(block={"type": "refusal", "refusal": "no"})}},
            {"custom_id": "pred-4", "error": None,
             "response": {"status_code": 200, "body": _payload()}},
        ]
        with patch.object(llm, "_client") as client:
            client.return_value.files.content.return_value = SimpleNamespace(
                text="\n".join(json.dumps(l) for l in lines)
            )
            out = llm.fetch_batch_results("file-out")

        self.assertEqual(list(out), ["pred-4"])
