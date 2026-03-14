# Kaya Project Structure

kaya/
├── README.md                    # Project documentation (EN)
├── README_ru.md                 # Project documentation (RU)
├── pyproject.toml               # Python project: dependencies, version
├── .gitignore                   # Ignored files
│
├── db-srv/                      # 🗄️ Database service
│   ├── configs/
│   │   ├── docker-compose.yaml  # Docker Compose for PostgreSQL and Qdrant
│   │   ├── postgresql.conf      # PostgreSQL configuration
│   │   ├── pg_hba.conf          # Authentication rules
│   │   └── qdrant_config.yaml   # Qdrant configuration
│   └── scripts/
│       ├── start-db.sh          # DB startup script
│       └── create_qdrant_collection.py # Create kaya_db collection
│
├── main-srv/                    # 🖥️ Main server
│   ├── .venv/                   # Python virtual environment
│   ├── configs/
│   │   ├── model_config.yaml    # Model configuration (n_ctx, default temperature)
│   │   ├── postgres_config.yaml # DB connection configuration
│   │   └── qdrant_config.yaml   # Qdrant connection configuration
│   │
│   ├── llama.cpp/               # ⚙️ llama.cpp submodule (fork)
│   │   ├── CMakeLists.txt
│   │   ├── Makefile
│   │   ├── build/               # Built binaries (git-ignored)
│   │   └── ...                  # llama.cpp sources
│   │
│   ├── models/                  # 📦 LLM models (git-ignored)
│   │   ├── qwen3_8b/
│   │   │   └── Qwen3-8B-Q4_K_M.gguf  # Quantized Qwen3 model
│   │   └── qwen3-8b-tokenizer/
│   │       └── tokenizer.json   # Tokenizer
│   │
│   ├── scripts/
│   │   └── model_orchestrator.sh # Launch llama-server (API)
│   │
│   └── src/                     # 🐍 Python source code
│       ├── __init__.py
│       ├── main.py              # 🚀 Entry point (API launch + Orchestrator)
│       ├── version.py           # Version from pyproject.toml
│       │
│       ├── db_manager/          # 💾 Database management
│       │   ├── __init__.py
│       │   ├── db_manager.py    # PostgreSQL connection
│       │   └── migrations/
│       │       ├── __init__.py
│       │       ├── migration_manager.py         # Migration application manager
│       │       ├── V001_initial.sql             # Initial schema (orchestrator, users)
│       │       ├── V002_room_switching.sql      # Rooms, sessions, switching
│       │       └── V003_messages_processing.sql # COMBINED: normalization + reclassification + history
│       │ 
│       ├── interfaces/          # 🖥️ Interfaces
│       │   ├── __init__.py
│       │   └── console_interface.py  # Console UI (client)
│       │
│       ├── model_service/       # 🤖 Model service
│       │   ├── __init__.py
│       │   └── model_service.py # Interaction with llama-server (generate, chat)
│       │
│       ├── orchestrator/        # 🧠 Orchestrator (system core)
│       │   ├── __init__.py
│       │   ├── orchestrator.py  # Main coordinator (task loop, priorities)
│       │   ├── orchestrator_entry.py  # Orchestrator entry point
│       │   ├── preprocessor.py  # Pre-parsing of user questions
│       │   ├── context_builder.py  # Dialogue context construction (history + RAG)
│       │   ├── response_composer.py  # Response generation + background tasks launch
│       │   │
│       │   └── tools/           # 🛠️ Background processing tools
│       │       ├── __init__.py
│       │       ├── messages_normalize.py    # 🔤 Text normalization (spelling, emoji)
│       │       └── reclassification_rooms.py # 🏠 Room reclassification (semantics)
│       │
│       ├── services/            # 🔧 Basic services
│       │   ├── __init__.py
│       │   ├── service_metrics.py  # Orchestrator, steps and LLM metrics collection
│       │   └── tokens_counter.py   # Token counting (Qwen3 compatible)
│       │
│       ├── session_services/    # 🔄 Session and room management
│       │   ├── __init__.py
│       │   ├── session_manager.py  # Session lifecycle manager
│       │   └── room_switch_manager.py  # Room switching logic
│       │
│       └── logs/                # 📝 Logs (created automatically)
│           └── kaya_full.log    # Full log (DEBUG+)
│ 
└── docs/                        # 📚 Documentation
    └── ...
