"""Тестовые fixture-данные для metrics_analytics."""

# Минимальный пример: руководитель + 2 сотрудника, 2 уровня вложенности
SAMPLE_MIN = {
    "me": {
        "id": "m1",
        "name": "Boss",
        "position": "CEO",
        "metrics": [
            {
                "id": "rev",
                "name": "Revenue",
                "value": 1000,
                "period": "2024-Q1",
                "children": [],
            }
        ],
    },
    "employees": [
        {
            "id": "e1",
            "name": "Alice",
            "department": "sales",
            "metrics": [
                {
                    "id": "deals",
                    "name": "Deals",
                    "value": 12,
                    "period": "2024-Q1",
                    "children": [
                        {"id": "won", "name": "Won", "value": 8, "period": "2024-Q1"},
                        {"id": "lost", "name": "Lost", "value": 4, "period": "2024-Q1"},
                    ],
                }
            ],
        },
        {
            "id": "e2",
            "name": "Bob",
            "department": "engineering",
            "metrics": [
                {
                    "id": "deals",
                    "name": "Deals",
                    "value": 5,
                    "period": "2024-Q1",
                    "children": [],
                }
            ],
        },
    ],
}

# Период как dict-ключи в значении метрики
SAMPLE_PERIOD_DICT = {
    "me": {},
    "employees": [
        {
            "id": "e1",
            "name": "Bob",
            "metrics": [
                {
                    "id": "rev",
                    "name": "Revenue",
                    "value": {
                        "2024-Q1": 100,
                        "2024-Q2": 150,
                        "2024-Q3": 200,
                        "2024-Q4": 180,
                    },
                    "children": [],
                }
            ],
        }
    ],
}

# Период на уровне объекта, не на уровне метрики
SAMPLE_OBJECT_PERIOD = {
    "me": {},
    "employees": [
        {
            "id": "e1",
            "name": "Carol",
            "period": "2024-Q2",
            "metrics": [
                {
                    "id": "rev",
                    "name": "Revenue",
                    "value": 500,
                    "children": [
                        {"id": "online", "name": "Online", "value": 300},
                        {"id": "offline", "name": "Offline", "value": 200},
                    ],
                }
            ],
        }
    ],
}

# Гетерогенные схемы: у одного есть `region`, у другого нет
SAMPLE_HETEROGENEOUS = {
    "me": {},
    "employees": [
        {
            "id": "e1",
            "name": "Alice",
            "region": "EU",
            "metrics": [{"id": "rev", "name": "Revenue", "value": 100, "period": "2024"}],
        },
        {
            "id": "e2",
            "name": "Bob",
            "metrics": [{"id": "rev", "name": "Revenue", "value": 200, "period": "2024"}],
        },
    ],
}

# Глубокая иерархия (4 уровня)
SAMPLE_DEEP_TREE = {
    "me": {},
    "employees": [
        {
            "id": "e1",
            "name": "Tree",
            "metrics": [
                {
                    "id": "L0",
                    "name": "Total",
                    "value": 1000,
                    "period": "2024-Q1",
                    "children": [
                        {
                            "id": "L1A",
                            "name": "BranchA",
                            "value": 600,
                            "period": "2024-Q1",
                            "children": [
                                {
                                    "id": "L2A",
                                    "name": "SubA",
                                    "value": 400,
                                    "period": "2024-Q1",
                                    "children": [
                                        {"id": "L3A", "name": "LeafA", "value": 250, "period": "2024-Q1"},
                                        {"id": "L3B", "name": "LeafB", "value": 150, "period": "2024-Q1"},
                                    ],
                                },
                                {
                                    "id": "L2B",
                                    "name": "SubB",
                                    "value": 200,
                                    "period": "2024-Q1",
                                    "children": [],
                                },
                            ],
                        },
                        {
                            "id": "L1B",
                            "name": "BranchB",
                            "value": 400,
                            "period": "2024-Q1",
                            "children": [],
                        },
                    ],
                }
            ],
        }
    ],
}

# Зарезервированные слова в JSON-ключах
SAMPLE_RESERVED_WORDS = {
    "me": {},
    "employees": [
        {
            "id": "e1",
            "name": "Tester",
            "select": "S1",
            "from": "office",
            "metrics": [
                {
                    "id": "rev",
                    "name": "Revenue",
                    "value": 100,
                    "where": "online",
                    "period": "2024",
                }
            ],
        }
    ],
}

# Стандартный набор описаний полей
DEFAULT_DESCRIPTIONS = {
    "name": "Имя/название",
    "value": "Числовое значение метрики",
    "period": "Отчётный период",
    "position": "Должность сотрудника",
    "department": "Подразделение",
    "region": "Регион работы",
    "metric_name": "Название метрики",
}
