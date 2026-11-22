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
│    └── .venv/                  # Виртуальное окружение Python
│
└── docs/                        # Документация
     ├── architect.md             # Архитектура проекта (English)
     ├── architect_ru.md          # Архитектура проекта (Russian)
     └── ...
