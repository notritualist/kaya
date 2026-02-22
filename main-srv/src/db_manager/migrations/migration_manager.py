"""/main-srv/src/db_manager/migrations/migration_manager.py"""

__version__ = "1.0.0"
__description__ = "Модуль миграций БД Postgres"


import re
import logging
import psycopg2
from pathlib import Path
from typing import List, Optional
from dataclasses import dataclass
from datetime import datetime
from psycopg2.extensions import connection
from version import __version__ as kaya_version 


# Логгер для модуля
logger = logging.getLogger(__name__)


@dataclass
class MigrationRecord:
    """Модель записи миграций"""
    version: str
    description: str
    status: str
    applied_at: datetime
    kaya_version: str


class MigrationManager:
    def __init__(self, migrations_path: str = "migrations"):
        self.migrations_path = Path(migrations_path)
        self.kaya_version = kaya_version
        
    
    def get_applied_migrations(self, conn: connection) -> List[MigrationRecord]:
        """Получает список уже примененных миграций Postgres"""
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT version, description, status, applied_at, kaya_version
                FROM architect.schema_version 
                WHERE status = 'applied'
                ORDER BY version
            """)
            
            migrations = []
            result = cur.fetchall()
            if result:
                for row in result:
                    migrations.append(MigrationRecord(
                        version=row[0],
                        description=row[1],
                        status=row[2],
                        applied_at=row[3],
                        kaya_version=row[4]
                    ))
            return migrations
        finally:
            cur.close()
    
    
    def get_pending_migrations(self, conn: connection) -> List[Path]:
        """Находит миграции Postgres, которые еще не применены"""
        applied = {m.version for m in self.get_applied_migrations(conn)}
        pending = []
        
        if not self.migrations_path.exists():
            return pending
            
        for file_path in sorted(self.migrations_path.glob("*.sql")):
            version = self._extract_version(file_path.name)
            if version and version not in applied:
                pending.append(file_path)
                
        return pending
    
    
    def _extract_version(self, filename: str) -> Optional[str]:
        """Извлекает версию из имени файла"""
        match = re.match(r"V?(\d+)_.*\.sql", filename)
        if match:
            number = match.group(1)
            return f"V{number.zfill(3)}"  # дополняем нулями слева
        return None
    
    
    def apply_migration(self, conn: connection, migration_file: Path) -> bool:
        """Применяет одну миграцию"""
        version = self._extract_version(migration_file.name)
        if not version:
            logger.error(f"Неверное имя файла миграций Postgres: {migration_file.name}")
            return False
            
        try:
            with open(migration_file, 'r', encoding='utf-8') as f:
                sql_content = f.read()
            
            # Извлекаем описание из комментариев SQL
            description = self._extract_description(sql_content)
            
            cur = conn.cursor()
            
            # Выполняем SQL миграции
            cur.execute(sql_content)
            
            # Записываем в журнал миграций
            cur.execute("""
                INSERT INTO architect.schema_version 
                (version, description, status, kaya_version)
                VALUES (%s, %s, 'applied', %s)
            """, (version, description, self.kaya_version))
            
            conn.commit()
            logger.info(f"Применена миграция БД Postgres: {version} - {description}")
            return True
            
        except Exception as e:
            conn.rollback()
            logger.error(f"Не удалось применить миграцию БД Postgres {migration_file}: {e}")
            return False
    
    
    def _extract_description(self, sql_content: str) -> str:
        """Извлекает описание из комментариев SQL"""
        lines = sql_content.split('\n')
        for line in lines:
            if line.startswith('-- Описание:'):
                return line.replace('-- Описание:', '').strip()
        return "Не найдено описание"
    
    
    def ensure_schema_ready(self, postgres_config: dict) -> bool:
        try:
            with psycopg2.connect(**postgres_config) as conn:
                
                # Проверяем, существует ли таблица миграций Postgres
                if not self._check_migrations_table_exists(conn):
                    logger.info("Первый запуск: создание базовой схемы миграций Postgres...")
                    self._create_initial_schema(conn)
                
                # Применяем pending миграции Postgres
                pending = self.get_pending_migrations(conn)
                if pending:
                    logger.info(f"Обнаружено {len(pending)} ожидающих миграций Postgres")
                    for migration_file in pending:
                        if not self.apply_migration(conn, migration_file):
                            return False
                else:
                    logger.info("Структура базы данных Postgres соответствует миграциям")
                    
                return True
            
        except Exception as e:
            logger.error(f"Ошибка миграций БД Postgres: {e}")
            return False
    
    
    def _check_migrations_table_exists(self, conn: connection) -> bool:
        """Проверяет существование таблицы миграций в Postgres"""
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables 
                    WHERE table_schema = 'architect' 
                    AND table_name = 'schema_version'
                )
            """)
            result = cur.fetchone()
            return result[0] if result else False
        finally:
            cur.close()
    
    
    def _create_initial_schema(self, conn: connection):
        """Создает начальную схему если БД Postgres пустая"""
        cur = conn.cursor()
        try:
            # Создаем схему architect если не существует
            cur.execute("CREATE SCHEMA IF NOT EXISTS architect")
            
            # Создаем таблицу миграций
            cur.execute("""
                CREATE TABLE architect.schema_version (
                    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
                    version VARCHAR(10) NOT NULL UNIQUE,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('applied', 'rolled_back')),
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    kaya_version TEXT NOT NULL
                )
            """)
            
            # Добавляем комментарии
            cur.execute("COMMENT ON TABLE architect.schema_version IS 'Таблица-журнал для отслеживания истории миграций схемы базы данных.'")
            cur.execute("COMMENT ON COLUMN architect.schema_version.id IS 'Уникальный идентификатор строки в журнале миграций.'")
            cur.execute("COMMENT ON COLUMN architect.schema_version.version IS 'Номер версии миграции (например, V001, V002). Используется для определения порядка применения.'")
            cur.execute("COMMENT ON COLUMN architect.schema_version.description IS 'Подробное описание изменений, внесенных в схему БД этой миграцией.'")
            cur.execute("COMMENT ON COLUMN architect.schema_version.status IS 'Текущий статус миграции: applied - применена, rolled_back - откатана.'")
            cur.execute("COMMENT ON COLUMN architect.schema_version.applied_at IS 'Метка времени (в UTC) момента применения или отката миграции.'")
            cur.execute("COMMENT ON COLUMN architect.schema_version.kaya_version IS 'Версия Каи (из pyproject.toml) на момент применения миграции.'")
            
            conn.commit()
            logger.info("Создана базовая таблица миграций Postgres")
        except Exception as e:
            conn.rollback()
            logger.error(f"Не удалось создать базовую таблицу миграций Postgres: {e}")
            raise
        finally:
            cur.close()