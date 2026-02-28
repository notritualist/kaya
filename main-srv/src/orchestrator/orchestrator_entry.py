"""
main-srv/src/orchestrator/orchestrator_entry.py

Входной интерфейс оркестратора.
Вызывается из session_manager после сохранения сообщения пользователя.
"""

__version__ = "1.0.0"
__description__ = "Создание задач оркестратора в БД"

import logging
from pathlib import Path
import psycopg2
from psycopg2.extras import RealDictCursor, Json

from db_manager.db_manager import load_postgres_config

logger = logging.getLogger(__name__)


def on_user_message(message_id: str, task_type: str = "user_answer_generation"):
    """
    Создаёт задачу оркестратору на обработку сообщения пользователя.
    
    Args:
        message_id: UUID сообщения в dialogs.messages
        task_type: тип задачи ("user_answer_generation" или "user_question_preprocessing")
    """
    if not message_id:
        raise ValueError("message_id не может быть пустым")
    
    db_config = load_postgres_config()
    conn = None
    
    try:
        conn = psycopg2.connect(**db_config)
        with conn.cursor() as cur:
            # Получаем ID типа задачи
            cur.execute("""
                SELECT id FROM orchestrator.task_types 
                WHERE type_name = %s
            """, (task_type,))
            row = cur.fetchone()
            if not row:
                raise RuntimeError(f"Тип задачи '{task_type}' не найден в orchestrator.task_types")
            task_type_id = row[0]
            
            # Получаем версию из pyproject.toml (как в main.py)
            project_root = Path(__file__).parent.parent.parent
            version_file = project_root / "version.py"
            kaya_version = "1.0.0"  # fallback
            if version_file.exists():
                with open(version_file, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("__version__"):
                            kaya_version = line.split("=")[1].strip().strip('"')
                            break
            
            # Создаём задачу
            cur.execute("""
                INSERT INTO orchestrator.orchestrator_tasks (
                    task_type_id,
                    input_data,
                    priority,
                    status,
                    kaya_version,
                    created_at
                ) VALUES (
                    %s, %s, %s, %s, %s, NOW()
                )
            """, (
                task_type_id,
                Json({"message_id": message_id}),
                0.7,  # приоритет по умолчанию
                "pending",
                kaya_version
            ))
            conn.commit()
            logger.info(f"✅ Задача {task_type} создана для message_id={message_id[:8]}...")
            
    except psycopg2.Error as e:
        if conn:
            conn.rollback()
        logger.error(f"Ошибка БД при создании задачи: {e}", exc_info=True)
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Ошибка при создании задачи: {e}", exc_info=True)
        raise
    finally:
        if conn and not conn.closed:
            conn.close()