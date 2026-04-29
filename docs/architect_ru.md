# Структура проекта Kaya

Структура проекта Kaya
kaya/
├── README.md                    # Описание проекта (EN)
├── README_ru.md                 # Описание проекта (RU)
├── pyproject.toml               # Python проект: зависимости, версия
├── .gitignore                   # Игнорируемые файлы
├── .gitmodules                  # Импортированные модули 
│
├── db-srv/                      # Сервис базы данных
│   ├── configs/
│   │   ├── docker-compose.yaml  # Docker Compose для PostgreSQL и Qdrant
│   │   ├── postgresql.conf      # Конфигурация PostgreSQL
│   │   ├── pg_hba.conf          # Правила аутентификации PostgresSQL
│   │   └── qdrant_config.yaml   # Конфигурация Qdrant
│   └── scripts/
│       └── start-db.sh          # Скрипт запуска всех БД
│
├── main-srv/                    # Основной сервер
│   ├── .venv/                   # Виртуальное окружение Python
│   ├── configs/
│   │   └── postgres_config.yaml # Конфигурация подключения к БД PostgresSQL
│   │
│   ├── llama.cpp/               # Субмодуль llama.cpp (форк)
│   │   ├── CMakeLists.txt
│   │   ├── Makefile
│   │   ├── build/               # Собранные бинарники (игнорируется git)
│   │   └── ...                  # Исходники llama.cpp
│   │
│   ├── logs                     # Логи работы агента для main-srv
│   │   └── kaya_full.log        # Полный лог (DEBUG+)
│   │
│   ├── models/                  # LLM модели (игнорируется git)
│   │   └── qwen3_5/
│   │       └── Qwen3.5-9B-Q4_K_M.gguf
│   │
│   ├── scripts/
│   │   └── start_llama-server.sh # Запуск llama-server (API)
│   │
│   └── src/                     # Исходный код Python
│       ├── __init__.py
│       ├── main.py              # Точка входа (запуск агента)
│       ├── version.py           # Глобальная версия из pyproject.toml
│       │
│       └── db_manager/          # Управление БД
│           ├── __init__.py
│           ├── db_manager.py    # Подключение к PostgreSQL
│           └── migrations/
│               ├── __init__.py
│               ├── pg_migration_manager.py      # Менеджер применений миграций БД
│               └── V001_initial.sql             # Начальная схема (основные таблицы агента для PostgreSQL)
│
└── docs/                        # Документация
└── ...
