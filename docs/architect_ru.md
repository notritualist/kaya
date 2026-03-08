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
│       ├── start-db.sh          # Скрипт запуска БД
│       └── create_qdrant_collection.py # Создание коллекции kaya_db
│
├── main-srv/                    # 🖥️ Основной сервер
│   ├── .venv/                   # Виртуальное окружение Python
│   ├── configs/
│   │   ├── model_config.yaml    # Конфигурация модели
│   │   ├── postgres_config.yaml # Конфигурация подключения к БД
│   │   └── qdrant_config.yaml   # Конфигурация подключения к Qdrant
│   │
│   ├── llama.cpp/               # ⚙️ Субмодуль llama.cpp (форк)
│   │   ├── CMakeLists.txt
│   │   ├── Makefile
│   │   ├── README.md
│   │   ├── build/               # Собранные бинарники (игнорируется)
│   │   ├── examples/            # Примеры использования
│   │   ├── ggml/                # Библиотека тензорных вычислений
│   │   ├── src/                 # Исходники llama.cpp
│   │   └── tests/               # Тесты
│   │
│   ├── models/                  # 📦 LLM модели (игнорируется git)
│   │   ├── qwen3_8b/
│   │   │   └── Qwen3-8B-Q4_K_M.gguf  # Квантованная модель Qwen3
│   │   └── qwen3-8b-tokenizer/
│   │       └── tokenizer.json   # Токенизатор
│   │
│   ├── scripts/
│   │   └── model_orchestrator.sh # Запуск llama-server
│   │
│   └── src/                     # 🐍 Исходный код Python
│       ├── init.py
│       ├── main.py              # 🚀 Точка входа
│       ├── version.py           # Версия из pyproject.toml
│       │
│       ├── db_manager/          # 💾 Управление БД
│       │   ├── init.py
│       │   ├── db_manager.py    # Подключение к PostgreSQL
│       │   └── migrations/
│       │       ├── init.py
│       │       ├── migration_manager.py  # Менеджер миграций
│       │       ├── V001_initial.sql      # Начальная схема
│       │       └── V002_room_switching.sql  # Комнаты и переключения
│       │
│       ├── interfaces/          # 🖥️ Интерфейсы
│       │   ├── init.py
│       │   └── console_interface.py  # Консольный UI
│       │
│       ├── model_service/       # 🤖 Сервис модели
│       │   ├── init.py
│       │   └── model_service.py # Взаимодействие с llama-server
│       │
│       ├── orchestrator/        # 🧠 Оркестратор (ядро)
│       │   ├── init.py
│       │   ├── orchestrator.py  # Главный координатор
│       │   ├── orchestrator_entry.py  # Точка входа оркестратора
│       │   ├── preprocessor.py  # Предразбор сообщений (классификация комнат)
│       │   ├── context_builder.py  # Построение контекста диалога
│       │   └── response_composer.py  # Компоновка ответа
│       │
│       ├── services/            # 🔧 Базовые сервисы
│       │   ├── init.py
│       │   ├── service_metrics.py  # Сбор метрик оркестратора и LLM
│       │   └── tokens_counter.py   # Подсчёт токенов
│       │
│       ├── session_services/    # 🔄 Управление сессиями и комнатами
│       │   ├── init.py
│       │   ├── session_manager.py  # Менеджер сессий
│       │   └── room_switch_manager.py  # Переключение комнат
│       │
│       └── logs/                # 📝 Логи (создаётся автоматически)
│           └── kaya_full.log    # Полный лог (DEBUG+)
│
└── docs/                        # 📚 Документация
    └── ...
