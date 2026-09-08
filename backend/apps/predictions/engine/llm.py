"""
Narrative layer — the model reads the same stat pack the Poisson engine saw and
writes the user-facing read.

It does NOT set the probability. The number comes from poisson.py so the public
accuracy record stays defensible; the model's job is to explain that number in
football language and to flag context the engine structurally cannot see (a cup
final, a manager sacked on Thursday, a keeper ruled out after the data pull).
Where it disagrees strongly with the numbers, that's surfaced as a caveat, not
as an override.

Because this layer owns no numbers, the provider behind it is a cost and voice
decision rather than a modelling one, and a failure here is non-fatal: callers
publish the pick without prose.
"""

import json
import logging

import openai
from django.conf import settings

logger = logging.getLogger(__name__)

# Frozen across every call. Too short to hit any provider's automatic prompt
# cache (those start around 1k tokens), so this is about consistency of voice,
# not cost — the per-fixture pack that follows is what varies and it could never
# be cached anyway.
ANALYST_BRIEF = """You are a football analyst writing the short read that ships \
alongside a statistical model's pick.

You are given a stat pack and the model's calibrated probabilities. The model owns \
the numbers; you own the explanation.

Rules:
- Never contradict the model's selection. If the numbers look wrong to you, say so \
in `caveats` — do not pick something else.
- Never promise, guarantee, or imply certainty. No "sure", "banker", "guaranteed", \
"100%". The confidence figure speaks for itself.
- Ground every claim in the pack. If a fact isn't in the pack, don't assert it.
- Write for a bettor reading on a phone: 2-3 sentences, concrete, no filler.
- `caveats` is where availability gaps, thin samples, and rotation risk go. An \
empty list is fine, but say so honestly rather than inventing balance."""

# `additionalProperties: false` plus a complete `required` list is what strict
# mode demands — without both, the API rejects the schema rather than silently
# relaxing it.
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "rationale": {
            "type": "string",
            "description": "2-3 sentences explaining the pick, for the end user.",
        },
        "key_factors": {
            "type": "array",
            "items": {"type": "string"},
            "description": "3-5 short bullet phrases drawn from the stat pack.",
        },
        "caveats": {
            "type": "array",
            "items": {"type": "string"},
            "description": "What could invalidate this read. May be empty.",
        },
        "context_risk": {
            "type": "string",
            "enum": ["low", "medium", "high"],
            "description": "How much non-statistical context the model is missing.",
        },
    },
    "required": ["rationale", "key_factors", "caveats", "context_risk"],
    "additionalProperties": False,
}

# Reasoning tokens are billed as output and count against this ceiling, so it
# has to cover the thinking as well as the ~200-token JSON body. Too low and the
# response truncates mid-object, json.loads throws, and we have paid for a call
# that produced nothing.
MAX_OUTPUT_TOKENS = 4000


def _client() -> openai.OpenAI:
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set")
    return openai.OpenAI(api_key=settings.OPENAI_API_KEY)


def request_body(
    stat_pack: dict,
    model_output: dict,
    selection: str,
    market: str,
    selection_label: str = "",
    market_label: str = "",
) -> dict:
    """
    The single definition of a rationale request.

    Both paths render through here — the synchronous call and the batch JSONL —
    so the prose cannot drift between them. A pick written inline during a manual
    re-run must read the same as one written by the nightly batch, or the public
    record has two voices.
    """
    # Both the storage key and the display string go in. Sending only the key
    # means the model writes the key: "Home draw is supported by..." instead of
    # "Man City or Draw". The bot renders the label, so prose that uses the key
    # reads like a different product.
    payload = {
        "market": market_label or market,
        "market_key": market,
        "model_selection": selection_label or selection,
        "model_selection_key": selection,
        "model_probabilities": model_output,
        "stat_pack": stat_pack,
    }
    return {
        "model": settings.PREDICTION_MODEL,
        "input": [
            {"role": "system", "content": ANALYST_BRIEF},
            {
                "role": "user",
                "content": json.dumps(payload, separators=(",", ":"), sort_keys=True),
            },
        ],
        "reasoning": {"effort": "medium"},
        "text": {
            "format": {
                "type": "json_schema",
                "name": "prediction_rationale",
                "strict": True,
                "schema": RESPONSE_SCHEMA,
            }
        },
        "max_output_tokens": MAX_OUTPUT_TOKENS,
    }


def parse_response(data: dict) -> dict:
    """
    Pull the rationale object out of a Responses API payload.

    Takes a plain dict so the synchronous path (via `model_dump()`) and the batch
    path (raw JSON from the output file) are parsed by the same code — including
    the failure cases, which is where two parsers would quietly diverge.
    """
    if data.get("status") == "incomplete":
        # Reasoning tokens bill as output and count against max_output_tokens, so
        # a long think can eat the budget before the JSON is written. Surface it
        # as a clean error rather than a json.loads traceback.
        reason = (data.get("incomplete_details") or {}).get("reason")
        raise RuntimeError(f"rationale incomplete: {reason}")

    for item in data.get("output", []):
        if item.get("type") != "message":
            continue
        for block in item.get("content", []):
            if block.get("type") == "refusal":
                raise RuntimeError(f"rationale refused: {block.get('refusal')}")
            if block.get("type") == "output_text":
                return json.loads(block["text"])

    raise RuntimeError("rationale response carried no text")


def write_rationale(
    stat_pack: dict, model_output: dict, selection: str, market: str,
    selection_label: str = "", market_label: str = "",
) -> dict:
    """
    Synchronous path. Returns {rationale, key_factors, caveats, context_risk}.

    Callers must treat a failure here as non-fatal: a pick with no prose is still
    a publishable pick, so never let the narrative layer block the pipeline.
    """
    response = _client().responses.create(
        **request_body(stat_pack, model_output, selection, market,
                       selection_label, market_label)
    )
    return parse_response(response.model_dump())


# --- Batch path -----------------------------------------------------------
#
# Half price, and the daily generation run is exactly the workload it suits:
# scheduled, not latency-sensitive, and nobody is waiting on the output. The
# tradeoff is that results are only guaranteed within the completion window, so
# callers must publish without prose and backfill later.

BATCH_ENDPOINT = "/v1/responses"


def submit_batch(items: list[tuple[str, dict]], description: str = "") -> dict:
    """
    Upload a JSONL of rationale requests and start a batch.

    `items` is [(custom_id, request_body)]. Returns the provider batch object as
    a dict. Raises if the batch cannot be created — the caller decides whether
    that is fatal (it isn't; the picks just ship without prose).
    """
    if not items:
        raise ValueError("refusing to submit an empty batch")

    lines = [
        json.dumps(
            {"custom_id": cid, "method": "POST", "url": BATCH_ENDPOINT, "body": body},
            separators=(",", ":"),
        )
        for cid, body in items
    ]
    blob = ("rationales.jsonl", ("\n".join(lines) + "\n").encode(), "application/jsonl")

    client = _client()
    upload = client.files.create(file=blob, purpose="batch")
    batch = client.batches.create(
        input_file_id=upload.id,
        endpoint=BATCH_ENDPOINT,
        completion_window=settings.LLM_BATCH_COMPLETION_WINDOW,
        metadata={"description": description} if description else None,
    )
    logger.info("submitted rationale batch %s (%s requests)", batch.id, len(items))
    return batch.model_dump()


def poll_batch(batch_id: str) -> dict:
    return _client().batches.retrieve(batch_id).model_dump()


def fetch_batch_results(output_file_id: str) -> dict[str, dict]:
    """
    custom_id -> parsed rationale, for the lines that succeeded.

    Output line order does not match input order, so everything is keyed on
    custom_id. A line that failed or refused is logged and omitted rather than
    raising: one bad rationale must not cost the whole batch.
    """
    text = _client().files.content(output_file_id).text
    out: dict[str, dict] = {}

    for line in text.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        custom_id = row.get("custom_id")

        if row.get("error"):
            logger.warning("batch line %s errored: %s", custom_id, row["error"])
            continue

        response = row.get("response") or {}
        if response.get("status_code") != 200:
            logger.warning("batch line %s http %s", custom_id, response.get("status_code"))
            continue

        try:
            out[custom_id] = parse_response(response.get("body") or {})
        except Exception:
            logger.exception("batch line %s unparseable", custom_id)

    return out
