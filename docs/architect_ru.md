# Структура проекта

agent/
├── pyproject.toml               # Python проект: зависимости, версия
├── .gitignore                   # Игнорируемые файлы
│
├── db-srv/                      # Сервис базы данных
│    ├── configs/
│    │   ├── docker-compose.yaml  # Docker Compose для PostgreSQL и Qdrant
│    │   ├── postgresql.conf      # Конфигурация PostgreSQL
│    │   ├── pg_hba.conf          # Правила аутентентификации PostgreSQL
│    │   └── qdrant_config.yaml   # Конфигурация Qdrant
│    └── scripts/
│        ├── start-db.sh          # Скрипт запуска всех БД
│        └── create_qdrant_collection.py # Скрипт создания коллекции в qdrant памяти агента с оптимизацией под 2560d векторы
├── main-srv/                    # Основной сервер
│    ├── .venv/                  # Виртуальное окружение Python
│    │
│    ├── configs/                # Конфигурации агента
│    │   ├── postgres_config.yaml  # Настройки подключения к PostgreSQL
│    │   └── qdrant_config.yaml    # Настройки подключения к Qdrant
│    │
│    ├── llama.cpp/               # Субмодуль llama.cpp (форк)
│    │   ├── build/               # Собранные бинарники (игнорируется git)
│    │   └── ...                  # Исходники llama.cpp
│    │
│    ├── logs/                    # Логи работы агента
│    │   └── agent_full.log      # Полный лог (DEBUG+)
│    │
│    ├── models/                  # LLM модели (игнорируется git)
│    │   ├── qwen3_5/
│    │   │   └── Qwen3.5-9B-Q4_K_M.gguf # Локальная LLM модель
│    │   │ 
│    │   └── qwen3_5-tokenizer   
│    │       └── tokenizer.json  # Токенизатор моделей qwen3_5
│    │
│    ├── requirements.txt        # Файл зависимостей .venv (main-srv)
│    │
│    ├── scripts/
│    │   └── start_llama-server.sh # Скрипт запуска локального llama-server (API)
│    │
│    └── src/                     # Исходный код Python
│        ├── __init__.py
│        ├── main.py              # Точка входа (запуск агента)
│        ├── version.py           # Глобальная версия из pyproject.toml
│        │
│        └── db_manager/          # Управление БД
│            ├── __init__.py
│            ├── db_manager.py    # Подключение к PostgreSQL и Qdrant
│            └── migrations/
│                ├── __init__.py
│                ├── pg_migration_manager.py  # Менеджер применения миграций БД
│                └── V001_initial.sql         # Начальная схема (основные таблицы агента)
│
└── docs/                         # Документация
     ├── architect.md             # Архитектура проекта (English)
     ├── architect_ru.md          # Архитектура проекта (Russian)
     └── ...
