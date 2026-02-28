#!/bin/bash
# Запуск Qwen3-8B через llama-server

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR/.."

SERVER_BIN="$PROJECT_ROOT/llama.cpp/build/bin/llama-server"
MODEL_PATH="$PROJECT_ROOT/models/qwen3_8b/Qwen3-8B-Q4_K_M.gguf"

# Проверки
if [[ ! -f "$SERVER_BIN" ]]; then
  echo "❌ Server not found: $SERVER_BIN" >&2
  exit 1
fi

if [[ ! -f "$MODEL_PATH" ]]; then
  echo "❌ Model not found: $MODEL_PATH" >&2
  exit 1
fi

echo "🚀 Launching the LLM model orchestrator server..."
echo "   Model: $MODEL_PATH"
echo "   Server: $SERVER_BIN"

exec "$SERVER_BIN" \
  -m "$MODEL_PATH" \
  --ctx-size 8192 \
  --n-gpu-layers -1 \
  --cache-type-k f16 \
  --cache-type-v f16 \
  --threads 4 \
  --batch-size 2048 \
  --parallel 1 \
  --port 8081 \
  --host main-srv
