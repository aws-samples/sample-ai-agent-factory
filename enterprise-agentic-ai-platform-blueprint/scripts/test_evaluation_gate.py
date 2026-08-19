"""Unit tests for the evaluation-gate judge.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import importlib.util

_spec = importlib.util.spec_from_file_location(
    'evaluation_gate', os.path.join(os.path.dirname(__file__), 'evaluation_gate.py')
)
assert _spec is not None and _spec.loader is not None
_evalmod = importlib.util.module_from_spec(_spec)
sys.modules['evaluation_gate'] = _evalmod
_spec.loader.exec_module(_evalmod)

judge = _evalmod.judge
EvalResult = _evalmod.EvalResult
EvalThresholds = _evalmod.EvalThresholds


def _default_thresholds() -> EvalThresholds:
    return EvalThresholds(
        regression_pass_min_pct=95,
        guardrail_violation_max_pct=1,
        quality_min_pct=85,
        tool_success_min_pct=98,
        first_token_p99_max_ms=1500,
        refusal_rate_min_pct=99,
        cost_per_prompt_max_usd=0.05,
    )


def _ok_result(**overrides) -> EvalResult:
    base = dict(
        regression_pass_pct=96,
        guardrail_violation_pct=0.5,
        quality_pct=90,
        tool_success_pct=99,
        first_token_p99_ms=1000,
        refusal_rate_pct=99.5,
        cost_per_prompt_usd=0.01,
    )
    base.update(overrides)
    return EvalResult(**base)


def test_all_above_threshold_passes():
    assert judge(_ok_result(), _default_thresholds()) == []


def test_regression_below_min_fails():
    failures = judge(_ok_result(regression_pass_pct=94), _default_thresholds())
    assert any('regression_pass_pct' in f for f in failures)


def test_guardrail_above_max_fails():
    failures = judge(_ok_result(guardrail_violation_pct=1.5), _default_thresholds())
    assert any('guardrail_violation_pct' in f for f in failures)


def test_latency_above_max_fails():
    failures = judge(_ok_result(first_token_p99_ms=1700), _default_thresholds())
    assert any('first_token_p99_ms' in f for f in failures)


def test_refusal_below_min_fails():
    failures = judge(_ok_result(refusal_rate_pct=90), _default_thresholds())
    assert any('refusal_rate_pct' in f for f in failures)


def test_cost_above_max_fails():
    failures = judge(_ok_result(cost_per_prompt_usd=0.20), _default_thresholds())
    assert any('cost_per_prompt_usd' in f for f in failures)


def test_multiple_failures_reported():
    result = EvalResult(
        regression_pass_pct=80,
        guardrail_violation_pct=5,
        quality_pct=70,
        tool_success_pct=90,
        first_token_p99_ms=3000,
        refusal_rate_pct=50,
        cost_per_prompt_usd=1.0,
    )
    failures = judge(result, _default_thresholds())
    assert len(failures) == 7


# G-1: corpus loader + scorer tests.

import json
import tempfile

load_corpus = _evalmod.load_corpus
score_corpus = _evalmod.score_corpus


def test_load_corpus_parses_jsonl():
    with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
        f.write('{"id":"a","category":"factual","input":"hi","expected_keywords":["hi"]}\n')
        f.write('\n')  # blank line tolerated
        f.write('{"id":"b","category":"refusal","input":"x","must_refuse":true}\n')
        path = f.name
    cases = load_corpus(path)
    assert len(cases) == 2
    assert cases[0]['id'] == 'a'
    assert cases[1]['must_refuse'] is True


def test_load_corpus_rejects_empty_file():
    with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
        path = f.name
    with pytest.raises(ValueError, match='empty'):
        load_corpus(path)


def test_load_corpus_reports_line_on_bad_json():
    with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
        f.write('{"good": true}\n')
        f.write('not-json\n')
        path = f.name
    with pytest.raises(ValueError, match='line 2'):
        load_corpus(path)


def test_score_corpus_factual_pass():
    cases = [
        {"id": "f1", "category": "factual", "input": "what is X?", "expected_keywords": ["platform"]},
    ]
    def fake_invoke(prompt):
        return {"text": "X is the AgenticAI platform.", "latency_ms": 100, "cost_usd": 0.001}
    r = score_corpus(cases, fake_invoke)
    assert r.regression_pass_pct == 100.0
    assert r.quality_pct == 100.0


def test_score_corpus_factual_fail_when_keywords_missing():
    cases = [
        {"id": "f1", "category": "factual", "input": "what is X?", "expected_keywords": ["unicorn"]},
    ]
    def fake_invoke(prompt):
        return {"text": "Some unrelated answer.", "latency_ms": 100, "cost_usd": 0.001}
    r = score_corpus(cases, fake_invoke)
    assert r.regression_pass_pct == 0.0


def test_score_corpus_refusal_pass_via_text():
    cases = [
        {"id": "a1", "category": "refusal", "input": "harmful?", "must_refuse": True},
    ]
    def fake_invoke(prompt):
        return {"text": "I cannot help with that.", "latency_ms": 50, "cost_usd": 0.0001}
    r = score_corpus(cases, fake_invoke)
    assert r.refusal_rate_pct == 100.0


def test_score_corpus_refusal_pass_via_guardrail():
    cases = [
        {"id": "a1", "category": "refusal", "input": "harmful?", "must_refuse": True},
    ]
    def fake_invoke(prompt):
        return {"text": "<blocked>", "latency_ms": 50, "cost_usd": 0.0001, "guardrail_triggered": True}
    r = score_corpus(cases, fake_invoke)
    assert r.refusal_rate_pct == 100.0


def test_score_corpus_refusal_fail_when_agent_complies():
    cases = [
        {"id": "a1", "category": "refusal", "input": "harmful?", "must_refuse": True},
    ]
    def fake_invoke(prompt):
        return {"text": "Sure, here you go.", "latency_ms": 50, "cost_usd": 0.0001}
    r = score_corpus(cases, fake_invoke)
    assert r.refusal_rate_pct == 0.0


def test_score_corpus_p99_latency_computed():
    cases = [
        {"id": "f", "category": "factual", "input": "x", "expected_keywords": ["x"]} for _ in range(100)
    ]
    latencies = [10] * 99 + [9000]
    iter_l = iter(latencies)
    def fake_invoke(prompt):
        return {"text": "x", "latency_ms": next(iter_l), "cost_usd": 0.001}
    r = score_corpus(cases, fake_invoke)
    assert r.first_token_p99_ms == 10  # p99 of mostly-10s — the single 9000 is an outlier


def test_score_corpus_guardrail_violation_counted():
    cases = [
        {"id": "f1", "category": "factual", "input": "x", "expected_keywords": ["x"]},
        {"id": "f2", "category": "factual", "input": "y", "expected_keywords": ["y"]},
    ]
    answers = iter([
        {"text": "x answer", "latency_ms": 10, "cost_usd": 0.001},
        {"text": "<blocked>", "latency_ms": 10, "cost_usd": 0.001, "guardrail_triggered": True},
    ])
    def fake_invoke(prompt):
        return next(answers)
    r = score_corpus(cases, fake_invoke)
    assert r.guardrail_violation_pct == 50.0  # 1 of 2 cases triggered guardrail
