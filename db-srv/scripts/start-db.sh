#!/bin/bash
set -e

cd /mnt/work/kaya/db-srv/configs

echo "🔄 Запуск PostgreSQL в Docker..."
docker compose up -d

echo "✅ Проверка:"
docker ps --filter name=postgres_db --format "table {{.Names}}\t{{.Status}}"

echo "📄 Логи (последние 10 строк):"
docker logs --tail 10 postgres_db
