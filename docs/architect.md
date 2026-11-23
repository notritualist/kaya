# Project Structure

agent/
├── pyproject.toml               # Python project: dependencies, version
├── .gitignore                   # Ignored files
│
├── db-srv/                      # Database service
│    ├── configs/
│    │   ├── docker-compose.yaml  # Docker Compose for PostgreSQL and Qdrant
│    │   ├── postgresql.conf      # PostgreSQL configuration
│    │   ├── pg_hba.conf          # PostgreSQL authentication rules
│    │   └── qdrant_config.yaml   # Qdrant configuration
│    └── scripts/
│        ├── start-db.sh          # Script to start all databases
│        └── create_qdrant_collection.py # Script to create collection in agent's qdrant memory optimized for 2560d vectors
├── main-srv/                    # Main server
│    ├── .venv/                  # Python virtual environment
│    │
│    ├── llama.cpp/               # llama.cpp submodule (fork)
│    │   ├── build/               # Built binaries (ignored by git)
│    │   └── ...                  # llama.cpp sources
│    │
│    ├── logs/                   # Agent operation logs
│    │   └── kaya_full.log        # Full log (DEBUG+)
│    │
│    ├── models/                 # LLM models (ignored by git)
│    │   ├── qwen3_5/
│    │   │   └── Qwen3.5-9B-Q4_K_M.gguf # Local LLM model
│    │   │ 
│    │   └── qwen3_5-tokenizer   
│    │       └── tokenizer.json  # Tokenizer for qwen3_5 models
│    │
│    ├── requirements.txt        # .venv dependencies file (main-srv)
│    │
│    ├── scripts/
│    │   └── start_llama-server.sh # Script for launching a local llama-server (API)
│
└── docs/                         # Documentation
     ├── architect.md             # Architecture documentation (English)
     ├── architect_ru.md          # Architecture documentation (Russian)
     └── ...