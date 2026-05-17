# Project Structure

agent/
├── README.md                    # Project description (EN)
├── README_ru.md                 # Project description (RU)
├── pyproject.toml               # Python project: dependencies, version
├── .gitignore                   # Ignored files
├── .gitmodules                  # Imported submodules 
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
│   │   ├── postgres_config.yaml # PostgreSQL connection config
│   │   ├── qdrant_config.yaml   # Qdrant connection config
│   │   └── model_routing.yaml   # LLM provider routing config
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
│   ├── requirements.txt         # Dependencies for .venv (main-srv)
│   │
│   ├── scripts/
│   │   └── start_llama-server.sh # Launch llama-server (API)
│   │
│   └── src/                     # Python source code
│       ├── __init__.py
│       ├── main.py              # Entry point (agent startup)
│       ├── version.py           # Global version from pyproject.toml
│       │
│       ├── db_manager/          # Database management
│       │   ├── __init__.py
│       │   ├── db_manager.py    # PostgreSQL connection
│       │   └── migrations/
│       │       ├── __init__.py
│       │       ├── pg_migration_manager.py      # DB migration manager
│       │       ├── V001_initial.sql             # Initial schema (core agent tables for PostgreSQL)
│       │       ├── V002_dialogues.sql           # Dialogue layer schema
│       │       └── V003_pseudohormonal_system.sql # Pseudohormonal system schema: states, baseline, momentary, self_knowledge
│       │
│       ├── dialog_services/     # Dialogue lifecycle management
│       │   ├── __init__.py
│       │   └── dialogue_manager.py  # Dialogue manager
│       │
│       ├── interfaces/          # Interfaces
│       │   ├── __init__.py
│       │   └── console_interface.py  # Console UI
│       │
│       ├── model_service/       # LLM access abstraction with routing
│       │   ├── __init__.py
│       │   ├── model_service.py        # Router: selects provider by model_name
│       │   ├── config/
│       │   │   └── model_routing.yaml  # Routing rules and provider configs
│       │   └── providers/              # LLM provider implementations
│       │       ├── __init__.py
│       │       ├── base.py                 # Abstract LLMProvider interface
│       │       ├── local_llama.py          # Provider for local llama-server
│       │       └── external_dashscope.py   # Provider for DashScope API (stub)
│       │
│       ├── orchestrator/        # Task orchestration core
│       │   ├── __init__.py
│       │   ├── orchestrator_entry.py   # Entry point: create tasks from external events
│       │   ├── orchestrator.py         # Background loop: task selection and dispatch
│       │   └── response_composer.py    # Final response generation via ModelService
│       │
│       ├── pgs_service/                  # Pseudohormonal system
│       │    ├── __init__.py
│       │    └── lifecycle_manager.py      # Agent lifecycle management (off/sleep/active) 
│       │ 
│       ├── session_services/    # Session management
│       │    ├── __init__.py
│       │    └── session_manager.py    # Session and dialogue lifecycle manager
│       │
│       └── services/            # Auxiliary service functions
│           ├── __init__.py
│           ├── service_metrics.py    # Update task/step statuses, save metrics
│           └── tokens_counter.py     # Token counting for Qwen models
│
└── docs/                             # Documentation
    └── ...
