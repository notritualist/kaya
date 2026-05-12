"""
main-srv/src/orchestrator/orchestrator_entry.py

Orchestrator input interface.
Called from session_manager after saving the user's message.
Creates a task to generate the final answer (user_answer_generation).

Architectural requirements:
- Only one task: user_answer_generation.
- All data is taken from existing Postgres tables.
- The agent version is passed globally.
"""

__version__ = "1.0.0"
__description__ = "Entry point for orchestrator: create final answer generation task"

import logging
import psycopg2
from psycopg2.extras import RealDictCursor, Json

# Глобальная версия проекта (из pyproject.toml через version.py)
from version import __version__ as agent_version

logger = logging.getLogger(__name__)


def on_user_message(message_id: str) -> str:
    """
    Создаёт задачу оркестратору на генерацию финального ответа пользователю.

    Args:
        message_id (str): UUID сообщения из dialogs.row_messages

    Returns:
        str: UUID созданной задачи в orchestrator.orchestrator_tasks

    Raises:
        ValueError: если message_id пустой или недействительный
        RuntimeError: если тип задачи не найден в БД
        psycopg2.Error: при ошибках работы с базой данных
    """
    if not message_id or not isinstance(message_id, str):
        raise ValueError("message_id must be a non-empty string")

    from db_manager.db_manager import load_postgres_config
    db_config = load_postgres_config()
    conn = None

    try:
        conn = psycopg2.connect(**db_config)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # 1. Получаем ID типа задачи 'user_answer_generation'
            cur.execute("""
                SELECT id FROM orchestrator.task_types 
                WHERE type_name = %s
            """, ("user_answer_generation",))
            row = cur.fetchone()
            if not row:
                raise RuntimeError(
                    "Task type 'user_answer_generation' not found in orchestrator.task_types. "
                    "Ensure V001_initial.sql migration was applied."
                )
            task_type_id = row["id"]

            # 2. Создаём задачу с приоритетом 0.7
            priority = 0.7

            cur.execute("""
                INSERT INTO orchestrator.orchestrator_tasks (
                    task_type_id,
                    input_data,
                    priority,
                    status,
                    agent_version,
                    created_at
                ) VALUES (
                    %(task_type_id)s,
                    %(input_data)s,
                    %(priority)s,
                    'pending',
                    %(agent_version)s,
                    NOW()
                )
                RETURNING id
            """, {
                "task_type_id": task_type_id,
                "input_data": Json({"message_id": message_id}),  # ← ВОТ ТАК
                "priority": priority,
                "agent_version": agent_version
            })

            task_id = str(cur.fetchone()["id"])
            conn.commit()

            logger.info(
                f"Orchestrator task created: task_id={task_id[:8]}..., "
                f"message_id={message_id[:8]}..., priority={priority}"
            )
            return task_id

    except psycopg2.Error as e:
        if conn:
            conn.rollback()
        logger.error(f"Database error while creating orchestrator task: {e}", exc_info=True)
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Unexpected error in orchestrator_entry: {e}", exc_info=True)
        raise
    finally:
        if conn:
            conn.close()