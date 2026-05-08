"""
metrics_analytics.py

Встраиваемый класс MetricsAnalytics: загружает иерархичный JSON с метриками
в in-memory SQLite (одна таблица), отвечает на бизнес-вопросы пользователя
через LLM (генерация SQL в JSON, валидация pydantic, retry с фидбэком).

Зависимости: pydantic>=2.6,<3 и stdlib.
Python 3.9+.

Использование:
    def my_llm(prompt: str) -> str: ...
    descriptions = {"name": "ФИО", "value": "Значение метрики", ...}
    agent = MetricsAnalytics(get_llm=my_llm, field_descriptions=descriptions)
    agent.json_to_sqlite(payload)
    result = agent.analyze("Кто лучший по выручке?", "контекст", "одна строка")
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import unicodedata
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, Type, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


# ============================================================================
# Константы
# ============================================================================

TABLE_NAME = "metrics"

# Регулярка для определения "ключи этого dict — периоды"
PERIOD_KEY_PATTERN = re.compile(r'^\d{4}([-_/].*|[QHW]\d{1,2})?$')

# Запрещённые SQL-команды (read-only guard на уровне pydantic)
FORBIDDEN_SQL_KEYWORDS = re.compile(
    r'\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|DETACH|PRAGMA|VACUUM|TRUNCATE)\b',
    re.IGNORECASE,
)

# Зарезервированные слова SQLite (для нормализации имён колонок)
SQLITE_RESERVED = frozenset({
    "abort", "action", "add", "after", "all", "alter", "analyze", "and", "as",
    "asc", "attach", "autoincrement", "before", "begin", "between", "by",
    "cascade", "case", "cast", "check", "collate", "column", "commit",
    "conflict", "constraint", "create", "cross", "current_date", "current_time",
    "current_timestamp", "database", "default", "deferrable", "deferred",
    "delete", "desc", "detach", "distinct", "drop", "each", "else", "end",
    "escape", "except", "exclusive", "exists", "explain", "fail", "for",
    "foreign", "from", "full", "glob", "group", "having", "if", "ignore",
    "immediate", "in", "index", "indexed", "initially", "inner", "insert",
    "instead", "intersect", "into", "is", "isnull", "join", "key", "left",
    "like", "limit", "match", "natural", "no", "not", "notnull", "null", "of",
    "offset", "on", "or", "order", "outer", "plan", "pragma", "primary",
    "query", "raise", "references", "regexp", "reindex", "release", "rename",
    "replace", "restrict", "right", "rollback", "row", "savepoint", "select",
    "set", "table", "temp", "temporary", "then", "to", "transaction", "trigger",
    "union", "unique", "update", "using", "vacuum", "values", "view", "virtual",
    "when", "where",
})

# Фиксированные колонки таблицы (после префиксов)
FIXED_COLUMNS = frozenset({
    "row_id", "object_role", "object_id", "object_name",
    "metric_id", "metric_local_id", "metric_name", "metric_level",
    "parent_metric_id", "period", "source_path",
})

# Лимиты
MAX_TEXT_LEN = 64 * 1024  # 64 KB
MAX_COLUMN_NAME_LEN = 60
MAX_DESCRIPTION_LEN = 200
SQL_RESULT_PREVIEW_ROWS = 50


# ============================================================================
# Pydantic-модели
# ============================================================================

class InputMetric(BaseModel):
    model_config = ConfigDict(extra='allow')
    id: Optional[Union[str, int]] = None
    name: Optional[str] = None
    period: Optional[str] = None
    children: List["InputMetric"] = Field(default_factory=list)


class InputObject(BaseModel):
    model_config = ConfigDict(extra='allow')
    id: Optional[Union[str, int]] = None
    name: Optional[str] = None
    period: Optional[str] = None
    metrics: List[InputMetric] = Field(default_factory=list)


class InputPayload(BaseModel):
    me: Union[InputObject, Dict[str, Any]] = Field(default_factory=dict)
    employees: List[InputObject] = Field(default_factory=list)

    @field_validator('me', mode='before')
    @classmethod
    def coerce_empty_me(cls, v: Any) -> Any:
        return v if v else {}


class SQLPlan(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reasoning: str
    sql: str
    expected_columns: List[str] = Field(default_factory=list)

    @field_validator('sql')
    @classmethod
    def must_be_select(cls, v: str) -> str:
        s = v.strip()
        while s.endswith(';'):
            s = s[:-1].strip()
        if not s:
            raise ValueError("sql is empty")
        head = s.split(None, 1)[0].upper()
        if head not in ('SELECT', 'WITH'):
            raise ValueError(
                f"only SELECT/WITH read queries are allowed, got '{head}'"
            )
        if FORBIDDEN_SQL_KEYWORDS.search(s):
            raise ValueError("DML/DDL keywords are forbidden")
        if ';' in s:
            raise ValueError("multiple statements (semicolons) are forbidden")
        return s


class FinalAnswer(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reasoning: str
    answer: str


class DBMetadata(BaseModel):
    n_objects: int
    n_employees: int
    has_me: bool
    n_periods: int
    periods_sample: List[str]
    n_unique_metrics: int
    n_metric_rows: int
    metric_levels: Dict[str, Dict[str, float]]
    columns: Dict[str, str]
    column_descriptions: Dict[str, str]
    warnings: List[str] = Field(default_factory=list)


# ============================================================================
# Внутренние исключения
# ============================================================================

class _RetriesExhausted(Exception):
    def __init__(self, errors: List[str], stage: str):
        self.errors = errors
        self.stage = stage
        super().__init__(f"[{stage}] retries exhausted ({len(errors)}): {errors}")


# ============================================================================
# Промпты
# ============================================================================

def _format_metadata_summary(metadata: DBMetadata) -> str:
    lines = [
        f"- Объектов всего: {metadata.n_objects} (руководитель: {metadata.has_me}, сотрудников: {metadata.n_employees})",
        f"- Уникальных метрик (по metric_local_id): {metadata.n_unique_metrics}",
        f"- Строк в таблице: {metadata.n_metric_rows}",
        f"- Уникальных периодов: {metadata.n_periods}",
    ]
    if metadata.periods_sample:
        lines.append(f"- Примеры периодов: {', '.join(metadata.periods_sample)}")
    if metadata.metric_levels:
        lines.append("- Распределение по уровням метрик:")
        for lvl in sorted(metadata.metric_levels.keys(), key=lambda x: int(x)):
            stats = metadata.metric_levels[lvl]
            parts = [f"уровень {lvl}"]
            if "node_count" in stats:
                parts.append(f"уник.метрик={int(stats['node_count'])}")
            if "avg_branching" in stats:
                parts.append(
                    f"ветвление avg={stats['avg_branching']:.2f} "
                    f"min={int(stats.get('min_branching', 0))} "
                    f"max={int(stats.get('max_branching', 0))}"
                )
            lines.append(f"    {', '.join(parts)}")
    if metadata.warnings:
        lines.append(f"- Предупреждения: {'; '.join(metadata.warnings)}")
    return "\n".join(lines)


def _format_columns_block(metadata: DBMetadata) -> str:
    lines = []
    for col, typ in metadata.columns.items():
        desc = metadata.column_descriptions.get(col, "")
        lines.append(f"  - {col} ({typ}): {desc}" if desc else f"  - {col} ({typ})")
    return "\n".join(lines)


def prompt_sql_generation(
    query: str,
    business_context: str,
    metadata: DBMetadata,
    table_ddl: str,
) -> str:
    return f"""Ты — SQL-аналитик, работающий с одной in-memory таблицей SQLite по имени `{TABLE_NAME}`.

ЖЁСТКИЕ ПРАВИЛА:
1. Возвращай ТОЛЬКО валидный JSON-объект формата:
   {{"reasoning": "<твои рассуждения>", "sql": "<один SELECT или WITH>", "expected_columns": ["<col1>", ...]}}
2. SQL должен начинаться с SELECT или WITH. Никаких INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/PRAGMA.
3. Только одно SQL-выражение, без точек с запятой внутри.
4. Никакого текста вне JSON-объекта.

DDL ТАБЛИЦЫ (с комментариями):
{table_ddl}

ОПИСАНИЯ КОЛОНОК:
{_format_columns_block(metadata)}

ХАРАКТЕРИСТИКИ ДАННЫХ:
{_format_metadata_summary(metadata)}

БИЗНЕС-КОНТЕКСТ ОТ ПОЛЬЗОВАТЕЛЯ:
{business_context}

ЗАПРОС ПОЛЬЗОВАТЕЛЯ:
{query}

Подумай пошагово в `reasoning` (какие колонки релевантны, какие фильтры/агрегации нужны),
а в `sql` верни сам запрос. Возвращай только JSON.
"""


def _format_result_preview(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "(пусто)"
    preview = rows[:SQL_RESULT_PREVIEW_ROWS]
    suffix = ""
    if len(rows) > SQL_RESULT_PREVIEW_ROWS:
        suffix = f"\n... ещё {len(rows) - SQL_RESULT_PREVIEW_ROWS} строк"
    return json.dumps(preview, ensure_ascii=False, default=str, indent=2) + suffix


def prompt_final_answer(
    query: str,
    business_context: str,
    response_format: str,
    sql: str,
    sql_reasoning: str,
    sql_result: List[Dict[str, Any]],
    metadata: DBMetadata,
) -> str:
    return f"""Ты — бизнес-аналитик. Сформулируй финальный ответ для пользователя на основе результата SQL.

ЖЁСТКИЕ ПРАВИЛА:
1. Возвращай ТОЛЬКО валидный JSON: {{"reasoning": "<твои рассуждения>", "answer": "<финальный ответ>"}}
2. Никакого текста вне JSON.
3. В поле `answer` соблюдай формат, заданный пользователем (см. ниже).

ИСХОДНЫЙ ЗАПРОС:
{query}

БИЗНЕС-КОНТЕКСТ:
{business_context}

ТРЕБУЕМЫЙ ФОРМАТ ОТВЕТА:
{response_format}

КРАТКАЯ СВОДКА БД:
{_format_metadata_summary(metadata)}

ВЫПОЛНЕННЫЙ SQL:
{sql}

ЛОГИКА SQL ОТ АНАЛИТИКА:
{sql_reasoning}

РЕЗУЛЬТАТ SQL (первые {SQL_RESULT_PREVIEW_ROWS} строк):
{_format_result_preview(sql_result)}

Сформулируй ответ. Возвращай только JSON.
"""


def prompt_with_feedback(base_prompt: str, errors: List[str]) -> str:
    error_block = "\n".join(f"- Попытка {i+1}: {e}" for i, e in enumerate(errors))
    return f"""Предыдущая(ие) попытка(и) провалилась:
{error_block}

Перегенерируй ответ, исправив указанные проблемы. Сохрани ту же JSON-схему вывода.

---

{base_prompt}
"""


# ============================================================================
# Класс MetricsAnalytics
# ============================================================================

class MetricsAnalytics:
    """In-memory SQLite + LLM анализ иерархичных метрик. Не thread-safe."""

    TABLE = TABLE_NAME
    DEFAULT_MAX_RETRIES = 3
    DEFAULT_SQL_TIMEOUT_SEC = 5.0
    DEFAULT_ROW_LIMIT = 10_000

    def __init__(
        self,
        get_llm: Callable[[str], str],
        field_descriptions: Dict[str, str],
        *,
        max_retries: int = DEFAULT_MAX_RETRIES,
        sql_timeout_sec: float = DEFAULT_SQL_TIMEOUT_SEC,
        row_limit: int = DEFAULT_ROW_LIMIT,
    ) -> None:
        self._get_llm = get_llm
        self._descriptions = dict(field_descriptions or {})
        self._max_retries = max_retries
        self._sql_timeout = sql_timeout_sec
        self._row_limit = row_limit

        self._conn: Optional[sqlite3.Connection] = None
        # column_name → original_key (для доступа к описанию)
        self._reverse_key_map: Dict[str, str] = {}
        # column_name → sqlite_type
        self._columns_in_order: List[Tuple[str, str]] = []
        # column_name → comment_text (что вошло в DDL)
        self._column_comments: Dict[str, str] = {}
        self._warnings: List[str] = []

    # ------------------------------------------------------------------
    # ПУБЛИЧНЫЙ API
    # ------------------------------------------------------------------

    def json_to_sqlite(self, data: Dict[str, Any]) -> None:
        """Валидирует payload, реинит in-memory БД, строит CREATE TABLE c
        комментариями, заполняет данными. ValueError на ошибке валидации."""
        # Валидация ДО мутации БД
        try:
            payload = InputPayload.model_validate(data)
        except ValidationError as e:
            raise ValueError(f"invalid input JSON: {self._format_pydantic_errors(e)}")

        if not payload.employees and (
            isinstance(payload.me, dict) and not payload.me
            or isinstance(payload.me, InputObject) and not (
                payload.me.metrics or payload.me.id or payload.me.name
            )
        ):
            raise ValueError(
                "payload contains neither non-empty 'me' nor any 'employees'"
            )

        # Создаём/пересоздаём соединение
        if self._conn is None:
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._conn.row_factory = sqlite3.Row

        self._reset_db()
        self._warnings = []

        # Discovery pass: собрать все ключи и типы
        obj_keys, metric_keys = self._discover_schema(payload)

        # Построить нормализованные имена колонок
        obj_columns = self._build_namespaced_columns(obj_keys, namespace="obj")
        metric_columns = self._build_namespaced_columns(metric_keys, namespace="m")

        # CREATE TABLE
        ddl = self._build_create_table(obj_columns, metric_columns)
        self._conn.executescript(ddl)

        # Сохранить порядок колонок
        cur = self._conn.execute(f"PRAGMA table_info({TABLE_NAME})")
        self._columns_in_order = [(r["name"], r["type"]) for r in cur.fetchall()]

        # Populate
        self._populate(payload)

        # Предупреждения по неиспользованным описаниям
        used_orig_keys = set(self._reverse_key_map.values())
        used_orig_keys.update({"name", "metric_name"})
        for desc_key in self._descriptions:
            if (desc_key not in used_orig_keys
                    and desc_key not in {c[0] for c in self._columns_in_order}):
                self._warnings.append(
                    f"description for '{desc_key}' provided but not seen in JSON"
                )

        # Размер БД
        page_count = self._conn.execute("PRAGMA page_count").fetchone()[0]
        page_size = self._conn.execute("PRAGMA page_size").fetchone()[0]
        n_rows = self._conn.execute(
            f"SELECT COUNT(*) FROM {TABLE_NAME}"
        ).fetchone()[0]
        size_mb = (page_count * page_size) / (1024 * 1024)
        print(
            f"[MetricsAnalytics] DB size: {size_mb:.2f} MB "
            f"({n_rows} rows, {len(self._columns_in_order)} columns)"
        )

    def analyze(
        self,
        query: str,
        business_context: str,
        response_format: str,
    ) -> Dict[str, Any]:
        """Полный пайплайн: discover metadata → LLM SQL (retry) → execute →
        LLM final answer (retry) → return dict."""
        partial = {
            "answer": "",
            "sql": "",
            "sql_result": [],
            "reasoning": "",
            "metadata": {},
        }

        if self._conn is None or not self._table_exists():
            return self._fail(
                partial, "input_validation", "DBNotInitialized",
                "DB не инициализирована — вызовите json_to_sqlite сначала.",
                retries=0,
            )

        # 1) Метаданные
        try:
            metadata = self._compute_metadata()
            partial["metadata"] = metadata.model_dump()
        except Exception as e:
            return self._fail(
                partial, "input_validation", type(e).__name__, str(e), retries=0
            )

        # 2) Генерация SQL
        try:
            sql_plan, sql_attempts = self._generate_sql(
                query, business_context, metadata
            )
            partial["sql"] = sql_plan.sql
            partial["reasoning"] = f"[SQL] {sql_plan.reasoning}"
        except _RetriesExhausted as e:
            return self._fail(
                partial, "sql_generation", "RetriesExhausted",
                "; ".join(e.errors), retries=len(e.errors),
            )

        # 3) Выполнение SQL
        try:
            sql_result = self._execute_sql(sql_plan.sql)
            partial["sql_result"] = sql_result
        except Exception as e:
            return self._fail(
                partial, "sql_execution", type(e).__name__, str(e),
                retries=sql_attempts,
            )

        # 4) Финальный ответ
        try:
            final, final_attempts = self._generate_final_answer(
                query, business_context, response_format,
                sql_plan, sql_result, metadata,
            )
            partial["answer"] = final.answer
            partial["reasoning"] += f"\n\n[FINAL] {final.reasoning}"
        except _RetriesExhausted as e:
            return self._fail(
                partial, "final_answer", "RetriesExhausted",
                "; ".join(e.errors), retries=len(e.errors),
            )

        return partial

    # ------------------------------------------------------------------
    # РЕИНИТ БД
    # ------------------------------------------------------------------

    def _reset_db(self) -> None:
        assert self._conn is not None
        cur = self._conn.cursor()
        # Дропнуть индексы и таблицу
        for row in cur.execute(
            "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'index')"
        ).fetchall():
            name, typ = row["name"], row["type"]
            if name.startswith("sqlite_"):
                continue
            cur.execute(f'DROP {typ.upper()} IF EXISTS "{name}"')
        self._conn.commit()
        self._reverse_key_map = {}
        self._columns_in_order = []
        self._column_comments = {}

    def _table_exists(self) -> bool:
        if self._conn is None:
            return False
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (TABLE_NAME,),
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # SCHEMA DISCOVERY
    # ------------------------------------------------------------------

    # Ключи, которые всегда идут в фиксированные колонки (не дублировать как obj_*/m_*)
    _OBJ_BUILTIN_KEYS = frozenset({"id", "name", "period", "metrics"})
    _METRIC_BUILTIN_KEYS = frozenset({"id", "name", "period", "children"})

    def _discover_schema(
        self, payload: InputPayload
    ) -> Tuple[Dict[str, set], Dict[str, set]]:
        """Returns (obj_keys, metric_keys), each: original_key → set[type]."""
        obj_keys: Dict[str, set] = {}
        metric_keys: Dict[str, set] = {}

        for obj in self._iter_objects(payload):
            obj_dump = obj.model_dump()
            for k, v in obj_dump.items():
                if k in self._OBJ_BUILTIN_KEYS:
                    continue
                obj_keys.setdefault(k, set()).add(self._observed_type(v))
            for metric in obj.metrics:
                self._discover_metric_keys(metric, metric_keys)

        return obj_keys, metric_keys

    def _discover_metric_keys(
        self, metric: InputMetric, accum: Dict[str, set]
    ) -> None:
        dump = metric.model_dump()
        for k, v in dump.items():
            if k in self._METRIC_BUILTIN_KEYS:
                continue
            if isinstance(v, dict) and self._is_period_dict(v):
                # Колонка хранит inner-значение
                for inner_v in v.values():
                    accum.setdefault(k, set()).add(self._observed_type(inner_v))
            else:
                accum.setdefault(k, set()).add(self._observed_type(v))
        for child in metric.children:
            self._discover_metric_keys(child, accum)

    def _iter_objects(self, payload: InputPayload) -> Iterator[InputObject]:
        if isinstance(payload.me, InputObject):
            me = payload.me
            if me.metrics or me.id is not None or me.name is not None:
                yield me
        elif isinstance(payload.me, dict) and payload.me:
            yield InputObject.model_validate(payload.me)
        for emp in payload.employees:
            yield emp

    @staticmethod
    def _observed_type(v: Any) -> type:
        if v is None:
            return type(None)
        return type(v)

    @staticmethod
    def _is_period_dict(d: Dict[Any, Any]) -> bool:
        if not d:
            return False
        return all(
            isinstance(k, str) and PERIOD_KEY_PATTERN.match(k) for k in d.keys()
        )

    @staticmethod
    def _infer_type(observed: set) -> str:
        types = observed - {type(None)}
        if not types:
            return "TEXT"
        if all(t is bool or t is int for t in types):
            return "INTEGER"
        if all(t is bool or t is int or t is float for t in types):
            return "REAL"
        return "TEXT"

    # ------------------------------------------------------------------
    # NORMALIZATION + DDL
    # ------------------------------------------------------------------

    def _normalize_key(self, key: str, namespace: str, taken: set) -> str:
        # ASCII-fy
        s = unicodedata.normalize("NFKD", str(key)).encode("ascii", "ignore").decode("ascii")
        s = s.lower()
        s = re.sub(r"[^a-z0-9]+", "_", s)
        s = re.sub(r"_+", "_", s).strip("_")
        if not s:
            s = "field"
        if s[0].isdigit():
            s = "_" + s
        # Префикс namespace
        col = f"{namespace}_{s}" if namespace else s
        # Truncate
        col = col[:MAX_COLUMN_NAME_LEN]
        # Конфликты с фиксированными колонками или зарезервированными словами
        if col in FIXED_COLUMNS or col.lower() in SQLITE_RESERVED:
            col = f"{col}_x"
        # Уникальность
        base = col
        i = 1
        while col in taken:
            suffix = f"_{i}"
            col = base[:MAX_COLUMN_NAME_LEN - len(suffix)] + suffix
            i += 1
        return col

    def _build_namespaced_columns(
        self, keys: Dict[str, set], namespace: str
    ) -> List[Tuple[str, str, str]]:
        """Returns list of (column_name, sqlite_type, original_key)."""
        taken = set(FIXED_COLUMNS)
        # Уже взятые колонки из второго namespace учитываются вызывающим контекстом —
        # но префиксы obj_/m_ предотвращают коллизии между namespace'ами.
        out: List[Tuple[str, str, str]] = []
        for orig_key in sorted(keys.keys()):
            sqlite_type = self._infer_type(keys[orig_key])
            col = self._normalize_key(orig_key, namespace, taken)
            taken.add(col)
            self._reverse_key_map[col] = orig_key
            out.append((col, sqlite_type, orig_key))
        return out

    def _resolve_description(self, original_key: str, normalized_col: str) -> str:
        # Сначала пробуем оригинальный JSON-ключ, потом нормализованное имя
        for k in (original_key, normalized_col):
            if k in self._descriptions:
                return self._descriptions[k]
        return ""

    @staticmethod
    def _sanitize_comment(s: str) -> str:
        s = str(s).replace("\r", " ").replace("\n", " ").replace("--", "- -")
        s = re.sub(r"\s+", " ", s).strip()
        return s[:MAX_DESCRIPTION_LEN]

    def _build_create_table(
        self,
        obj_columns: List[Tuple[str, str, str]],
        metric_columns: List[Tuple[str, str, str]],
    ) -> str:
        # (col, type_def, description)
        rows: List[Tuple[str, str, str]] = []

        def add(col: str, type_def: str, desc: str) -> None:
            rows.append((col, type_def, desc))
            self._column_comments[col] = self._sanitize_comment(desc)

        # Identity
        add("row_id", "INTEGER PRIMARY KEY AUTOINCREMENT",
            "auto-incrementing row identifier")
        add("object_role", "TEXT NOT NULL",
            "'me' for the manager, 'employee' for subordinates")
        add("object_id", "TEXT NOT NULL",
            "object identifier as supplied in JSON, stringified")
        add("object_name", "TEXT",
            self._descriptions.get("name", "human-readable name of the object"))

        # Object fields
        for col, typ, orig_key in obj_columns:
            if col in {"object_role", "object_id", "object_name"}:
                continue
            desc = self._resolve_description(orig_key, col) or "(no description)"
            add(col, typ, desc)

        # Metric identity
        add("metric_id", "TEXT NOT NULL",
            "globally unique materialized path of the metric")
        add("metric_local_id", "TEXT",
            "metric id as appeared in JSON; not unique across objects")
        add("metric_name", "TEXT",
            self._descriptions.get("metric_name",
                                   self._descriptions.get("name", "human-readable name of the metric")))
        add("metric_level", "INTEGER NOT NULL",
            "depth in the metric tree (0 = root metric of the object)")
        add("parent_metric_id", "TEXT",
            "metric_id of the parent metric; NULL for level 0")

        # Period
        add("period", "TEXT",
            self._descriptions.get("period",
                                   "time period label resolved (metric > ancestor > object); may be NULL"))

        # Metric fields
        for col, typ, orig_key in metric_columns:
            if col in {"metric_id", "metric_local_id", "metric_name",
                       "metric_level", "parent_metric_id", "period"}:
                continue
            desc = self._resolve_description(orig_key, col) or "(no description)"
            add(col, typ, desc)

        # Provenance
        add("source_path", "TEXT",
            "JSON-pointer-like path of the metric node in the source payload")

        # Сборка DDL
        lines = [f"CREATE TABLE {TABLE_NAME} ("]
        for i, (col, type_def, desc) in enumerate(rows):
            sep = "," if i < len(rows) - 1 else ""
            comment = self._sanitize_comment(desc)
            lines.append(f'    "{col}" {type_def}{sep}  -- {comment}')
        lines.append(");")
        ddl = "\n".join(lines)

        # Индексы
        ddl += (
            f"\nCREATE INDEX idx_{TABLE_NAME}_object   "
            f"ON {TABLE_NAME}(object_role, object_id);"
            f"\nCREATE INDEX idx_{TABLE_NAME}_parent   "
            f"ON {TABLE_NAME}(parent_metric_id);"
            f"\nCREATE INDEX idx_{TABLE_NAME}_period   "
            f"ON {TABLE_NAME}(period);"
            f"\nCREATE INDEX idx_{TABLE_NAME}_name     "
            f"ON {TABLE_NAME}(metric_name);"
        )
        return ddl

    # ------------------------------------------------------------------
    # POPULATE
    # ------------------------------------------------------------------

    def _populate(self, payload: InputPayload) -> None:
        rows: List[Dict[str, Any]] = []
        for obj in self._iter_objects(payload):
            role = "me" if self._is_me(obj, payload) else "employee"
            rows.extend(self._walk_object(obj, role))
        if rows:
            self._insert_rows(rows)

    def _is_me(self, obj: InputObject, payload: InputPayload) -> bool:
        if isinstance(payload.me, InputObject):
            return obj is payload.me
        return False

    def _walk_object(
        self, obj: InputObject, role: str
    ) -> Iterator[Dict[str, Any]]:
        obj_dump = obj.model_dump()
        obj_id_str = str(obj_dump.get("id") or f"{role}_{id(obj)}")
        obj_name = obj_dump.get("name")
        object_period = obj_dump.get("period")

        # Поля объекта (всё кроме metrics)
        obj_extra: Dict[str, Any] = {
            k: v for k, v in obj_dump.items() if k not in {"metrics"}
        }
        # base_row: фиксированные колонки объекта
        base_row: Dict[str, Any] = {
            "object_role": role,
            "object_id": obj_id_str,
            "object_name": obj_name,
        }
        # Денормализованные obj_-поля
        for orig_key, val in obj_extra.items():
            if orig_key in self._OBJ_BUILTIN_KEYS:
                continue  # уже в фикс.колонках
            col = self._find_column(orig_key, namespace="obj")
            if col:
                base_row[col] = self._serialize_value(val)

        # Корневые метрики
        for sibling_idx, root_metric in enumerate(obj.metrics):
            yield from self._walk_metric(
                root_metric,
                parent_path=None,
                parent_metric_id=None,
                level=0,
                sibling_idx=sibling_idx,
                role=role,
                obj_id_str=obj_id_str,
                object_period=object_period,
                base_row=base_row,
            )

    def _walk_metric(
        self,
        metric: InputMetric,
        parent_path: Optional[str],
        parent_metric_id: Optional[str],
        level: int,
        sibling_idx: int,
        role: str,
        obj_id_str: str,
        object_period: Optional[str],
        base_row: Dict[str, Any],
        ancestor_period: Optional[str] = None,
    ) -> Iterator[Dict[str, Any]]:
        m_dump = metric.model_dump()
        local_id_raw = m_dump.get("id")
        local_id_str = str(local_id_raw) if local_id_raw is not None else f"_idx{sibling_idx}"

        if parent_path is None:
            path = f"{role}:{obj_id_str}/{local_id_str}"
        else:
            path = f"{parent_path}/{local_id_str}"

        metric_name = m_dump.get("name")
        direct_period = m_dump.get("period")
        # Резолвим период: метрика → предок → объект
        resolved_fallback = direct_period or ancestor_period or object_period
        # Раскрываем периоды (если value/target/... — dict с ключами-периодами)
        period_buckets = self._expand_periods(m_dump, resolved_fallback)

        for period, value_view in period_buckets:
            row: Dict[str, Any] = dict(base_row)
            row.update({
                "metric_id": path,
                "metric_local_id": str(local_id_raw) if local_id_raw is not None else None,
                "metric_name": metric_name,
                "metric_level": level,
                "parent_metric_id": parent_metric_id,
                "period": period,
                "source_path": path,
            })
            # Поля метрики из value_view
            for orig_key, val in value_view.items():
                if orig_key in self._METRIC_BUILTIN_KEYS:
                    continue
                col = self._find_column(orig_key, namespace="m")
                if col:
                    row[col] = self._serialize_value(val)
            yield row

        # Рекурсия в children
        for child_idx, child in enumerate(metric.children):
            yield from self._walk_metric(
                child,
                parent_path=path,
                parent_metric_id=path,
                level=level + 1,
                sibling_idx=child_idx,
                role=role,
                obj_id_str=obj_id_str,
                object_period=object_period,
                base_row=base_row,
                ancestor_period=resolved_fallback,
            )

    def _expand_periods(
        self, m_dump: Dict[str, Any], fallback_period: Optional[str]
    ) -> List[Tuple[Optional[str], Dict[str, Any]]]:
        period_keyed: Dict[str, Dict[str, Any]] = {}
        static: Dict[str, Any] = {}
        for k, v in m_dump.items():
            if k in self._METRIC_BUILTIN_KEYS:
                continue
            if isinstance(v, dict) and self._is_period_dict(v):
                period_keyed[k] = v
            else:
                static[k] = v

        if not period_keyed:
            return [(fallback_period, static)]

        all_periods = sorted({p for d in period_keyed.values() for p in d.keys()})
        buckets: List[Tuple[Optional[str], Dict[str, Any]]] = []
        for period in all_periods:
            view = dict(static)
            for fname, vals in period_keyed.items():
                view[fname] = vals.get(period)
            buckets.append((period, view))
        return buckets

    def _find_column(self, original_key: str, namespace: str) -> Optional[str]:
        for col, orig in self._reverse_key_map.items():
            if orig == original_key and col.startswith(f"{namespace}_"):
                return col
        return None

    @staticmethod
    def _serialize_value(v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, bool):
            return 1 if v else 0
        if isinstance(v, (int, float)):
            return v
        if isinstance(v, str):
            if len(v) > MAX_TEXT_LEN:
                return v[: MAX_TEXT_LEN - 20] + "...[truncated]"
            return v
        if isinstance(v, bytes):
            return sqlite3.Binary(v)
        # dict / list / прочее → JSON-string
        try:
            return json.dumps(v, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(v)

    def _insert_rows(self, rows: List[Dict[str, Any]]) -> None:
        assert self._conn is not None
        if not rows:
            return
        all_cols = [c for c, _ in self._columns_in_order if c != "row_id"]
        placeholders = ", ".join("?" * len(all_cols))
        quoted = ", ".join(f'"{c}"' for c in all_cols)
        sql = f'INSERT INTO {TABLE_NAME} ({quoted}) VALUES ({placeholders})'
        cur = self._conn.cursor()
        for row in rows:
            values = tuple(row.get(c) for c in all_cols)
            cur.execute(sql, values)
        self._conn.commit()

    # ------------------------------------------------------------------
    # METADATA
    # ------------------------------------------------------------------

    def _compute_metadata(self) -> DBMetadata:
        assert self._conn is not None
        c = self._conn.cursor()
        n_emp = c.execute(
            f"SELECT COUNT(DISTINCT object_id) FROM {TABLE_NAME} "
            f"WHERE object_role='employee'"
        ).fetchone()[0]
        has_me = c.execute(
            f"SELECT EXISTS(SELECT 1 FROM {TABLE_NAME} WHERE object_role='me')"
        ).fetchone()[0] == 1
        n_objects = n_emp + (1 if has_me else 0)

        n_periods = c.execute(
            f"SELECT COUNT(DISTINCT period) FROM {TABLE_NAME} WHERE period IS NOT NULL"
        ).fetchone()[0]
        periods_sample = [
            r[0] for r in c.execute(
                f"SELECT DISTINCT period FROM {TABLE_NAME} "
                f"WHERE period IS NOT NULL ORDER BY period LIMIT 10"
            ).fetchall()
        ]

        n_unique_metrics = c.execute(
            f"SELECT COUNT(DISTINCT metric_local_id) FROM {TABLE_NAME} "
            f"WHERE metric_local_id IS NOT NULL"
        ).fetchone()[0]
        n_metric_rows = c.execute(
            f"SELECT COUNT(*) FROM {TABLE_NAME}"
        ).fetchone()[0]

        levels: Dict[str, Dict[str, float]] = {}
        for row in c.execute(
            f"SELECT metric_level, COUNT(DISTINCT metric_local_id) "
            f"FROM {TABLE_NAME} GROUP BY metric_level"
        ).fetchall():
            lvl, n = row[0], row[1]
            levels.setdefault(str(lvl), {})["node_count"] = float(n)

        for row in c.execute(f"""
            WITH child_counts AS (
                SELECT parent_metric_id,
                       metric_level - 1 AS parent_level,
                       COUNT(DISTINCT metric_local_id) AS k
                FROM {TABLE_NAME}
                WHERE parent_metric_id IS NOT NULL
                GROUP BY parent_metric_id, parent_level
            )
            SELECT parent_level, AVG(k), MIN(k), MAX(k), COUNT(*)
            FROM child_counts GROUP BY parent_level
        """).fetchall():
            parent_level, avg_k, min_k, max_k, n_parents = row
            entry = levels.setdefault(str(parent_level), {})
            entry["avg_branching"] = float(avg_k)
            entry["min_branching"] = float(min_k)
            entry["max_branching"] = float(max_k)
            entry["parents_at_level"] = float(n_parents)

        # Колонки и описания
        columns: Dict[str, str] = {}
        for r in c.execute(f"PRAGMA table_info({TABLE_NAME})").fetchall():
            columns[r["name"]] = r["type"]
        # Описания: из self._column_comments (то что вошло в DDL)
        column_descriptions = dict(self._column_comments)

        return DBMetadata(
            n_objects=n_objects,
            n_employees=int(n_emp),
            has_me=has_me,
            n_periods=int(n_periods),
            periods_sample=periods_sample,
            n_unique_metrics=int(n_unique_metrics),
            n_metric_rows=int(n_metric_rows),
            metric_levels=levels,
            columns=columns,
            column_descriptions=column_descriptions,
            warnings=list(self._warnings),
        )

    def get_table_ddl(self) -> str:
        """Возвращает оригинальный CREATE TABLE с -- комментариями (из sqlite_master)."""
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (TABLE_NAME,),
        ).fetchone()
        return row[0] if row else ""

    # ------------------------------------------------------------------
    # SQL EXECUTION
    # ------------------------------------------------------------------

    def _execute_sql(self, sql: str) -> List[Dict[str, Any]]:
        """Выполняет SELECT с timeout и row_limit."""
        assert self._conn is not None
        deadline = time.monotonic() + self._sql_timeout

        def progress():
            return 1 if time.monotonic() > deadline else 0

        self._conn.set_progress_handler(progress, 1000)
        try:
            cur = self._conn.execute(sql)
            fetched = cur.fetchmany(self._row_limit + 1)
        finally:
            self._conn.set_progress_handler(None, 0)

        truncated = len(fetched) > self._row_limit
        if truncated:
            fetched = fetched[: self._row_limit]
        result = [dict(r) for r in fetched]
        if truncated:
            # пометка (не часть строк, а отдельный sentinel)
            result.append({
                "__note__": f"truncated to {self._row_limit} rows",
            })
        return result

    # ------------------------------------------------------------------
    # LLM ВЗАИМОДЕЙСТВИЕ
    # ------------------------------------------------------------------

    def _generate_sql(
        self,
        query: str,
        business_context: str,
        metadata: DBMetadata,
    ) -> Tuple[SQLPlan, int]:
        table_ddl = self.get_table_ddl()
        base_prompt = prompt_sql_generation(
            query, business_context, metadata, table_ddl
        )

        def runtime_check(plan: SQLPlan) -> None:
            try:
                self._conn.execute(f"EXPLAIN {plan.sql}").fetchall()
            except sqlite3.Error as e:
                cols = list(metadata.columns.keys())
                preview = cols[:30]
                more = f" (+{len(cols) - 30} more)" if len(cols) > 30 else ""
                raise ValueError(
                    f"SQL error from EXPLAIN: {e}. "
                    f"Available columns: {preview}{more}"
                )

        result, errors = self._llm_json_call(
            base_prompt, SQLPlan, runtime_check, stage="sql_generation"
        )
        return result, len(errors)

    def _generate_final_answer(
        self,
        query: str,
        business_context: str,
        response_format: str,
        sql_plan: SQLPlan,
        sql_result: List[Dict[str, Any]],
        metadata: DBMetadata,
    ) -> Tuple[FinalAnswer, int]:
        base_prompt = prompt_final_answer(
            query, business_context, response_format,
            sql_plan.sql, sql_plan.reasoning, sql_result, metadata,
        )
        result, errors = self._llm_json_call(
            base_prompt, FinalAnswer, runtime_check=None, stage="final_answer"
        )
        return result, len(errors)

    def _llm_json_call(
        self,
        base_prompt: str,
        schema: Type[BaseModel],
        runtime_check: Optional[Callable[[BaseModel], None]],
        stage: str,
    ) -> Tuple[BaseModel, List[str]]:
        errors: List[str] = []
        for _attempt in range(self._max_retries):
            prompt = (
                prompt_with_feedback(base_prompt, errors) if errors else base_prompt
            )
            raw = self._get_llm(prompt)
            try:
                parsed_dict = self._parse_json(raw)
            except (json.JSONDecodeError, ValueError) as e:
                errors.append(f"JSON parse error: {e}. Raw start: {raw[:200]!r}")
                continue
            try:
                obj = schema.model_validate(parsed_dict)
            except ValidationError as e:
                errors.append(f"pydantic validation: {self._format_pydantic_errors(e)}")
                continue
            if runtime_check is not None:
                try:
                    runtime_check(obj)
                except Exception as e:
                    errors.append(f"runtime check: {e}")
                    continue
            return obj, errors
        raise _RetriesExhausted(errors, stage)

    @staticmethod
    def _parse_json(raw: str) -> Dict[str, Any]:
        s = raw.strip()
        # Удалим markdown-обёртку ```json ... ```
        if s.startswith("```"):
            s = re.sub(r"^```(?:json)?\s*", "", s)
            s = re.sub(r"\s*```\s*$", "", s)
        # Найти первый { и парный к нему }
        start = s.find("{")
        if start < 0:
            raise ValueError("no JSON object found in LLM output")
        depth = 0
        end = -1
        in_str = False
        escape = False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end < 0:
            raise ValueError("unterminated JSON object in LLM output")
        return json.loads(s[start:end])

    @staticmethod
    def _format_pydantic_errors(err: ValidationError) -> str:
        out = []
        for e in err.errors()[:5]:
            loc = ".".join(str(p) for p in e.get("loc", []))
            msg = e.get("msg", "")
            out.append(f"{loc}: {msg}")
        return "; ".join(out)

    # ------------------------------------------------------------------
    # FAIL HELPER
    # ------------------------------------------------------------------

    @staticmethod
    def _fail(
        partial: Dict[str, Any],
        stage: str,
        err_type: str,
        message: str,
        retries: int,
    ) -> Dict[str, Any]:
        result = dict(partial)
        result["error"] = {
            "stage": stage,
            "type": err_type,
            "message": message,
            "retries": retries,
        }
        return result
