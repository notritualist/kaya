"""
main-srv/src/orchestrator/orchestrator_entry.py

Orchestrator input interface.

Called from session_manager after saving the user's message.
Creates a task to generate the final answer (user_answer_generation).
Records user activity for global lifecycle state.

Architectural requirements:
- Only one task type: user_answer_generation
- All data from existing Postgres tables
- Lifecycle activity recorded with actor_id from message
- Agent version passed globally
"""

__version__ = "1.1.0"
__description__ = "Entry point for orchestrator"

import logging
import psycopg2
from psycopg2.extras import RealDictCursor, Json
from phs_service.lifecycle_manager import LifecycleManager
from phs_service.lifecycle_manager import LifecycleManager

# Глобальная версия проекта (из pyproject.toml через version.py)
from version import __version__ as agent_version

logger = logging.getLogger(__name__)


def on_user_message(message_id: str) -> str:
    """
   Создаёт задачу оркестратору и фиксирует активность для lifecycle.
    
    Логика:
    1. Валидация message_id
    2. Загрузка actor_id из dialogs.row_messages
    3. Фиксация активности через LifecycleManager.record_activity
    4. Создание задачи user_answer_generation
    
    Args:
        message_id: UUID сообщения из dialogs.row_messages
    
    Returns:
        str: UUID созданной задачи в orchestrator.orchestrator_tasks
    
    Raises:
        ValueError: если message_id пустой или недействительный
        RuntimeError: если сообщение или тип задачи не найдены
        psycopg2.Error: при ошибках БД
    """
    if not message_id or not isinstance(message_id, str):
        raise ValueError("message_id must be a non-empty string")

    from db_manager.db_manager import load_postgres_config
    db_config = load_postgres_config()
    conn = None

    try:
       conn = psycopg2.connect(**db_config)
       with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # === 1. Получаем actor_id из сообщения ===
            cur.execute(
                "SELECT actor_id FROM dialogs.row_messages WHERE id = %s",
                (message_id,)
            )
            msg_row = cur.fetchone()
            if not msg_row:
                raise RuntimeError(f"Message {message_id} not found in dialogs.row_messages")
            actor_id = str(msg_row['actor_id'])

            # === 2. Фиксируем активность для lifecycle ===
            lifecycle_mgr = LifecycleManager(db_config)
            lifecycle_mgr.record_activity(actor_id, 'user_activity')

            # === 3. Получаем ID типа задачи ===
            cur.execute(
                "SELECT id FROM orchestrator.task_types WHERE type_name = %s",
                ("user_answer_generation",)
            )
            row = cur.fetchone()
            if not row:
                raise RuntimeError(
                    "Task type 'user_answer_generation' not found in orchestrator.task_types. "
                    "Ensure V001_initial.sql migration was applied."
                )
            task_type_id = row["id"]

            # === 4. Создаём задачу ===
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
                f"message_id={message_id[:8]}..., priority={priority}, "
                f"activity recorded for actor {actor_id[:8]}"
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