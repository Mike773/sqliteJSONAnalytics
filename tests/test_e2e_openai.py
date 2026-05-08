"""End-to-end тест с реальным OpenAI API.

Запускается только если задана переменная окружения OPENAI_API_KEY:
    OPENAI_API_KEY=sk-... python -m pytest tests/test_e2e_openai.py -v -s

Без переменной окружения тесты помечаются skipped — обычный
`pytest tests/` их пропускает.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytestmark = pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY env var not set",
)

openai = pytest.importorskip("openai")

from metrics_analytics import MetricsAnalytics  # noqa: E402
from tests.fixtures import (  # noqa: E402
    DEFAULT_DESCRIPTIONS,
    SAMPLE_DEEP_TREE,
    SAMPLE_MIN,
)

DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")


def make_openai_llm(model: str = DEFAULT_MODEL):
    client = openai.OpenAI()

    def get_llm(prompt: str) -> str:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            response_format={"type": "json_object"},
        )
        return resp.choices[0].message.content or ""

    return get_llm


# =====================================================================
# Простой режим
# =====================================================================

def test_simple_top_employee():
    agent = MetricsAnalytics(
        get_llm=make_openai_llm(),
        field_descriptions=DEFAULT_DESCRIPTIONS,
    )
    agent.json_to_sqlite(SAMPLE_MIN)
    result = agent.analyze(
        query="Какой сотрудник заключил больше всего сделок (метрика Deals)?",
        business_context="воронка B2B продаж, Q1 2024",
        response_format="одна короткая фраза с именем и числом сделок",
    )
    print("\n[SIMPLE] answer:", result["answer"])
    print("[SIMPLE] sql:", result["sql"])
    assert "error" not in result, result.get("error")
    assert isinstance(result["answer"], str) and result["answer"].strip()
    assert isinstance(result["sql_result"], list)


def test_simple_uses_metadata():
    agent = MetricsAnalytics(
        get_llm=make_openai_llm(),
        field_descriptions=DEFAULT_DESCRIPTIONS,
    )
    agent.json_to_sqlite(SAMPLE_MIN)
    result = agent.analyze(
        query="Сколько уникальных метрик в базе?",
        business_context="инвентаризация данных",
        response_format="одно число",
    )
    print("\n[META] answer:", result["answer"])
    assert "error" not in result, result.get("error")
    assert result["metadata"]["n_unique_metrics"] >= 1


# =====================================================================
# Deep-режим: итеративное обследование
# =====================================================================

def test_deep_overview_min():
    agent = MetricsAnalytics(
        get_llm=make_openai_llm(),
        field_descriptions=DEFAULT_DESCRIPTIONS,
    )
    agent.json_to_sqlite(SAMPLE_MIN)
    result = agent.analyze(
        query="Дай первичный обзор данных: какие объекты, какие метрики, какие периоды.",
        business_context="первый взгляд на новый датасет",
        response_format="bullet list, по пункту на категорию",
        deep=True,
        max_inspection_steps=6,
    )
    print(f"\n[DEEP-MIN] {len(result['investigation_steps'])} steps:")
    for s in result["investigation_steps"]:
        print(f"  • {s['sql']}  → {len(s.get('result_full', []))} rows")
    print("[DEEP-MIN] answer:\n", result["answer"])
    assert "error" not in result, result.get("error")
    assert result["answer"].strip()
    # В deep-режиме ожидаем хотя бы один шаг обследования (LLM редко сразу отвечает)
    assert len(result["investigation_steps"]) >= 1


def test_deep_hierarchy_analysis():
    agent = MetricsAnalytics(
        get_llm=make_openai_llm(),
        field_descriptions=DEFAULT_DESCRIPTIONS,
    )
    agent.json_to_sqlite(SAMPLE_DEEP_TREE)
    result = agent.analyze(
        query="Опиши структуру иерархии метрик: глубину, ветвление, какие листовые метрики самые большие.",
        business_context="дерево декомпозиции бизнес-показателя",
        response_format="2-3 предложения",
        deep=True,
        max_inspection_steps=6,
    )
    print(f"\n[DEEP-TREE] {len(result['investigation_steps'])} steps:")
    for s in result["investigation_steps"]:
        print(f"  • {s['sql']}  → {len(s.get('result_full', []))} rows")
    print("[DEEP-TREE] answer:\n", result["answer"])
    assert "error" not in result, result.get("error")
    assert result["answer"].strip()
    assert len(result["investigation_steps"]) >= 1
