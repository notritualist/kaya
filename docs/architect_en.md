# Kaya Project Structure

kaya/
├── README.md                    # Project description (EN)
├── README_ru.md                 # Project description (RU)
├── pyproject.toml               # Python project: dependencies, version
├── .gitignore                   # Ignored files
├── .gitmodules                  # Imported modules 
│
├── db-srv/                      # Database service
│   ├── configs/
│   │   ├── docker-compose.yaml  # Docker Compose for PostgreSQL and Qdrant
│   │   ├── postgresql.conf      # PostgreSQL configuration
│   │   ├── pg_hba.conf          # PostgreSQL authentication rules
│   │   └── qdrant_config.yaml   # Qdrant configuration
│   └── scripts/
│       └── start-db.sh          # Script to start all databases
│
├── main-srv/                    # Main server
│   ├── .venv/                   # Python virtual environment
│   ├── configs/
│   │   └── postgres_config.yaml # PostgreSQL connection configuration
│   │
│   ├── llama.cpp/               # llama.cpp submodule (fork)
│   │   ├── CMakeLists.txt
│   │   ├── Makefile
│   │   ├── build/               # Built binaries (ignored by git)
│   │   └── ...                  # llama.cpp source files
│   │
│   ├── logs                     # Agent logs for main-srv
│   │   └── kaya_full.log        # Full log (DEBUG+)
│   │
│   ├── models/                  # LLM models (ignored by git)
│   │   └── qwen3_5/
│   │       └── Qwen3.5-9B-Q4_K_M.gguf
│   │
│   ├── scripts/
│   │   └── start_llama-server.sh # Start llama-server (API)
│   │
│   └── src/                     # Python source code
│       ├── __init__.py
│       ├── main.py              # Entry point (agent startup)
│       ├── version.py           # Global version from pyproject.toml
│       │
│       └── db_manager/          # Database management
│           ├── __init__.py
│           ├── db_manager.py    # PostgreSQL connection
│           └── migrations/
│               ├── __init__.py
│               ├── pg_migration_manager.py      # Database migration manager
│               └── V001_initial.sql             # Initial schema (main agent tables for PostgreSQL)
│
└── docs/                        # Documentation
└── ...