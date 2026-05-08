# MetricsAnalytics

Встраиваемый Python-класс для аналитики иерархичных метрик. Загружает JSON в in-memory SQLite, отвечает на бизнес-вопросы через LLM (генерация SQL в JSON, валидация pydantic, retry с фидбэком).

## Установка

```bash
pip install -r requirements.txt
```

Зависимости: `pydantic>=2.6,<3` и stdlib. Python 3.9+.

## Использование

```python
from metrics_analytics import MetricsAnalytics

def my_llm(prompt: str) -> str:
    # любой клиент (Anthropic / OpenAI / локальная модель)
    ...

descriptions = {
    "name":    "ФИО объекта",
    "value":   "Числовое значение метрики",
    "period":  "Отчётный период",
    "region":  "Регион",
    # ...
}

agent = MetricsAnalytics(get_llm=my_llm, field_descriptions=descriptions)

payload = {
    "me": {"id": "m1", "name": "Boss", "metrics": [...]},
    "employees": [
        {"id": "e1", "name": "Alice", "department": "sales", "metrics": [...]},
    ],
}

agent.json_to_sqlite(payload)
# [MetricsAnalytics] DB size: 0.03 MB (5 rows, 14 columns)

result = agent.analyze(
    query="Кто лидер по сделкам в Q1?",
    business_context="воронка продаж",
    response_format="одна короткая фраза",
)
print(result["answer"])
```

### Deep-режим: итеративное обследование БД

```python
result = agent.analyze(
    query="Какие аномалии в данных?",
    business_context="...",
    response_format="bullet list",
    deep=True,                  # многошаговый цикл
    max_inspection_steps=7,     # опционально, override
)
```

В `deep=True` вместо одного SQL-запроса агент делает **итеративный цикл**: на каждом шаге LLM решает — запустить ещё один `SELECT` (DISTINCT, COUNT, агрегации, выборки) или дать финальный ответ. Replan происходит естественно: каждый шаг LLM видит всю историю и пересматривает курс. В возврате — поле `investigation_steps` со всем трейсом исследования.

## Возвращаемая структура `analyze`

```python
{
    "answer":              str,   # финальный ответ от LLM
    "sql":                 str,   # последний выполненный SQL
    "sql_result":          list,  # результат последнего SQL
    "reasoning":           str,   # рассуждения LLM (склейка по шагам)
    "metadata":            dict,  # автоопределённые характеристики БД
    "investigation_steps": list,  # только в deep=True: трейс [{step_num, sql, result_full, ...}]
    "error":               dict,  # только при hard-fail (stage, type, message, retries)
}
```

## Структура единой таблицы `metrics`

Одна строка = одна тройка (объект, метрика, период). Иерархия — adjacency list через `parent_metric_id` (materialized path).

Фиксированные колонки: `row_id`, `object_role`, `object_id`, `object_name`, `metric_id`, `metric_local_id`, `metric_name`, `metric_level`, `parent_metric_id`, `period`, `source_path`. Поля объекта/метрики, обнаруженные в JSON, — с префиксами `obj_*` / `m_*`. Описания из `field_descriptions` встраиваются как `--` комментарии в DDL и сохраняются в `sqlite_master.sql`.

Период резолвится по приоритету: `metric.period` → ближайший предок-метрика → `object.period` → `NULL`. Если значение метрики — dict с ключами-периодами (`{"2024-Q1": 100, "2024-Q2": 150}`), walker раскрывает его в N строк.

## Обработка ошибок

- **Read-only SQL** в 2 слоя: pydantic-regex (`SELECT`/`WITH` only) + `EXPLAIN` перед execute.
- **Retry с фидбэком**: при невалидном JSON / ошибке pydantic / SQL-ошибке отправляем текст ошибки обратно в LLM (макс 3 попытки).
- **Fail-fast** после исчерпания ретраев: возвращается dict с ключом `error`, исключения наружу не пробрасываются.
- **Timeout SQL** через `set_progress_handler`, **row limit** на выходе (по умолчанию 10000).
- **Валидация входного JSON** через pydantic — ошибка до мутации БД, БД остаётся прежней.

## Тесты

```bash
pip install pytest
python3 -m pytest tests/
```

30 тестов: schema/load, metadata, analyze (simple + deep) со StubLLM (без реальных вызовов к модели).

## Файлы

- `metrics_analytics.py` — единственный имплементационный файл (класс + pydantic + промпты).
- `tests/` — pytest-тесты + fixture-данные.
- `requirements.txt` — зависимости.
