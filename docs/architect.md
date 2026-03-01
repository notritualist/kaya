# Kaya Project Structure

kaya/
├── README.md # Project documentation (EN)
├── README_ru.md # Project documentation (RU)
├── pyproject.toml # Python project: dependencies, version
├── .gitignore # Ignored files
│
├── db-srv/ # 🗄️ Database service
│ ├── configs/
│ │ ├── docker-compose.yaml # Docker Compose for PostgreSQL
│ │ ├── postgresql.conf # PostgreSQL configuration
│ │ └── pg_hba.conf # Authentication rules
│ └── scripts/
│ └── start-db.sh # Database startup script
│
├── main-srv/ # 🖥️ Main server
│ ├── .venv/ # Python virtual environment
│ ├── configs/
│ │ ├── model_config.yaml # Model configuration
│ │ └── postgres_config.yaml # Database connection config
│ │
│ ├── llama.cpp/ # ⚙️ llama.cpp submodule (fork)
│ │ ├── CMakeLists.txt
│ │ ├── Makefile
│ │ ├── README.md
│ │ ├── build/ # Compiled binaries (ignored)
│ │ ├── examples/ # Usage examples
│ │ ├── ggml/ # Tensor computation library
│ │ ├── src/ # llama.cpp sources
│ │ └── tests/ # Tests
│ │
│ ├── models/ # 📦 LLM models (git-ignored)
│ │ ├── qwen3_8b/
│ │ │ └── Qwen3-8B-Q4_K_M.gguf # Quantized Qwen3 model
│ │ └── qwen3-8b-tokenizer/
│ │ └── tokenizer.json # Tokenizer
│ │
│ ├── scripts/
│ │ └── model_orchestrator.sh # llama-server launcher
│ │
│ └── src/ # 🐍 Python source code
│ ├── init.py
│ ├── main.py # 🚀 Entry point
│ ├── version.py # Version from pyproject.toml
│ │
│ ├── db_manager/ # 💾 Database management
│ │ ├── init.py
│ │ ├── db_manager.py # PostgreSQL connection
│ │ └── migrations/
│ │ ├── init.py
│ │ ├── migration_manager.py # Migrations
│ │ └── V001_initial.sql # Initial schema
│ │
│ ├── interfaces/ # 🖥️ User interfaces
│ │ ├── init.py
│ │ └── console_interface.py # Console UI
│ │
│ ├── model_service/ # 🤖 Model service
│ │ ├── init.py
│ │ └── model_service.py # Interaction with llama-server
│ │
│ ├── orchestrator/ # 🧠 Orchestrator (core)
│ │ ├── init.py
│ │ ├── orchestrator.py # Main coordinator
│ │ ├── orchestrator_entry.py # Orchestrator entry point
│ │ ├── context_builder.py # Context construction
│ │ └── response_composer.py # Response composition
│ │
│ ├── services/ # 🔧 Base services
│ │ ├── init.py
│ │ ├── service_metrics.py # Metrics collection
│ │ └── tokens_counter.py # Token counting
│ │
│ ├── session_services/ # 🔄 Session management
│ │ ├── init.py
│ │ └── session_manager.py # Session manager
│ │
│ └── logs/ # 📝 Logs (auto-generated)
│ └── kaya_full.log # Full log (DEBUG+)
│
└── docs/ # 📚 Documentation
└── ...
