# Project Structure

agent/
├── README.md                    # Project description (EN)
├── README_ru.md                 # Project description (RU)
├── pyproject.toml               # Python project: dependencies, version
├── .gitignore                   # Ignored files
├── .gitmodules                  # Imported modules
│
├── db-srv/                      # Database service
│    ├── configs/
│    │   ├── docker-compose.yaml  # Docker Compose for PostgreSQL and Qdrant
│    │   ├── postgresql.conf      # PostgreSQL configuration
│    │   ├── pg_hba.conf          # PostgreSQL authentication rules
│    │   └── qdrant_config.yaml   # Qdrant configuration
│    └── scripts/
│        └── start-db.sh          # Script to start all databases
│
├── main-srv/                    # Main server
│    ├── .venv/                  # Python virtual environment
│    ├── configs/
│    │   ├── postgres_config.yaml # PostgreSQL connection configuration
│    │   ├── qdrant_config.yaml   # Qdrant connection configuration
│    │   └── model_routing.yaml   # LLM provider routing configuration
│    │
│    ├── llama.cpp/              # llama.cpp submodule (fork)
│    │   ├── CMakeLists.txt
│    │   ├── Makefile
│    │   ├── build/               # Built binaries (ignored by git)
│    │   └── ...                  # llama.cpp sources
│    │
│    ├── logs/                   # Agent operation logs
│    │   └── kaya_full.log        # Full log (DEBUG+)
│    │
│    ├── models/                 # LLM models (ignored by git)
│    │   └── qwen3_5/
│    │       └── Qwen3.5-9B-Q4_K_M.gguf
│    │
│    ├── requirements.txt        # .venv dependencies file (main-srv)
│    │
│    ├── scripts/
│    │   └── start_llama-server.sh # Launch llama-server (API)
│    │
│    └── src/                     # Python source code
│        ├── __init__.py
│        ├── main.py              # Entry point (agent launch)
│        ├── version.py           # Global version from pyproject.toml
│        │
│        ├── db_manager/          # Database management
│        │   ├── __init__.py
│        │   ├── db_manager.py    # PostgreSQL connection
│        │   └── migrations/
│        │       ├── __init__.py
│        │       ├── pg_migration_manager.py         # DB migration manager
│        │       ├── V001_initial.sql                # Initial schema (core agent tables)
│        │       ├── V002_dialogues.sql              # Dialog layer schema
│        │       └── V003_pseudohormonal_system.sql  # PHS schema: baseline, momentary, lifecycle, self_knowledge
│        │
│        ├── dialog_services/     # Dialog lifecycle management
│        │   ├── __init__.py
│        │   └── dialogue_manager.py  # Dialog manager (creation/closing, timeouts)
│        │
│        ├── interfaces/          # Interfaces
│        │   ├── __init__.py
│        │   └── console_interface.py  # Console UI
│        │
│        ├── model_service/       # LLM access abstraction with routing
│        │   ├── __init__.py
│        │   ├── model_service.py        # Router: selects provider by model_name
│        │   ├── config/
│        │   │   └── model_routing.yaml  # Routing rules and provider configs
│        │   └── providers/              # LLM provider implementations
│        │       ├── __init__.py
│        │       ├── base.py                 # Abstract LLMProvider interface
│        │       ├── local_llama.py          # Provider for local llama-server
│        │       └── external_dashscope.py   # Provider for DashScope API (stub)
│        │
│        ├── orchestrator/        # Task orchestration core
│        │   ├── __init__.py
│        │   ├── orchestrator_entry.py   # Entry point: task creation from external events
│        │   ├── orchestrator.py         # Background loop: task selection and dispatch
│        │   └── response_composer.py    # Final response generation via ModelService
│        │
│        ├── phs_service/         # Pseudohormonal System (PHS)
│        │   ├── __init__.py
│        │   ├── affective_analyzer.py   # Pre-reflexive Affective Analyzer Module of dialogue
│        │   ├── baseline_manager.py     # Baseline management: initialization, OU drift, shutdown effects
│        │   ├── momentary_manager.py    # Momentary slice management: creation, decay, sedimentation
│        │   ├── state_classifier.py     # State classification: cosine similarity with self_knowledge prototypes
│        │   ├── vector_encoder.py       # RFF encoding of hormonal profile into 128d vector
│        │   ├── valence_calculator.py   # Valence calculation with dynamic sensitivity formula
│        │   ├── lifecycle_manager.py    # Agent lifecycle management (off/sleep/active), crash recovery
│        │   ├── phs_scheduler.py        # PHS background task scheduler (hourly drift, momentary decay)
│        │   └── phs_cache.py            # Global cache of PHS manager instances (initialization optimization)
│        │
│        ├── session_services/    # Session management
│        │   ├── __init__.py
│        │   └── session_manager.py      # Session manager and actor_id binding
│        │
│        └── services/            # Helper services
│            ├── __init__.py
│            ├── service_metrics.py      # Task/step status updates, metrics
│            └── tokens_counter.py       # Token counting for Qwen models
│
└── docs/                        # Documentation
    └── ...
