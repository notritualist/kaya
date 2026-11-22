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
│    └── .venv/                  # Python virtual environment
│
└── docs/                        # Documentation
     ├── architect.md             # Architecture documentation (English)
     ├── architect_ru.md          # Architecture documentation (Russian)
     └── ...