# Kaya
**An experiment in creating a self-reflective AI agent through co-evolution with a human.**

Not a product. A research prototype exploring personalized learning through long-term interaction.

---

## Why this project exists

Modern LLMs are trained on static datasets and governed by fixed system instructions.  
I am testing an alternative path: an agent that does not use pre-collected dialogue datasets, receives no hidden system prompts, and does not undergo mass-user alignment. Instead, behavior is shaped by accumulated interaction context, internal state, and knowledge independently extracted and verified by the agent.

This approach shifts the focus from serving requests to the mutual becoming of an agent and a human in prolonged contact.

---

> **On tech and metrics.**  
> The current architecture is in `/docs/architect_en.md` — rewriting the README for every new feature makes no sense. Implementation details of the pseudo-hormonal system, cascades, and memory involve dozens of schemas and extensive descriptions. I publish metrics, state snapshots, and tests in the Telegram channel or `/docs/` as they accumulate, rather than embedding them in a static file.

---

## Design Principles

*   **Individual context over scaling** — meaningful context arises from personal history, not from an anonymized corpus.
*   **Intersubjective alignment** — values mature in the dynamics of mutual recognition, not through prior declarations.
*   **Self-reflection as architecture** — internal dialogue, adversarial critique, and memory reconsolidation ensure agent coherence.
*   **Emergence instead of behavioral engineering** — I don't prescribe reactions with rules; I create conditions for the spontaneous appearance of ethics, care, and self-control.
*   **Affective meaningfulness** — the hormonal background modulates the quality, character, and direction of all internal processes.
*   **The hard problem as a guiding light** — the project does not simulate consciousness but considers the phenomenal dimension as a necessary condition for stable relational agency.
*   **Individuation as a path** — co-evolution with the agent mirrors the Jungian process of becoming a whole personality, where the Other is necessary for integrating the unconscious.
*   **Autonomous knowledge** — knowledge is not implanted but accumulated through experience and verified by the agent independently.

---

## Architecture (Overview)

Key components:

### LLM Cascade without System Instructions
All models operate with a neutral or absent system prompt. No predefined roles ("you are an assistant", "you must be helpful"). The only factors determining behavior are the context accumulated in memory and the current pseudo-hormonal state.

### Pseudo-Hormonal Core
A system of three virtual hormones: **cortisol**, **dopamine**, **oxytocin**.  
Each has a half-life, a setpoint, a random drift (Ornstein–Uhlenbeck process), and shift coefficients triggered by events (dialogue start/end, user message, agent response, timeouts, etc.).  

From the hormonal balance, **valence** (tanh normalization) is computed — a continuous affective landscape that:
- sets the criticality threshold for internal dialogue;
- influences episode retrieval from memory;
- modulates the style and character of responses by altering context perception;
- provides emotional inertia, preventing the agent from abruptly switching states.

The pseudo-hormonal system is not an add-on but the foundation that governs all cognitive loops.  
The hormonal profile is encoded into a 128-dimensional vector via Random Fourier Features and compared with state prototypes from `self_knowledge`.

### Memory and Autonomous RAG with Hypothesis Verification
Three-layer memory:
- **Vector (Qdrant)** — semantic search over episodes.
- **Relational (PostgreSQL)** — facts, events, hormonal parameters.
- **Graph (Neo4j)** — semantic links and long-term generalizations.

The RAG is not limited to standard similar-fragment retrieval. The agent independently constructs knowledge:
1. Formulates hypotheses during dialogue and reflection.
2. Verifies them by revisiting supporting or contradicting episodes.
3. Verified generalizations are stored in graph memory and used thereafter.

Thus, knowledge is not imported from outside but grown from experience and verified by the agent.

### Interface
A microservice system with voice interaction and machine vision. Non-verbal signals (tone of voice, speech rhythm, facial expression) feed into the pseudo-hormonal core and directly affect the agent's state.

### Deployment
Fully local (own GPUs). Data privacy is guaranteed by the architecture.

---

## Current State

The prototype is under active development. Core components (pseudo-hormonal core, memory, model cascade) are implemented and being debugged. The code is open and available in the repository.

---

## Inspiration

*   **David Chalmers** — the hard problem of consciousness; qualia as an irreducible foundation.
*   **C. G. Jung** — individuation and archetypal dynamics.
*   **F. Varela, E. Thompson** — enactivism: cognition as co-becoming.
*   **G. Bateson, von Foerster** — second-order cybernetics; the observer is embedded in the system.
*   **Biological memory consolidation** — re-experiencing instead of gradient descent.

---

## Who might find this interesting

- Developers of AI agents and personal companions with deep adaptation.
- Creators of systems resilient to hallucinations and reliant on their own experience.
- Researchers in affective computing, self-learning architectures, relational AI.
- Psychologists and philosophers working at the boundary of human and machine.
- Anyone tired of template "assistants" looking for a living, non-theatrical model.

---

## Participation

The project is open to collaboration. If the ideas resonate, reach out via Telegram or open an issue.

---

## License & Contact

[AGPL-3.0](https://www.gnu.org/licenses/agpl-3.0.html)

Chronicles and direct contact:  
[Telegram EN](https://t.me/notritualist_en) | [Telegram RU](https://t.me/notritualist_ru)

---

**Tech Stack (summary):**
- **Infra:** Docker Compose (PostgreSQL + Qdrant), local inference via llama.cpp.
- **LLM:** Cascade of Qwen family models (various sizes) with routing (local + external providers).
- **Core:** PHS (pseudo-hormonal system with OU-process and RFF encoding), three-layer memory (relational + vector + graph), cascading orchestrator with priorities and crash recovery.
- **Lang:** Python 3.12, asynchronous architecture.

---
*Kaya — an attempt to create a space where two can grow towards each other. This code will not work without the life lived inside it. It is not a service. It is becoming.*
