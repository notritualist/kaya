# Структура проекта

agent/
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
│   │   ├── postgres_config.yaml # Конфигурация подключения к БД PostgresSQL
│   │   ├── qdrant_config.yaml   # Конфигурация подключения к БД Qdrant
│   │   └── model_routing.yaml   # Конфигурация роутинга LLM-провайдеров
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
│   ├── requirements.txt        # Файл зависимостей .venv (main-srv)
│   │
│   ├── scripts/
│   │   └── start_llama-server.sh # Запуск llama-server (API)
│   │
│   └── src/                     # Исходный код Python
│       ├── __init__.py
│       ├── main.py              # Точка входа (запуск агента)
│       ├── version.py           # Глобальная версия из pyproject.toml
│       │
│       ├── db_manager/          # Управление БД
│       │   ├── __init__.py
│       │   ├── db_manager.py    # Подключение к PostgreSQL
│       │   └── migrations/
│       │       ├── __init__.py
│       │       ├── pg_migration_manager.py         # Менеджер применений миграций БД
│       │       ├── V001_initial.sql                # Начальная схема (основные таблицы агента для PostgreSQL)
│       │       ├── V002_dialogues.sql              # Схема слоя диалогов
│       │       └── V003_pseudohormonal_system.sql  # Схема ПГС: состояния, baseline, momentary, self_knowledge
│       │
│       ├── dialog_services/     # Управление жизненным циклом диалогов
│       │   ├── __init__.py
│       │   └── dialogue_manager.py  # Менеджер управления диалогами
│       │
│       ├── interfaces/          # Интерфейсы
│       │   ├── __init__.py
│       │   └── console_interface.py  # Консольный UI
│       │
│       ├── model_service/       # Абстракция доступа к LLM с роутингом
│       │   ├── __init__.py
│       │   ├── model_service.py        # Роутер: выбор провайдера по model_name
│       │   ├── config/
│       │   │   └── model_routing.yaml  # Правила роутинга и конфиги провайдеров
│       │   └── providers/              # Реализации провайдеров сервисов LLM
│       │       ├── __init__.py
│       │       ├── base.py                 # Абстрактный интерфейс LLMProvider
│       │       ├── local_llama.py          # Провайдер для локального llama-server
│       │       └── external_dashscope.py   # Провайдер для DashScope API (заглушка)
│       │
│       ├── orchestrator/        # Ядро оркестрации задач
│       │   ├── __init__.py
│       │   ├── orchestrator_entry.py   # Точка входа: создание задач из внешних событий
│       │   ├── orchestrator.py         # Фоновый цикл: выбор и диспетчеризация задач
│       │   └── response_composer.py    # Генерация финального ответа через ModelService
│       │
│       ├── pgs_service/                  # Псевдогормональная система
│       │    ├── __init__.py
│       │    └── lifecycle_manager.py      # Управление жизненным циклом агента (off/sleep/active) 
│       │ 
│       ├── session_services/    # Управление сессиями
│       │    ├── __init__.py
│       │    └── session_manager.py    # Менеджер жизненного цикла сессий и диалогов
│       │
│       └── services/            # Вспомогательные сервисные функции
│           ├── __init__.py
│           ├── service_metrics.py    # Обновление статусов задач/шагов, сохранение метрик
│           └── tokens_counter.py     # Подсчёт токенов для моделей Qwen
│
└── docs/                             # Документация
    └── ...
