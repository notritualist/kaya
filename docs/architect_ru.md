# Структура проекта

agent/
├── README.md                    # Описание проекта (EN)
├── README_ru.md                 # Описание проекта (RU)
├── pyproject.toml               # Python проект: зависимости, версия
├── .gitignore                   # Игнорируемые файлы
├── .gitmodules                  # Импортированные модули
│
├── db-srv/                      # Сервис базы данных
│    ├── configs/
│    │   ├── docker-compose.yaml  # Docker Compose для PostgreSQL и Qdrant
│    │   ├── postgresql.conf      # Конфигурация PostgreSQL
│    │   ├── pg_hba.conf          # Правила аутентентификации PostgreSQL
│    │   └── qdrant_config.yaml   # Конфигурация Qdrant
│    └── scripts/
│        └── start-db.sh          # Скрипт запуска всех БД
│
├── main-srv/                    # Основной сервер
│    ├── .venv/                  # Виртуальное окружение Python
│    ├── configs/
│    │   ├── postgres_config.yaml # Конфигурация подключения к PostgreSQL
│    │   ├── qdrant_config.yaml   # Конфигурация подключения к Qdrant
│    │   └── model_routing.yaml   # Конфигурация роутинга LLM-провайдеров
│    │
│    ├── llama.cpp/              # Субмодуль llama.cpp (форк)
│    │   ├── CMakeLists.txt
│    │   ├── Makefile
│    │   ├── build/               # Собранные бинарники (игнорируется git)
│    │   └── ...                  # Исходники llama.cpp
│    │
│    ├── logs/                   # Логи работы агента
│    │   └── kaya_full.log        # Полный лог (DEBUG+)
│    │
│    ├── models/                 # LLM модели (игнорируется git)
│    │   └── qwen3_5/
│    │       └── Qwen3.5-9B-Q4_K_M.gguf
│    │
│    ├── requirements.txt        # Файл зависимостей .venv (main-srv)
│    │
│    ├── scripts/
│    │   └── start_llama-server.sh # Запуск llama-server (API)
│    │
│    └── src/                     # Исходный код Python
│        ├── __init__.py
│        ├── main.py              # Точка входа (запуск агента)
│        ├── version.py           # Глобальная версия из pyproject.toml
│        │
│        ├── db_manager/          # Управление БД
│        │   ├── __init__.py
│        │   ├── db_manager.py    # Подключение к PostgreSQL
│        │   └── migrations/
│        │       ├── __init__.py
│        │       ├── pg_migration_manager.py         # Менеджер применения миграций БД
│        │       ├── V001_initial.sql                # Начальная схема (основные таблицы агента)
│        │       ├── V002_dialogues.sql              # Схема слоя диалогов
│        │       └── V003_pseudohormonal_system.sql  # Схема PHS: baseline, momentary, lifecycle, self_knowledge
│        │
│        ├── dialog_services/     # Управление жизненным циклом диалогов
│        │   ├── __init__.py
│        │   └── dialogue_manager.py  # Менеджер диалогов (создание/закрытие, таймауты)
│        │
│        ├── interfaces/          # Интерфейсы
│        │   ├── __init__.py
│        │   └── console_interface.py  # Консольный UI
│        │
│        ├── model_service/       # Абстракция доступа к LLM с роутингом
│        │   ├── __init__.py
│        │   ├── model_service.py        # Роутер: выбор провайдера по model_name
│        │   ├── config/
│        │   │   └── model_routing.yaml  # Правила роутинга и конфиги провайдеров
│        │   └── providers/              # Реализации провайдеров LLM
│        │       ├── __init__.py
│        │       ├── base.py                 # Абстрактный интерфейс LLMProvider
│        │       ├── local_llama.py          # Провайдер для локального llama-server
│        │       └── external_dashscope.py   # Провайдер для DashScope API (заглушка)
│        │
│        ├── orchestrator/        # Ядро оркестрации задач
│        │   ├── __init__.py
│        │   ├── orchestrator_entry.py   # Точка входа: создание задач из внешних событий
│        │   ├── orchestrator.py         # Фоновый цикл: выбор и диспетчеризация задач
│        │   └── response_composer.py    # Генерация финального ответа через ModelService
│        │
│        ├── phs_service/         # Псевдогормональная система (PHS)
│        │   ├── __init__.py
│        │   ├── baseline_manager.py     # Управление baseline: инициализация, OU-дрейф, эффекты выключений
│        │   ├── vector_encoder.py       # RFF-кодирование гормонального профиля в вектор 128d
│        │   ├── valence_calculator.py   # Расчёт валентности по формуле с динамической чувствительностью
│        │   ├── lifecycle_manager.py    # Управление жизненным циклом агента (off/sleep/active), crash recovery
│        │   └── phs_scheduler.py        # Планировщик фоновых задач PHS (ежечасный дрейф)
│        │
│        ├── session_services/    # Управление сессиями
│        │   ├── __init__.py
│        │   └── session_manager.py      # Менеджер сессий и привязки actor_id
│        │
│        └── services/            # Вспомогательные сервисы
│            ├── __init__.py
│            ├── service_metrics.py      # Обновление статусов задач/шагов, метрики
│            └── tokens_counter.py       # Подсчёт токенов для моделей Qwen
│
└── docs/                        # Документация
    └── ...
