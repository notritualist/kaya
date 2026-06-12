"""
main-srv/src/orchestrator/orchestrator_entry.py

Orchestrator Input Interface

Single entry point for creating orchestrator tasks.
All modules (session_manager, phs_scheduler, lifecycle_manager) must use this interface instead of direct SQL INSERT.

Supported task types:
    user_answer_generation: generates the final response to the user.
    phs_baseline_drift: natural baseline drift (OU process + sedimentation).
    phs_momentary_decay: momentary decay to baseline.

Architecture:
    Generic function create_orchestrator_task() with task_type validation.
    Specialized wrappers: on_user_message / schedule_phs_baseline_drift / schedule_phs_momentary_decay.
    Lifecycle activity recorded with actor_id from the message.
    Agent version passed globally via version.py.
"""

__version__ = "1.2.0"
__description__ = "Entry point for orchestrator"

import logging
import psycopg2
from typing import Optional, Dict, Any
from psycopg2.extras import RealDictCursor, Json
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


def create_orchestrator_task(
    task_type_name: str,
    input_data: Dict[str, Any],
    priority: float = 0.5
) -> str:
    """
    Универсальная функция создания задачи оркестратора.
    
    Единственная точка входа для создания задач. Все модули должны
    использовать эту функцию вместо прямого INSERT.
    
    Логика:
    1. Проверка существования типа задачи в orchestrator.task_types
    2. Создание записи в orchestrator.orchestrator_tasks
    3. Возврат UUID созданной задачи
    
    Args:
        task_type_name: Имя типа задачи (user_answer_generation, phs_baseline_drift, etc.)
        input_data: Данные для задачи (message_id, drift_type, etc.)
        priority: Приоритет задачи (0.0-1.0, по умолчанию 0.5)
        
    Returns:
        str: UUID созданной задачи
        
    Raises:
        RuntimeError: если тип задачи не найден или ошибка БД
    """
    from db_manager.db_manager import load_postgres_config
    db_config = load_postgres_config()
    
    try:
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # 1. Проверяем существование типа задачи
                cur.execute(
                    "SELECT id FROM orchestrator.task_types WHERE type_name = %s",
                    (task_type_name,)
                )
                row = cur.fetchone()
                if not row:
                    raise RuntimeError(
                        f"Task type '{task_type_name}' not found in orchestrator.task_types. "
                        "Ensure migration was applied."
                    )
                task_type_id = row["id"]
                
                # 2. Создаём задачу
                cur.execute("""
                    INSERT INTO orchestrator.orchestrator_tasks (
                        task_type_id, input_data, priority, status, agent_version, created_at
                    ) VALUES (
                        %s, %s, %s, 'pending', %s, NOW()
                    )
                    RETURNING id
                """, (task_type_id, Json(input_data), priority, agent_version))
                
                task_id = str(cur.fetchone()["id"])
                conn.commit()
                
                logger.info(
                    f"Orchestrator task created: type={task_type_name}, "
                    f"task_id={task_id[:8]}, priority={priority}"
                )
                return task_id
                
    except psycopg2.Error as e:
        logger.error(f"Database error creating orchestrator task: {e}", exc_info=True)
        raise RuntimeError(f"Failed to create orchestrator task: {e}") from e
    
def schedule_phs_baseline_drift(
    drift_type: str,
    baseline_id: Optional[str] = None,
    priority: float = 0.3
) -> str:
    """
    Создаёт задачу дрейфа baseline.
    
    Обёртка над create_orchestrator_task для phs_baseline_drift.
    
    Args:
        drift_type: Тип дрейфа ('hourly', 'offline')
        baseline_id: ID активного baseline (опционально)
        priority: Приоритет задачи
        
    Returns:
        str: UUID созданной задачи
    """
    input_data = {"drift_type": drift_type}
    if baseline_id:
        input_data["baseline_id"] = baseline_id
    
    return create_orchestrator_task(
        task_type_name="phs_baseline_drift",
        input_data=input_data,
        priority=priority
    )


def schedule_phs_momentary_decay(
    decay_type: str = "natural",
    priority: float = 0.4
) -> str:
    """
    Создаёт задачу затухания momentary.
    
    Обёртка над create_orchestrator_task для phs_momentary_decay.
    
    Args:
        decay_type: Тип затухания ('natural')
        priority: Приоритет задачи
        
    Returns:
        str: UUID созданной задачи
    """
    return create_orchestrator_task(
        task_type_name="phs_momentary_decay",
        input_data={"decay_type": decay_type},
        priority=priority
    )