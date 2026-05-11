"""
main-srv/src/dialog_services/dialogue_manager.py

A stateless module for dialog management.
Responsible for:
- Creating and closing dialogs
- Lazy checking of inactivity timeouts
- Cleaning up stuck records at system startup (agent code only)

All functions work directly with the database via psycopg2.
They don't store state in memory, allowing for safe operation with concurrent users.
"""

version = "1.0.0"
description = "Module for dialog management"

import logging
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timezone

# Настраиваемая константа таймаута неактивности (в минутах)
DIALOGUE_INACTIVITY_TIMEOUT_MINUTES = 1

logger = logging.getLogger(__name__)


def ensure_active_dialogue(
    db_config: dict, 
    session_id: str, 
    actor_id: str, 
    agent_version: str
) -> str:
    """
    Основная точка входа для получения ID текущего диалога.
    Реализует логику "ленивой" проверки таймаута:
    1. Ищет активный диалог для данного actor_id и session_id.
    2. Если найден: проверяет last_activity_at.
       - Если прошло больше DIALOGUE_INACTIVITY_TIMEOUT_MINUTES -> закрывает старый, создает новый.
       - Иначе -> обновляет last_activity_at и возвращает ID.
    3. Если не найден -> создает новый и возвращает ID.
    
    Returns:
        str: UUID активного или вновь созданного диалога.
    """
    conn = psycopg2.connect(**db_config)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        # Поиск последнего активного диалога пользователя
        cur.execute("""
            SELECT id, last_activity_at 
            FROM dialogs.dialogues 
            WHERE actor_id = %s AND session_id = %s AND status = 'active'
            ORDER BY start_at DESC LIMIT 1
        """, (actor_id, session_id))
        
        active_dialogue = cur.fetchone()
        now = datetime.now(timezone.utc)
        dialogue_id = None

        if active_dialogue:
            elapsed_sec = (now - active_dialogue['last_activity_at']).total_seconds()
            timeout_sec = DIALOGUE_INACTIVITY_TIMEOUT_MINUTES * 60

            if elapsed_sec > timeout_sec:
                logger.info(
                    f"Dialogue {active_dialogue['id'][:8]} expired after {elapsed_sec:.1f}s. "
                    f"Closing due to inactivity."
                )
                _close_dialogue(cur, active_dialogue['id'], 'inactivity_timeout')
                dialogue_id = _create_dialogue(cur, session_id, actor_id, agent_version)
            else:
                # Диалог активен, просто обновляем метку активности
                dialogue_id = active_dialogue['id']
                cur.execute(
                    "UPDATE dialogs.dialogues SET last_activity_at = %s WHERE id = %s",
                    (now, dialogue_id)
                )
        else:
            # Активного диалога нет (первое сообщение или после закрытия)
            logger.debug("No active dialogue found. Creating new one.")
            dialogue_id = _create_dialogue(cur, session_id, actor_id, agent_version)

        conn.commit()
        return dialogue_id

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def close_active_dialogue(db_config: dict, session_id: str, actor_id: str, reason: str):
    """
    Закрывает текущий активный диалог с указанной причиной.
    Используется при Ctrl+N или корректном завершении сессии.
    """
    conn = psycopg2.connect(**db_config)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        cur.execute("""
            SELECT id FROM dialogs.dialogues 
            WHERE actor_id = %s AND session_id = %s AND status = 'active'
            ORDER BY start_at DESC LIMIT 1
        """, (actor_id, session_id))
        
        row = cur.fetchone()
        if row:
            _close_dialogue(cur, row['id'], reason)
            logger.info(f"Active dialogue {row['id'][:8]} closed with reason: {reason}")
            conn.commit()
        else:
            logger.debug("No active dialogue to close for this session/actor.")
            
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def close_dangling_dialogues(db_config: dict) -> int:
    """
    Завершает все зависшие активные диалоги при перезапуске системы.
    Выполняется напрямую в коде агента, без хранимых процедур БД.
    Вызывается из SessionManager перед стартом интерфейса.
    """
    logger.info("Checking for dangling dialogues...")
    conn = psycopg2.connect(**db_config)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE dialogs.dialogues
                SET status = 'completed', reason = 'system_restart'::dialog_close_reason, end_at = NOW()
                WHERE status = 'active'
                  AND session_id IN (SELECT id FROM dialogs.sessions WHERE status = 'active')
            """)
            count = cur.rowcount
            conn.commit()
            if count > 0:
                logger.warning(f"Closed {count} dangling dialogues on startup.")
            else:
                logger.debug("No dangling dialogues found.")
            return count
    except Exception as e:
        logger.error(f"Error closing dangling dialogues: {e}", exc_info=True)
        conn.rollback()
        return 0
    finally:
        conn.close()


# --- Внутренние утилиты ---

def _create_dialogue(cur, session_id: str, actor_id: str, agent_version: str) -> str:
    """Создает запись диалога внутри активной транзакции."""
    cur.execute("""
        INSERT INTO dialogs.dialogues (session_id, actor_id, agent_version)
        VALUES (%s, %s, %s)
        RETURNING id
    """, (session_id, actor_id, agent_version))
    new_id = str(cur.fetchone()['id'])
    logger.debug(f"New dialogue created: {new_id[:8]}")
    return new_id


def _close_dialogue(cur, dialogue_id: str, reason: str):
    """Закрывает запись диалога внутри активной транзакции."""
    cur.execute("""
        UPDATE dialogs.dialogues 
        SET status = 'completed', reason = %s::dialog_close_reason, end_at = NOW()
        WHERE id = %s
    """, (reason, dialogue_id))