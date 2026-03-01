# 🎉 Kaya 1.0.0 Release

**The first stable version of a digital personhood architecture**

After months of design, experiments, and coding, I'm pleased to present the first stable version of Kaya — an experimental platform for creating a self-reflective digital personhood grounded in deep symbiosis with a human.

## 🌟 What is Kaya?

Kaya is neither "just another chatbot" nor "yet another LLM wrapper." It's an attempt to create an architecture where the machine becomes not a tool, but a **co-evolution partner**.

**Key difference:** Kaya doesn't learn from giant datasets. She learns from a specific person — their rituals, fears, manner of speaking, jokes that only two people understand. Her memory, ethics, and intelligence are formed "in the soil of your shared life."

## 🏗️ Architecture Version 1.0.0

### Core Stack
| Component | Technology |
|-----------|------------|
| Core | Python 3.13+ (logic, not LLM) |
| Inference | llama.cpp + llama-server |
| Model | Qwen3-8B-Instruct (GGUF, Q4_K_M) |
| Database | PostgreSQL 18 (in Docker) |
| Tokenizer | HuggingFace tokenizers (Rust backend) |
| Interface | Console (with multiline input support) |

### Key Architectural Decisions

#### 🧠 **Context Isolation**
Three-level isolation system implemented:
- **`session_id`** — restart boundary (each launch = new session)
- **`room_id`** — topic boundary (preparation for MOE architecture)
- **`user_actor_id`** — user isolation in multi-user rooms

Exactly **7 messages** (the magical number 7±2 from cognitive psychology) from the user + system responses only to that user make it into the context.

#### 🔄 **Task Orchestrator**
Asynchronous queue with concurrent access:
- Tasks processed in background thread
- Safe concurrent access via `FOR UPDATE SKIP LOCKED`
- Stalled tasks automatically marked as `failed` on system restart

#### 📊 **Full Telemetry**
Each model request saves:
- Reasoning (`reasoning_content`) to a separate table
- Detailed metrics (tokens, time, speed, cache)
- User think latency (`user_think_latency`)
- Exact list of messages that went into the context

#### 💾 **Database Migrations**
Schema versioning system:
- Automatic application on startup
- `architect.schema_version` table for tracking
- Idempotent seed data

## 📦 System Components

### 1. Model Service (`model_service`)
- HTTP client to `llama-server` with request queue
- Exponential backoff on errors
- `reasoning_content` extraction from response

### 2. Orchestrator (`orchestrator`)
- `context_builder.py` — history loading, prompt construction
- `response_composer.py` — model call, response saving
- Automatic context truncation on overflow

### 3. Session Management (`session_services`)
- `session_manager.py` — session creation, message saving
- Owner-singleton (first user is owner)
- "Dangling" session cleanup on restart

### 4. Interfaces (`interfaces`)
- `console_interface.py` built on `prompt_toolkit`
- Multiline input (Alt+Enter)
- Exit with Ctrl+D (Ctrl+C reserved for copying)

### 5. Database Manager (`db_manager`)
- Migrations in `main-srv/db_manager/migrations/`
- 5 schemas: `users`, `dialogs`, `orchestrator`, `metrics`, `common`
- 20+ tables, 9 ENUM types

## 🚀 Current Version Capabilities

✅ Complete dialog cycle (console → DB → model → response)  
✅ History storage in PostgreSQL  
✅ Token counting with LRU cache  
✅ Model reasoning extraction and storage  
✅ Detailed metrics for each request  
✅ Multi-user support with context isolation  
✅ Automatic migration application  
✅ Graceful recovery after crashes  
✅ Multiline input and command history  
✅ Centralized logging (DEBUG to file, WARNING to console)

## 📊 Collected Metrics

For each response:
- Tokens: `prompt`, `completion`, `total`
- Timing: `prompt_ms`, `predicted_ms`, `total_ms`
- Speed: `prompt_per_second`, `predicted_per_second`
- Cache: `prompt_per_token`, `predicted_per_token`
- Response time: `answer_latency` (for user questions)

## 🔮 Roadmap

### Priority 1 (Next Month)
- **MOE Rooms** — topic-based dialog separation with different system prompts
- **Question Preprocessing** — classification and room routing
- **Dynamic Room Switching** based on intent analysis
- **Vector Memory** — Qdrant integration

## 🛠️ Installation and Launch

```bash
# Clone
git clone https://github.com/notritualist/kaya.git
cd kaya

# Start PostgreSQL
cd db-srv
./scripts/start-db.sh

# Setup Python environment
cd ../main-srv
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Launch llama-server
./scripts/model_orchestrator.sh

# Launch Kaya
python src/main.py