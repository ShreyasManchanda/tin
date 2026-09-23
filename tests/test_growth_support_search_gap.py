"""Offline tests for the growth.support_search_gap code package.

Mirrors the pattern used for example.feedback_digest in test_public_workflows.py
and growth.testimonial_miner's own tests: load the real manifest and main.py
from disk, validate the manifest against the code contract, and drive `run`
with stub `ctx.services.call` / `ctx.models.generate` so no network, model
credentials or live Search Console/Gmail connection is needed.
"""

import json
import re
import runpy
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest

from tin_lite.code_models import request_contract
from tin_lite.code_services import OPERATIONS
from tin_lite.workflow_code import validate_code_definition, validate_code_result

ROOT = Path(__file__).resolve().parent.parent / "workflow_packages" / "growth.support_search_gap"
CREATED_AT = "2026-09-23T00:00:00+00:00"


def load():
    definition = json.loads((ROOT / "workflow.json").read_text())["definition"]
    module = SimpleNamespace(**runpy.run_path(str(ROOT / "main.py")))
    return module, definition


def service_contract(spec, payload):
    """A lightweight mirror of code_services.py's own reviewed-mapping check."""
    if not isinstance(payload, dict) or set(payload) != {
        "service",
        "step",
        "operation",
        "arguments",
    }:
        raise ValueError("invalid service request fields")
    if not isinstance(payload["step"], str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}", payload["step"]
    ):
        raise ValueError("invalid service step")
    binding = next(s for s in spec.services if s.name == payload["service"])
    capability, fields = OPERATIONS[(binding.provider_key, payload["operation"])]
    if capability not in binding.capabilities:
        raise ValueError("service call uses an undeclared capability")
    if set(payload["arguments"]) - fields:
        raise ValueError("service call uses undeclared arguments")


def gsc_row(query, impressions, clicks=0):
    return {
        "keys": [query],
        "clicks": clicks,
        "impressions": impressions,
        "ctr": 0.0,
        "position": 1.0,
    }


class Context(dict):
    """Mirrors tin_lite.code_runner.Context: dict access plus .services/.models."""

    def __init__(self, data, *, services, models):
        super().__init__(data)
        self.services = services
        self.models = models


def make_context(*, gsc_rows, gmail_estimates, phrase_pairs=None):
    """gmail_estimates is consumed in derived-phrase order, one per Gmail call."""
    _, definition = load()
    spec = validate_code_definition(definition)
    service_calls, model_calls = [], []
    estimates = list(gmail_estimates)

    async def services_call(**payload):
        service_contract(spec, payload)
        service_calls.append(payload)
        if payload["operation"] == "search_analytics.read":
            return {"rows": gsc_rows}
        estimate = estimates.pop(0)
        return {"result_size_estimate": estimate, "truncated": False}

    async def models_generate(**payload):
        request_contract(spec, payload)
        model_calls.append(payload)
        if phrase_pairs is not None:
            output = {"pairs": phrase_pairs}
        else:
            output = {
                "pairs": [
                    {"query": query, "gmail_phrase": f"subject:({query})"}
                    for query in payload["data"]
                ]
            }
        jsonschema.validate(output, payload["output_schema"])
        return {"parsed": output, "text": json.dumps(output)}

    context = Context(
        {"run_id": "11111111-1111-1111-1111-111111111111", "created_at": CREATED_AT},
        services=SimpleNamespace(call=services_call),
        models=SimpleNamespace(generate=models_generate),
    )
    return context, service_calls, model_calls, spec


def test_manifest_validates_against_the_code_contract():
    _, definition = load()
    spec = validate_code_definition(definition)
    assert {s.name for s in spec.services} == {"gsc", "gmail"}
    assert {r.name for r in spec.model_routes} == {"derive_phrases"}


async def test_rows_above_both_thresholds_clear_the_gate_in_normal_mode():
    module, _ = load()
    context, service_calls, model_calls, spec = make_context(
        gsc_rows=[gsc_row("export my data csv", 500), gsc_row("cancel my plan", 40)],
        gmail_estimates=[12, 1],
    )
    result = await module.run(context, {"lookback_days": 28, "row_limit": 5, "mode": "normal"})
    validate_code_result(json.dumps(result).encode(), spec)
    content = result["content"]
    assert "export my data csv" in content and "content gap" in content
    assert "cancel my plan" in content and "insufficient data" in content
    assert "coincides with an independent Gmail signal" in content
    assert "causal" not in content.lower() and " caused " in content
    steps = [c["step"] for c in service_calls]
    assert steps == ["gsc_query_pull", "gmail_search_0", "gmail_search_1"]
    assert model_calls[0]["step"] == "derive_gmail_phrases"


async def test_moneyball_mode_emits_only_the_table():
    module, _ = load()
    context, *_ = make_context(gsc_rows=[gsc_row("export my data csv", 500)], gmail_estimates=[12])
    result = await module.run(context, {"lookback_days": 28, "row_limit": 5, "mode": "moneyball"})
    lines = [line for line in result["content"].splitlines() if line]
    assert all(line.startswith("|") for line in lines)
    assert "Findings" not in result["content"] and "coincides" not in result["content"]


async def test_ranking_keeps_top_impressions_and_bounds_gmail_calls_by_row_limit():
    module, _ = load()
    rows = [gsc_row(f"query {i}", impressions=100 - i) for i in range(10)]
    context, service_calls, _, _ = make_context(gsc_rows=rows, gmail_estimates=[0, 0, 0])
    result = await module.run(context, {"lookback_days": 28, "row_limit": 3, "mode": "normal"})
    gmail_steps = [c for c in service_calls if c["operation"] == "gmail.messages.search"]
    assert len(gmail_steps) == 3
    assert "query 0" in result["content"] and "query 9" not in result["content"]


async def test_empty_search_console_result_skips_model_and_gmail_calls():
    module, _ = load()
    context, service_calls, model_calls, spec = make_context(gsc_rows=[], gmail_estimates=[])
    result = await module.run(context, {"lookback_days": 28, "row_limit": 5, "mode": "normal"})
    validate_code_result(json.dumps(result).encode(), spec)
    assert [c["step"] for c in service_calls] == ["gsc_query_pull"]
    assert model_calls == []
    assert "No Search Console queries were returned" in result["content"]


async def test_a_plausible_but_unusable_model_result_is_rejected():
    module, _ = load()
    context, _, _, _ = make_context(
        gsc_rows=[gsc_row("export my data csv", 500)],
        gmail_estimates=[12],
        phrase_pairs=[{"query": "a different query entirely", "gmail_phrase": "subject:x"}],
    )
    with pytest.raises(ValueError, match="query set"):
        await module.run(context, {"lookback_days": 28, "row_limit": 5, "mode": "normal"})


async def test_a_duplicated_query_from_the_model_is_rejected():
    module, _ = load()
    context, _, _, _ = make_context(
        gsc_rows=[gsc_row("export my data csv", 500), gsc_row("cancel my plan", 40)],
        gmail_estimates=[12, 1],
        phrase_pairs=[
            {"query": "export my data csv", "gmail_phrase": "subject:x"},
            {"query": "export my data csv", "gmail_phrase": "subject:y"},
        ],
    )
    with pytest.raises(ValueError, match="duplicated"):
        await module.run(context, {"lookback_days": 28, "row_limit": 5, "mode": "normal"})


def test_malformed_gsc_rows_are_rejected_not_silently_dropped():
    module, _ = load()
    with pytest.raises(ValueError, match="malformed"):
        module._top_queries([{"keys": ["x"], "impressions": "not a number"}], 5)
