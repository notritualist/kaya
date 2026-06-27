**Kaya AI Agent: Development Status & Quick Start Guide**  

> **Status:** Active development (pre‑v1.1) | **License:** AGPL‑3.0  
> **Last updated:** 12 May 2026 (based on project log)

---

## 1. Overview  

**Kaya** is a modular, locally‑running AI agent designed for **conversational interaction**, **task orchestration** and **full observability**. It uses a large language model (currently Qwen3.5‑9B served via [`llama.cpp`](https://github.com/ggerganov/llama.cpp)) and stores all state—dialogs, reasoning chains, prompts and performance metrics—in a relational + vector dual‑database backend.

The project’s philosophy:
- **Privacy‑first**: everything runs on‑premise; no data leaves the host.
- **Full audit trail**: every message, reasoning step, LLM call and metric is persisted in PostgreSQL and Qdrant.
- **Modular & extensible**: the codebase is organised into independent services (sessions, dialogs, orchestrator, model abstraction), making it easy to swap LLM backends or add new interfaces (Telegram, REST).

---

## 2. High‑Level Architecture  

```
┌────────────────────────────────────────────────────────┐
│  Interfaces: console_interface (CLI)                    │
│  Orchestrator: orchestrator_entry, orchestrator_loop,   │
│                response_composer                        │
│  Model Service: model_service (router),                 │
│                 local_llama provider                    │
│  Data: PostgreSQL (relational history, tasks, metrics)  │
│         Qdrant (vector semantic memory – future)        │
└────────────────────────────────────────────────────────┘
```

All components are **stateless** – they operate directly on the databases. The orchestrator runs as a background thread, dispatching tasks created by the interfaces.

*For a detailed file‑by‑file breakdown, see `main-srv/docs/architecture.md` (English) or `architecture_ru.md` (Russian).*

---

## 3. Prerequisites  

- **Linux** (Debian‑based recommended; tested on Debian Trixie)
- **Python 3.13** (project uses `pyproject.toml`)
- **Docker Engine** (v27+; installed from official repo, not distro package)
- **llama.cpp** built with your hardware acceleration of choice (CUDA/Metal/CPU)
- **Qwen3.5‑9B** GGUF model placed in `main-srv/models/`

---

## 4. Quick Start  

### 4.1. Clone the Repository  

```bash
git clone --recurse-submodules https://github.com/your-org/kaya.git
cd kaya
```

The `--recurse-submodules` ensures `llama.cpp` is cloned into the project.

### 4.2. Set Up Python Environment  

```bash
cd main-srv
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e .          # installs all dependencies from pyproject.toml
```

### 4.3. Start Databases (PostgreSQL + Qdrant)  

From the project root:

```bash
cd db-srv
./scripts/start-db.sh
```

This script:
- Starts a PostgreSQL 18.1 container with optimized settings (bind‑mount to `/mnt/data_bases/pg_data/`)
- Starts a Qdrant vector database
- Waits until both are healthy

### 4.4. Launch the LLM Server  

```bash
cd main-srv
./scripts/start-llama-server.sh
```

The script runs `llama-server` on port 8080 with the Qwen3.5‑9B model and `enable_thinking: false` chat template.

### 4.5. Run the Agent  

```bash
cd main-srv/src
python main.py
```

You will see the console prompt. Type your message, press `Enter` to send, `Alt+Enter` for multi‑line input. Use `Ctrl+N` to manually start a new dialogue, and `exit`/`Ctrl+D` to quit. All activity is logged to `logs/kaya_full.log`.

---

## 5. Core Components & Data Flow  

### 5.1. User Input Pipeline  

1. **Console Interface** captures raw input.
2. **Session Manager** ensures an active physical session (linked to an `actor`) and an active **dialogue** (logical conversation context).  
   - Dialogues are lazily created on the first message.  
   - Inactivity timeout (15 min) automatically starts a new dialogue.
3. The message is saved into `dialogs.row_messages` with the appropriate `dialogue_id`.
4. **Orchestrator Entry** is called, creating a new **task** (`user_answer_generation`) in `orchestrator.orchestrator_tasks`.

### 5.2. Background Orchestrator  

- A singleton thread continuously polls for pending tasks with `SELECT ... FOR UPDATE SKIP LOCKED`.
- It dispatches to **Response Composer** for the `user_answer_generation` task type.

### 5.3. Response Generation  

1. **Response Composer** loads the system prompt (`agent_core_identity` from `orchestrator.prompts`) and recent conversation history (last 10 messages of the same dialogue).
2. It calls **Model Service**, which routes to the correct provider based on `configs/model_routing.yaml`. Currently only `local_llama` is active.
3. **Local Llama Provider** sends a chat completion request to the running `llama-server`.
4. The response is parsed into:
   - `response` (main answer text)  
   - `reasoning_content` (model’s chain‑of‑thought, if any)
5. **Service Metrics** records LLM usage data into `metrics.llm_internal` and links it to the new **orchestrator step**.
6. Reasoning content is stored in `orchestrator.reasonings`.
7. The final answer is saved as a new row in `dialogs.row_messages` with the same `dialogue_id`.
8. The task and step are marked as completed.

### 5.4. Session & Dialogue Lifecycle  

- When the user exits (Ctrl+D, `exit`, critical error), the current dialogue is closed with an appropriate reason (`user_exit`, `user_command`, `loop_error`, …) and then the session is closed.
- On startup, any dangling sessions are closed with `system_restart` reason.

---

## 6. Database Schema (overview)  

The PostgreSQL database `kaya_db` is organised into four main schemas:

| Schema        | Purpose                                             |
|---------------|-----------------------------------------------------|
| `users`       | Actors, external IDs (system, owner, users)         |
| `dialogs`     | Sessions, dialogues, messages                       |
| `orchestrator`| Tasks, steps, reasoning chains, prompts             |
| `metrics`     | LLM and embedding performance metrics               |

Key ENUM types (in `public`):  
`actor_type`, `session_status`, `session_close_reason`, `dialog_close_reason`, `task_status`, `step_type`, `prompt_status`, `reasoning_content_type`, etc.

All tables are created by the migration files in `/main-srv/src/db_manager/migrations/` and applied automatically on first run.

**Vector Database** (Qdrant):  
A collection `kaya_db` is created for 2560‑dimensional vectors (COSINE distance, Scalar Quantization INT8, HNSW index). It is currently reserved for future semantic memory features.

---

## 7. Configuration  

| File | Purpose |
|------|---------|
| `configs/postgres_config.yaml` | PostgreSQL connection parameters |
| `configs/qdrant_config.yaml` | Qdrant connection parameters |
| `configs/model_routing.yaml` | LLM provider routing rules and model specs |

Example `model_routing.yaml`:
```yaml
providers:
  local_llama:
    models:
      "Qwen3.5-9B-Q4_K_M.gguf":
        n_ctx: 262144
        supports_reasoning: true
```

The router matches `model_name` (or glob `*.gguf`) to a provider. Additional providers (e.g., `external_dashscope`) can be added by implementing the `LLMProvider` interface.

---

## 8. Current Limitations & Next Steps  

- **Hardcoded variables** in prompt placeholders – these are waiting for dedicated memory/knowledge modules.
- **Only console interface** is implemented; REST and Telegram are planned.
- **Qdrant** is up and running but not yet integrated into the reasoning pipeline.
- No **multi‑user isolation** beyond separate sessions – actors are currently predefined (`system`, `owner`, `user`).
- **Token counting** is a simple heuristic (4 chars ≈ 1 token) and will be replaced with a proper tokeniser.

Planned improvements:
- Self‑reflection and dynamic prompt selection
- Long‑term semantic memory (using Qdrant)
- Full DashScope provider integration for cloud‑based fallback
- Web UI

---

## 9. License  

This project is licensed under the **GNU Affero General Public License v3.0** (AGPL‑3.0).  
See the [LICENSE](LICENSE) file for details.

---

*Questions? Open an issue or refer to the daily development log in `project.md`.*
