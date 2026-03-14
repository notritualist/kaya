# Структура проекта Kaya

kaya/
├── README.md                    # Проектная документация (EN)
├── README_ru.md                 # Проектная документация (RU)
├── pyproject.toml               # Python проект: зависимости, версия
├── .gitignore                   # Игнорируемые файлы
│
├── db-srv/                      # 🗄️ Сервис базы данных
│   ├── configs/
│   │   ├── docker-compose.yaml  # Docker Compose для PostgreSQL и Qdrant
│   │   ├── postgresql.conf      # Конфигурация PostgreSQL
│   │   ├── pg_hba.conf          # Правила аутентификации
│   │   └── qdrant_config.yaml   # Конфигурация Qdrant
│   └── scripts/
│       ├── start-db.sh                 # Скрипт запуска БД
│       └── create_qdrant_collection.py # Создание коллекции kaya_db
│
├── main-srv/                    # 🖥️ Основной сервер
│   ├── .venv/                   # Виртуальное окружение Python
│   ├── configs/
│   │   ├── model_config.yaml    # Конфигурация модели (n_ctx, температура по умолчанию)
│   │   ├── postgres_config.yaml # Конфигурация подключения к БД
│   │   └── qdrant_config.yaml   # Конфигурация подключения к Qdrant
│   │
│   ├── llama.cpp/               # ⚙️ Субмодуль llama.cpp (форк)
│   │   ├── CMakeLists.txt
│   │   ├── Makefile
│   │   ├── build/               # Собранные бинарники (игнорируется git)
│   │   └── ...                  # Исходники llama.cpp
│   │
│   ├── models/                  # 📦 LLM модели (игнорируется git)
│   │   ├── qwen3_8b/
│   │   │   └── Qwen3-8B-Q4_K_M.gguf  # Квантованная модель Qwen3
│   │   └── qwen3-8b-tokenizer/
│   │       └── tokenizer.json   # Токенизатор
│   │
│   ├── scripts/
│   │   └── model_orchestrator.sh # Запуск llama-server (API)
│   │
│   └── src/                     # 🐍 Исходный код Python
│       ├── __init__.py
│       ├── main.py              # 🚀 Точка входа (запуск API + Оркестратор)
│       ├── version.py           # Версия из pyproject.toml
│       │
│       ├── db_manager/          # 💾 Управление БД
│       │   ├── __init__.py
│       │   ├── db_manager.py    # Подключение к PostgreSQL
│       │   └── migrations/
│       │       ├── __init__.py
│       │       ├── migration_manager.py  # Менеджер применений миграций
│       │       ├── V001_initial.sql            # Начальная схема (оркестратор, пользователи)
│       │       ├── V002_room_switching.sql     # Комнаты, сессии, переключения
│       │       └── V003_messages_processing.sql # ОБЪЕДИНЁННАЯ: нормализация + реклассификация + история
│       │
│       ├── interfaces/          # 🖥️ Интерфейсы
│       │   ├── __init__.py
│       │   └── console_interface.py  # Консольный UI (клиент)
│       │
│       ├── model_service/       # 🤖 Сервис модели
│       │   ├── __init__.py
│       │   └── model_service.py # Взаимодействие с llama-server (generate, chat)
│       │
│       ├── orchestrator/        # 🧠 Оркестратор (ядро системы)
│       │   ├── __init__.py
│       │   ├── orchestrator.py  # Главный координатор (цикл задач, приоритеты)
│       │   ├── orchestrator_entry.py  # Точка входа оркестратора
│       │   ├── preprocessor.py  # Предразбор вопросов пользователя
│       │   ├── context_builder.py  # Построение контекста диалога (история + RAG)
│       │   ├── response_composer.py  # Генерация ответа + запуск фоновых задач
│       │   │
│       │   └── tools/           # 🛠️ Фоновые инструменты обработки
│       │       ├── __init__.py
│       │       ├── messages_normalize.py    # 🔤 Нормализация текста (орфография, эмодзи)
│       │       └── reclassification_rooms.py # 🏠 Реклассификация комнат (семантика)
│       │
│       ├── services/            # 🔧 Базовые сервисы
│       │   ├── __init__.py
│       │   ├── service_metrics.py  # Сбор метрик оркестратора, шагов и LLM
│       │   └── tokens_counter.py   # Подсчёт токенов (Qwen3 compatible)
│       │
│       ├── session_services/    # 🔄 Управление сессиями и комнатами
│       │   ├── __init__.py
│       │   ├── session_manager.py  # Менеджер жизненного цикла сессий
│       │   └── room_switch_manager.py  # Логика переключения комнат
│       │
│       └── logs/                # 📝 Логи (создаётся автоматически)
│           └── kaya_full.log    # Полный лог (DEBUG+)
│ 
└── docs/                        # 📚 Документация
    └── ...
