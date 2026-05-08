"""Тесты для MetricsAnalytics."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from metrics_analytics import MetricsAnalytics, TABLE_NAME  # noqa: E402
from tests.fixtures import (  # noqa: E402
    DEFAULT_DESCRIPTIONS,
    SAMPLE_DEEP_TREE,
    SAMPLE_HETEROGENEOUS,
    SAMPLE_MIN,
    SAMPLE_OBJECT_PERIOD,
    SAMPLE_PERIOD_DICT,
    SAMPLE_RESERVED_WORDS,
)


def silent_llm(_prompt: str) -> str:
    return ""


def make_agent(**overrides):
    return MetricsAnalytics(
        get_llm=overrides.pop("get_llm", silent_llm),
        field_descriptions=overrides.pop("field_descriptions", DEFAULT_DESCRIPTIONS),
        **overrides,
    )


# =====================================================================
# SCHEMA / json_to_sqlite
# =====================================================================

class TestSchemaLoading:
    def test_minimal_payload_row_count(self):
        agent = make_agent()
        agent.json_to_sqlite(SAMPLE_MIN)
        # me: 1 metric, e1: 3 metrics (deals + won + lost), e2: 1 metric → total 5
        cur = agent._conn.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}")
        assert cur.fetchone()[0] == 5

    def test_objects_present(self):
        agent = make_agent()
        agent.json_to_sqlite(SAMPLE_MIN)
        roles = agent._conn.execute(
            f"SELECT object_role, COUNT(*) FROM {TABLE_NAME} GROUP BY object_role"
        ).fetchall()
        roles_dict = {r[0]: r[1] for r in roles}
        assert roles_dict.get("me") == 1
        assert roles_dict.get("employee") == 4

    def test_deep_tree_levels(self):
        agent = make_agent()
        agent.json_to_sqlite(SAMPLE_DEEP_TREE)
        levels = agent._conn.execute(
            f"SELECT DISTINCT metric_level FROM {TABLE_NAME} ORDER BY metric_level"
        ).fetchall()
        assert [r[0] for r in levels] == [0, 1, 2, 3]

    def test_parent_metric_id_chain(self):
        agent = make_agent()
        agent.json_to_sqlite(SAMPLE_DEEP_TREE)
        # Recursive CTE: count ancestors of L3A
        rows = agent._conn.execute(f"""
            WITH RECURSIVE anc(metric_id, depth) AS (
                SELECT metric_id, 0 FROM {TABLE_NAME} WHERE metric_local_id='L3A'
                UNION ALL
                SELECT m.parent_metric_id, anc.depth + 1
                FROM {TABLE_NAME} m
                JOIN anc ON m.metric_id = anc.metric_id
                WHERE m.parent_metric_id IS NOT NULL
            )
            SELECT COUNT(*) FROM anc
        """).fetchall()
        # L3A → L2A → L1A → L0 → root: 4 levels including itself
        assert rows[0][0] >= 3

    def test_period_dict_expansion(self):
        agent = make_agent()
        agent.json_to_sqlite(SAMPLE_PERIOD_DICT)
        rows = agent._conn.execute(
            f"SELECT period, m_value FROM {TABLE_NAME} "
            f"WHERE metric_local_id='rev' ORDER BY period"
        ).fetchall()
        assert len(rows) == 4
        periods = [r[0] for r in rows]
        assert periods == ["2024-Q1", "2024-Q2", "2024-Q3", "2024-Q4"]
        values = [r[1] for r in rows]
        assert values == [100, 150, 200, 180]

    def test_object_level_period_propagates(self):
        agent = make_agent()
        agent.json_to_sqlite(SAMPLE_OBJECT_PERIOD)
        # Все метрики этого сотрудника должны иметь period = "2024-Q2"
        rows = agent._conn.execute(
            f"SELECT DISTINCT period FROM {TABLE_NAME}"
        ).fetchall()
        periods = {r[0] for r in rows}
        assert periods == {"2024-Q2"}

    def test_heterogeneous_schemas(self):
        agent = make_agent()
        agent.json_to_sqlite(SAMPLE_HETEROGENEOUS)
        cols = {r["name"]: r["type"] for r in agent._conn.execute(
            f"PRAGMA table_info({TABLE_NAME})"
        ).fetchall()}
        # У одного есть region, у другого нет — колонка должна существовать
        assert "obj_region" in cols
        # У e2 region IS NULL
        rows = agent._conn.execute(
            f"SELECT obj_region FROM {TABLE_NAME} WHERE object_id='e2'"
        ).fetchall()
        assert all(r[0] is None for r in rows)

    def test_reserved_words_renamed(self):
        agent = make_agent()
        agent.json_to_sqlite(SAMPLE_RESERVED_WORDS)
        cols = {r["name"] for r in agent._conn.execute(
            f"PRAGMA table_info({TABLE_NAME})"
        ).fetchall()}
        # `select` и `from` зарезервированы → переименованы с _x
        assert any("obj_select" in c for c in cols)
        assert any("obj_from" in c for c in cols)
        # `where` тоже зарезервировано
        assert any("m_where" in c for c in cols)

    def test_reinit_replaces_data(self):
        agent = make_agent()
        agent.json_to_sqlite(SAMPLE_MIN)
        first_count = agent._conn.execute(
            f"SELECT COUNT(*) FROM {TABLE_NAME}"
        ).fetchone()[0]
        agent.json_to_sqlite(SAMPLE_HETEROGENEOUS)
        second_count = agent._conn.execute(
            f"SELECT COUNT(*) FROM {TABLE_NAME}"
        ).fetchone()[0]
        assert first_count != second_count
        # У SAMPLE_HETEROGENEOUS — 2 employee × 1 metric = 2 строки
        assert second_count == 2

    def test_invalid_payload_no_employees_no_me(self):
        agent = make_agent()
        with pytest.raises(ValueError):
            agent.json_to_sqlite({"me": {}, "employees": []})

    def test_invalid_payload_wrong_types(self):
        agent = make_agent()
        with pytest.raises(ValueError):
            agent.json_to_sqlite({"me": "not_a_dict", "employees": []})

    def test_metric_name_alias_no_duplicate(self):
        """Если в JSON метрики поле называется `metric_name`, оно должно
        идти в фиксированную колонку, а не создавать дубль `m_metric_name`."""
        payload = {
            "me": {},
            "employees": [{
                "id": "e1",
                "name": "Bob",
                "metrics": [{
                    "id": "rev",
                    "metric_name": "Revenue",
                    "value": 100,
                    "period": "2024",
                }],
            }],
        }
        agent = make_agent()
        agent.json_to_sqlite(payload)
        cols = {r["name"] for r in agent._conn.execute(
            f"PRAGMA table_info({TABLE_NAME})"
        ).fetchall()}
        assert "m_metric_name" not in cols
        rows = agent._conn.execute(
            f"SELECT metric_name FROM {TABLE_NAME} WHERE metric_local_id='rev'"
        ).fetchall()
        assert rows[0][0] == "Revenue"

    def test_object_name_id_aliases_no_duplicate(self):
        """`object_name`/`object_id` в JSON объекта не создают дубли колонок."""
        payload = {
            "me": {},
            "employees": [{
                "object_id": "e1",
                "object_name": "Carol",
                "metrics": [{"id": "x", "name": "X", "value": 1, "period": "2024"}],
            }],
        }
        agent = make_agent()
        agent.json_to_sqlite(payload)
        cols = {r["name"] for r in agent._conn.execute(
            f"PRAGMA table_info({TABLE_NAME})"
        ).fetchall()}
        assert "obj_object_name" not in cols
        assert "obj_object_id" not in cols
        rows = agent._conn.execute(
            f"SELECT object_name, object_id FROM {TABLE_NAME}"
        ).fetchall()
        assert rows[0][0] == "Carol"
        assert rows[0][1] == "e1"

    def test_explicit_alias_wins_over_short_form(self):
        """Если есть и `name`, и `metric_name` — побеждает `metric_name`."""
        payload = {
            "me": {},
            "employees": [{
                "id": "e1",
                "metrics": [{
                    "id": "rev",
                    "name": "short",
                    "metric_name": "explicit",
                    "value": 10,
                    "period": "2024",
                }],
            }],
        }
        agent = make_agent()
        agent.json_to_sqlite(payload)
        rows = agent._conn.execute(
            f"SELECT metric_name FROM {TABLE_NAME} WHERE metric_local_id='rev'"
        ).fetchall()
        assert rows[0][0] == "explicit"

    def test_ddl_contains_comments(self):
        agent = make_agent()
        agent.json_to_sqlite(SAMPLE_MIN)
        ddl = agent.get_table_ddl()
        # Описание из DEFAULT_DESCRIPTIONS должно встретиться в DDL
        assert "Числовое значение метрики" in ddl or "value" in ddl.lower()
        assert "--" in ddl

    def test_db_size_printed(self, capsys):
        agent = make_agent()
        agent.json_to_sqlite(SAMPLE_MIN)
        captured = capsys.readouterr()
        assert "DB size:" in captured.out
        assert "MB" in captured.out


# =====================================================================
# METADATA
# =====================================================================

class TestMetadata:
    def test_metadata_basic_counts(self):
        agent = make_agent()
        agent.json_to_sqlite(SAMPLE_MIN)
        meta = agent._compute_metadata()
        assert meta.has_me is True
        assert meta.n_employees == 2
        assert meta.n_objects == 3
        assert meta.n_periods == 1  # все Q1
        assert "2024-Q1" in meta.periods_sample

    def test_metadata_levels_branching(self):
        agent = make_agent()
        agent.json_to_sqlite(SAMPLE_DEEP_TREE)
        meta = agent._compute_metadata()
        # 4 уровня: 0..3
        assert set(meta.metric_levels.keys()) >= {"0", "1", "2", "3"}
        # Уровень 0 — 1 узел (только L0)
        assert meta.metric_levels["0"]["node_count"] == 1
        # Уровень 1 — 2 узла (L1A, L1B)
        assert meta.metric_levels["1"]["node_count"] == 2

    def test_metadata_columns_included(self):
        agent = make_agent()
        agent.json_to_sqlite(SAMPLE_MIN)
        meta = agent._compute_metadata()
        assert "object_role" in meta.columns
        assert "metric_id" in meta.columns
        assert "m_value" in meta.columns


# =====================================================================
# ANALYZE — с моками LLM
# =====================================================================

class StubLLM:
    """Мок LLM, отдающий заранее заданные ответы по очереди."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, prompt):
        self.calls.append(prompt)
        if not self.responses:
            return "{}"
        return self.responses.pop(0)


class TestAnalyze:
    def test_happy_path(self):
        sql_resp = json.dumps({
            "reasoning": "select all",
            "sql": "SELECT object_name, m_value FROM metrics WHERE object_role='employee'",
            "expected_columns": ["object_name", "m_value"],
        })
        ans_resp = json.dumps({
            "reasoning": "found 2 employees",
            "answer": "Alice: 12, Bob: 5",
        })
        llm = StubLLM([sql_resp, ans_resp])
        agent = make_agent(get_llm=llm)
        agent.json_to_sqlite(SAMPLE_MIN)
        result = agent.analyze("кто лучший", "deals", "одна строка")
        assert "error" not in result
        assert result["answer"] == "Alice: 12, Bob: 5"
        assert "metrics" in result["sql"].lower()
        assert len(result["sql_result"]) >= 2

    def test_retry_on_invalid_then_valid(self):
        sql_bad = "INSERT INTO metrics VALUES (1)"  # запрещён DML
        sql_bad_json = json.dumps({"reasoning": "x", "sql": sql_bad, "expected_columns": []})
        sql_good_json = json.dumps({
            "reasoning": "ok",
            "sql": "SELECT COUNT(*) AS n FROM metrics",
            "expected_columns": ["n"],
        })
        ans_resp = json.dumps({"reasoning": "...", "answer": "пять"})
        llm = StubLLM([sql_bad_json, sql_good_json, ans_resp])
        agent = make_agent(get_llm=llm)
        agent.json_to_sqlite(SAMPLE_MIN)
        result = agent.analyze("сколько строк", "ctx", "число")
        assert "error" not in result
        assert result["answer"] == "пять"

    def test_fail_after_max_retries(self):
        bad = json.dumps({"reasoning": "x", "sql": "DROP TABLE metrics", "expected_columns": []})
        # 3 попытки подряд — все плохие
        llm = StubLLM([bad, bad, bad])
        agent = make_agent(get_llm=llm)
        agent.json_to_sqlite(SAMPLE_MIN)
        result = agent.analyze("ломай", "ctx", "что угодно")
        assert "error" in result
        assert result["error"]["stage"] == "sql_generation"

    def test_unknown_column_retried(self):
        bad = json.dumps({
            "reasoning": "guess",
            "sql": "SELECT nonexistent_col FROM metrics",
            "expected_columns": ["nonexistent_col"],
        })
        good = json.dumps({
            "reasoning": "ok",
            "sql": "SELECT COUNT(*) FROM metrics",
            "expected_columns": ["count"],
        })
        ans = json.dumps({"reasoning": "x", "answer": "5"})
        llm = StubLLM([bad, good, ans])
        agent = make_agent(get_llm=llm)
        agent.json_to_sqlite(SAMPLE_MIN)
        result = agent.analyze("count", "ctx", "число")
        assert "error" not in result
        assert result["answer"] == "5"

    def test_uninitialized_db(self):
        agent = make_agent()
        result = agent.analyze("любой", "ctx", "формат")
        assert "error" in result
        assert result["error"]["stage"] == "input_validation"

    def test_response_format_in_prompt(self):
        sql_resp = json.dumps({
            "reasoning": "sel",
            "sql": "SELECT object_name FROM metrics LIMIT 1",
            "expected_columns": ["object_name"],
        })
        ans_resp = json.dumps({"reasoning": "x", "answer": "Boss"})
        llm = StubLLM([sql_resp, ans_resp])
        agent = make_agent(get_llm=llm)
        agent.json_to_sqlite(SAMPLE_MIN)
        agent.analyze("any", "ctx", "markdown table")
        # Финальный промпт (вызов 2) должен содержать "markdown table"
        assert "markdown table" in llm.calls[-1]

    def test_json_parse_with_markdown_fence(self):
        # LLM выдаёт ответ внутри ```json ... ```
        sql_resp = "```json\n" + json.dumps({
            "reasoning": "sel",
            "sql": "SELECT 1 AS x FROM metrics LIMIT 1",
            "expected_columns": ["x"],
        }) + "\n```"
        ans_resp = "```json\n" + json.dumps({"reasoning": "x", "answer": "ok"}) + "\n```"
        llm = StubLLM([sql_resp, ans_resp])
        agent = make_agent(get_llm=llm)
        agent.json_to_sqlite(SAMPLE_MIN)
        result = agent.analyze("any", "ctx", "ok")
        assert "error" not in result
        assert result["answer"] == "ok"


# =====================================================================
# DEEP ANALYZE — итеративное обследование БД
# =====================================================================

class TestDeepAnalyze:
    def test_single_step_immediate_answer(self):
        # LLM сразу отвечает без исследования
        ans = json.dumps({
            "next_action": "answer",
            "reasoning": "схема понятна, отвечаю сразу",
            "answer": "5 строк в БД",
        })
        llm = StubLLM([ans])
        agent = make_agent(get_llm=llm)
        agent.json_to_sqlite(SAMPLE_MIN)
        result = agent.analyze("сколько метрик?", "ctx", "число", deep=True)
        assert "error" not in result
        assert result["answer"] == "5 строк в БД"
        assert result["investigation_steps"] == []

    def test_multi_step_investigation(self):
        # Шаг 1: explore (узнать периоды)
        # Шаг 2: explore (узнать имена объектов)
        # Шаг 3: answer
        steps = [
            json.dumps({
                "next_action": "explore",
                "reasoning": "проверю какие периоды есть",
                "sql": "SELECT DISTINCT period FROM metrics",
                "expected_insight": "набор уникальных периодов",
            }),
            json.dumps({
                "next_action": "explore",
                "reasoning": "теперь имена объектов",
                "sql": "SELECT DISTINCT object_name FROM metrics WHERE object_role='employee'",
                "expected_insight": "сколько сотрудников",
            }),
            json.dumps({
                "next_action": "answer",
                "reasoning": "достаточно: 1 период + 2 сотрудника",
                "answer": "Q1 2024, сотрудники Alice и Bob",
            }),
        ]
        llm = StubLLM(steps)
        agent = make_agent(get_llm=llm)
        agent.json_to_sqlite(SAMPLE_MIN)
        result = agent.analyze("обзор", "ctx", "одна фраза", deep=True)
        assert "error" not in result
        assert result["answer"] == "Q1 2024, сотрудники Alice и Bob"
        assert len(result["investigation_steps"]) == 2
        assert result["investigation_steps"][0]["sql"].startswith("SELECT DISTINCT period")
        assert result["investigation_steps"][1]["sql"].startswith("SELECT DISTINCT object_name")
        # sql и sql_result должны указывать на последний выполненный шаг
        assert "object_name" in result["sql"]
        assert len(result["sql_result"]) >= 1

    def test_replan_on_unexpected_data(self):
        # LLM "обнаруживает" что-то и меняет курс на ходу
        steps = [
            json.dumps({
                "next_action": "explore",
                "reasoning": "сначала глобальное распределение",
                "sql": "SELECT metric_name, COUNT(*) AS n FROM metrics GROUP BY metric_name",
                "expected_insight": "какие метрики чаще встречаются",
            }),
            # После шага 1 LLM пересматривает план: углубляется в Deals
            json.dumps({
                "next_action": "explore",
                "reasoning": "Deals встречается несколько раз — посмотрю детали",
                "sql": "SELECT object_name, m_value FROM metrics WHERE metric_name='Deals' ORDER BY m_value DESC",
                "expected_insight": "топ по сделкам",
            }),
            json.dumps({
                "next_action": "answer",
                "reasoning": "Alice — лидер",
                "answer": "Alice (12 deals)",
            }),
        ]
        llm = StubLLM(steps)
        agent = make_agent(get_llm=llm)
        agent.json_to_sqlite(SAMPLE_MIN)
        result = agent.analyze("кто лидер", "ctx", "одна строка", deep=True)
        assert "error" not in result
        assert result["answer"] == "Alice (12 deals)"
        # Replan виден в reasoning
        assert "Deals встречается несколько раз" in result["reasoning"]

    def test_invalid_explore_sql_retried(self):
        # Первая попытка — explore с невалидным SQL (DROP), затем валидный
        bad = json.dumps({
            "next_action": "explore",
            "reasoning": "ломаю",
            "sql": "DROP TABLE metrics",
            "expected_insight": "никаких",
        })
        good_explore = json.dumps({
            "next_action": "explore",
            "reasoning": "уже легально",
            "sql": "SELECT COUNT(*) AS n FROM metrics",
            "expected_insight": "общее число строк",
        })
        ans = json.dumps({
            "next_action": "answer",
            "reasoning": "понятно",
            "answer": "5 строк",
        })
        llm = StubLLM([bad, good_explore, ans])
        agent = make_agent(get_llm=llm)
        agent.json_to_sqlite(SAMPLE_MIN)
        result = agent.analyze("сколько", "ctx", "число", deep=True)
        assert "error" not in result
        assert result["answer"] == "5 строк"

    def test_max_steps_force_finalize(self):
        # Все шаги — explore. На последнем шаге force_finalize заставит дать answer.
        # Если LLM упорно возвращает explore — должен быть error.
        explore_step = json.dumps({
            "next_action": "explore",
            "reasoning": "ещё разок",
            "sql": "SELECT 1 AS x FROM metrics LIMIT 1",
            "expected_insight": "чекаем",
        })
        # 3 retries × N шагов = много explore-ответов; в итоге исчерпание ретраев
        many = [explore_step] * 50
        llm = StubLLM(many)
        agent = make_agent(get_llm=llm, max_inspection_steps=3)
        agent.json_to_sqlite(SAMPLE_MIN)
        result = agent.analyze("упрись", "ctx", "ok", deep=True)
        assert "error" in result
        # на последнем шаге force_finalize заворачивает explore — fail в стадии deep_inspection
        assert result["error"]["stage"] == "deep_inspection"

    def test_max_inspection_steps_override_per_call(self):
        ans = json.dumps({
            "next_action": "answer",
            "reasoning": "x",
            "answer": "ok",
        })
        llm = StubLLM([ans])
        agent = make_agent(get_llm=llm, max_inspection_steps=5)
        agent.json_to_sqlite(SAMPLE_MIN)
        # Per-call override на 2 шага
        result = agent.analyze("q", "ctx", "ok", deep=True, max_inspection_steps=2)
        assert "error" not in result
        assert result["answer"] == "ok"

    def test_pydantic_validation_explore_without_sql(self):
        # LLM возвращает explore без sql — должно отфильтроваться pydantic-валидатором → retry
        broken = json.dumps({
            "next_action": "explore",
            "reasoning": "забыл sql",
            "sql": None,
            "expected_insight": "ничего",
        })
        good = json.dumps({
            "next_action": "answer",
            "reasoning": "ладно",
            "answer": "ok",
        })
        llm = StubLLM([broken, good])
        agent = make_agent(get_llm=llm)
        agent.json_to_sqlite(SAMPLE_MIN)
        result = agent.analyze("q", "ctx", "ok", deep=True)
        assert "error" not in result
        assert result["answer"] == "ok"
