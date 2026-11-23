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
│    ├── llama.cpp/               # Субмодуль llama.cpp (форк)
│    │   ├── build/               # Собранные бинарники (игнорируется git)
│    │   └── ...                  # Исходники llama.cpp
│    │
│    ├── logs/                    # Логи работы агента
│    │   └── kaya_full.log        # Полный лог (DEBUG+)
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
│
│
└── docs/                         # Документация
     ├── architect.md             # Архитектура проекта (English)
     ├── architect_ru.md          # Архитектура проекта (Russian)
     └── ...
