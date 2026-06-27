"""
main-srv/src/orchestrator/orchestrator_entry.py

Orchestrator Input Interface

Single entry point for creating orchestrator tasks.
All modules (session_manager, phs_scheduler, lifecycle_manager) must use this interface instead of direct SQL INSERT.

Supported task types:
- phs_affective_analysis: pre-reflexive affective analysis of agent-user message pairs.
- user_answer_generation: generates the final response to the user.
- phs_baseline_drift: natural baseline drift (OU process + sedimentation).
- phs_momentary_decay: momentary decay to baseline.

Architecture:
- Generic function create_orchestrator_task() with task_type validation.
- Specialized wrappers: on_user_message / schedule_phs_baseline_drift / schedule_phs_momentary_decay.
- All tasks are stamped with current PHS snapshot (baseline_id, momentary_id) via V004.
- For dialog-bound tasks, momentary_id is populated.
- For background PHS tasks, momentary_id may be NULL.
- Task dependencies enforced via parent_task_id field.
- Orchestrator checks parent_task_id status before execution.

Lifecycle integration:
- Activity recorded with actor_id from the message.
- Agent version passed globally via version.py.

Task creation flow for user messages:
1. on_user_message creates phs_affective_analysis task (priority 0.85).
2. on_user_message creates user_answer_generation task (priority 0.7) with parent_task_id = analysis_task_id.
3. Orchestrator ensures analysis completes before generation via parent_task_id check.
"""

__version__ = "1.3.0"
__description__ = "Entry point for orchestrator"

import logging
import psycopg2
from typing import Optional, Dict, Any
from psycopg2.extras import RealDictCursor, Json
from phs_service.lifecycle_manager import LifecycleManager
from phs_service.phs_cache import get_current_phs_snapshot
from db_manager.db_manager import load_postgres_config


# Глобальная версия проекта (из pyproject.toml через version.py)
from version import __version__ as agent_version

logger = logging.getLogger(__name__)


def on_user_message(message_id: str) -> str:
    """
    Создаёт задачи аффективного анализа и генерации ответа при получении сообщения пользователя.
    
    Логика:
    1. Валидация message_id и загрузка actor_id из dialogs.row_messages.
    2. Фиксация активности через LifecycleManager.record_activity.
    3. Получение PHS-среза (baseline_id, momentary_id) для actor_id.
    4. Создание задачи phs_affective_analysis с приоритетом 0.85.
    5. Создание задачи user_answer_generation с приоритетом 0.7 и parent_task_id,
       ссылающимся на задачу анализа.
    6. Оркестратор гарантирует выполнение анализа до генерации через проверку parent_task_id.
    
    Обе задачи штампуются текущим PHS-срезом для полной трассируемости.
    Связь через parent_task_id обеспечивает строгую последовательность без блокировки очереди.
    
    Args:
        message_id: UUID сообщения из dialogs.row_messages
        
    Returns:
        str: UUID задачи аффективного анализа (родительской)
        
    Raises:
        ValueError: если message_id пустой или не строка
        RuntimeError: если сообщение не найдено или типы задач отсутствуют в БД
    """
    if not message_id or not isinstance(message_id, str):
        raise ValueError("message_id must be a non-empty string")

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

            # === 3. Получаем ID типов задач ===
            cur.execute(
                "SELECT id, type_name FROM orchestrator.task_types WHERE type_name IN (%s, %s)",
                ("phs_affective_analysis", "user_answer_generation")
            )
            type_rows = {row['type_name']: row['id'] for row in cur.fetchall()}
            
            if "phs_affective_analysis" not in type_rows:
                raise RuntimeError(
                    "Task type 'phs_affective_analysis' not found in orchestrator.task_types. "
                    "Ensure V003 migration was applied."
                )
            if "user_answer_generation" not in type_rows:
                raise RuntimeError(
                    "Task type 'user_answer_generation' not found in orchestrator.task_types. "
                    "Ensure V001 migration was applied."
                )

            analysis_type_id = type_rows["phs_affective_analysis"]
            generation_type_id = type_rows["user_answer_generation"]

            # === 4. Получаем PHS-срез для actor_id ===
            baseline_id, momentary_id = get_current_phs_snapshot(db_config, actor_id)

            # === 5. Создаём задачу АФФЕКТИВНОГО АНАЛИЗА ===
            cur.execute("""
                INSERT INTO orchestrator.orchestrator_tasks (
                    task_type_id,
                    input_data,
                    priority,
                    status,
                    baseline_id,
                    momentary_id,
                    agent_version,
                    created_at
                ) VALUES (
                    %(task_type_id)s,
                    %(input_data)s,
                    %(priority)s,
                    'pending',
                    %(baseline_id)s,
                    %(momentary_id)s,
                    %(agent_version)s,
                    NOW()
                )
                RETURNING id
            """, {
                "task_type_id": analysis_type_id,
                "input_data": Json({"message_id": message_id, "user_actor_id": actor_id}),
                "priority": 0.85,  # Высокий приоритет
                "baseline_id": baseline_id,
                "momentary_id": momentary_id,
                "agent_version": agent_version
            })
            analysis_task_id = str(cur.fetchone()["id"])

            # === 6. Создаём задачу ГЕНЕРАЦИИ с ссылкой на родителя ===
            cur.execute("""
                INSERT INTO orchestrator.orchestrator_tasks (
                    task_type_id,
                    parent_task_id,
                    input_data,
                    priority,
                    status,
                    baseline_id,
                    momentary_id,
                    agent_version,
                    created_at
                ) VALUES (
                    %(task_type_id)s,
                    %(parent_task_id)s,
                    %(input_data)s,
                    %(priority)s,
                    'pending',
                    %(baseline_id)s,
                    %(momentary_id)s,
                    %(agent_version)s,
                    NOW()
                )
                RETURNING id
            """, {
                "task_type_id": generation_type_id,
                "parent_task_id": analysis_task_id,
                "input_data": Json({"message_id": message_id}),
                "priority": 0.7,  # Приоритет ниже анализа
                "baseline_id": baseline_id,
                "momentary_id": momentary_id,
                "agent_version": agent_version
            })
            generation_task_id = str(cur.fetchone()["id"])

            conn.commit()

            logger.info(
                f"Tasks created: analysis={analysis_task_id[:8]} (prio=0.85), "
                f"generation={generation_task_id[:8]} (prio=0.7, parent={analysis_task_id[:8]}), "
                f"activity recorded for actor {actor_id[:8]}"
            )
            return analysis_task_id

    except psycopg2.Error as e:
        if conn:
            conn.rollback()
        logger.error(f"Database error while creating orchestrator tasks: {e}", exc_info=True)
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
    priority: float = 0.5,
    baseline_id: Optional[str] = None,
    momentary_id: Optional[str] = None,
    parent_task_id: Optional[str] = None
) -> str:
    """
    Универсальная функция создания задачи оркестратора.
    
    Единственная точка входа для создания задач. Все модули должны
    использовать эту функцию вместо прямого INSERT.
    Опционально штампует задачу текущим состоянием ПГС и связывает с родительской задачей.
    
    Логика:
    1. Проверка существования типа задачи в orchestrator.task_types.
    2. Создание записи в orchestrator.orchestrator_tasks с PHS-штампами и parent_task_id.
    3. Возврат UUID созданной задачи.
    
    Args:
        task_type_name: Имя типа задачи (phs_affective_analysis, user_answer_generation, phs_baseline_drift, etc.)
        input_data: Данные для задачи (message_id, drift_type, etc.)
        priority: Приоритет задачи (0.0-1.0, по умолчанию 0.5)
        baseline_id: UUID активного baseline (V004, опционально)
        momentary_id: UUID активного momentary (V004, опционально)
        parent_task_id: UUID родительской задачи для зависимостей (опционально)
        
    Returns:
        str: UUID созданной задачи
        
    Raises:
        RuntimeError: если тип задачи не найден или ошибка БД
    """
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
                
                # 2. Создаём задачу с PHS-штампами и parent_task_id
                cur.execute("""
                    INSERT INTO orchestrator.orchestrator_tasks (
                        task_type_id, parent_task_id, input_data, priority, status, 
                        baseline_id, momentary_id, agent_version, created_at
                    ) VALUES (
                        %s, %s, %s, %s, 'pending', %s, %s, %s, NOW()
                    )
                    RETURNING id
                """, (
                    task_type_id, 
                    parent_task_id, 
                    Json(input_data), 
                    priority, 
                    baseline_id, 
                    momentary_id, 
                    agent_version
                ))
                
                task_id = str(cur.fetchone()["id"])
                conn.commit()
                
                logger.info(
                    f"Orchestrator task created: type={task_type_name}, "
                    f"task_id={task_id[:8]}, priority={priority}, "
                    f"parent_task_id={parent_task_id[:8] if parent_task_id else 'None'}"
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
    Фоновая задача ПГС, momentary_id не применяется.
    
    Args:
        drift_type: Тип дрейфа ('hourly', 'offline')
        baseline_id: ID активного baseline (опционально)
        priority: Приоритет задачи (по умолчанию 0.3)
        
    Returns:
        str: UUID созданной задачи
    """
    input_data = {"drift_type": drift_type}
    if baseline_id:
        input_data["baseline_id"] = baseline_id
    
    return create_orchestrator_task(
        task_type_name="phs_baseline_drift",
        input_data=input_data,
        priority=priority,
        baseline_id=baseline_id,
        momentary_id=None  # Фоновая задача ПГС
    )

def schedule_phs_momentary_decay(
    decay_type: str = "natural",
    priority: float = 0.4
) -> str:
    """
    Создаёт задачу затухания momentary.
    
    Обёртка над create_orchestrator_task для phs_momentary_decay.
    Фоновая задача ПГС, momentary_id не применяется.
    
    Args:
        decay_type: Тип затухания ('natural')
        priority: Приоритет задачи (по умолчанию 0.4)
        
    Returns:
        str: UUID созданной задачи
    """
    return create_orchestrator_task(
        task_type_name="phs_momentary_decay",
        input_data={"decay_type": decay_type},
        priority=priority,
        baseline_id=None,   # Фоновая задача ПГС
        momentary_id=None   # Фоновая задача ПГС
    )