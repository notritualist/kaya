"""
main-srv/src/dialog_services/dialogue_manager.py

A module for dialog management that operates directly on DB (no in-memory state).
Responsible for:
- Creating new dialogues and closing existing ones
- Dialogues are closed by the orchestrator via check_dialogue_timeouts (eager check).
- Closing dialogues for active sessions on system startup

All functions work directly with the database via psycopg2.
They don't store state in memory, allowing for safe operation with concurrent users.
"""

version = "1.1.0"
description = "Module for dialog management with DB-configurable timeout"

import logging
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _get_dialogue_timeout_minutes(cur) -> float:
    """
    Загружает таймаут неактивности диалога из state.settings.
    
    Args:
        cur: psycopg2 cursor
    
    Returns:
        float: таймаут в минутах (по умолчанию 30.0)
    """
    cur.execute("""
        SELECT value_float FROM state.settings 
        WHERE param_name = 'dialogue_inactivity_timeout_minutes'
    """)
    row = cur.fetchone()
    if not row or row['value_float'] is None:
        logger.warning("Missing 'dialogue_inactivity_timeout_minutes' in settings, using default 30.0")
        return 30.0
    return float(row['value_float'])


def check_dialogue_timeouts(db_config: dict) -> int:
    """
    Закрывает все диалоги, у которых last_activity_at старше таймаута.
    
    Вызывается оркестратором на каждом пульсе.
    Использует один UPDATE запрос для эффективности.
    
    Args:
        db_config: параметры подключения к PostgreSQL
    
    Returns:
        int: количество закрытых диалогов
    """
    conn = psycopg2.connect(**db_config)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
        timeout_minutes = _get_dialogue_timeout_minutes(cur)
        
        cur.execute("""
            UPDATE dialogs.dialogues
            SET 
                status = 'completed',
                reason = 'inactivity_timeout'::dialog_close_reason,
                end_at = NOW()
            WHERE status = 'active'
              AND last_activity_at < NOW() - (%s * INTERVAL '1 minute')
        """, (timeout_minutes,))
        
        count = cur.rowcount
        conn.commit()
        
        if count > 0:
            logger.debug(f"Closed {count} dialogue(s) due to inactivity timeout ({timeout_minutes} min)")
        
        return count
    
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_active_dialogue(
    db_config: dict,
    session_id: str,
    actor_id: str,
    agent_version: str
) -> str:
    """
    Возвращает ID активного диалога или создаёт новый.
    
    ВАЖНО: Не проверяет таймаут! Таймауты обрабатываются оркестратором.
    Если оркестратор закрыл диалог, здесь просто создастся новый.
    
    Логика:
    1. Ищет активный диалог для session_id + actor_id.
    2. Если найден → обновляет last_activity_at, возвращает ID.
    3. Если не найден → создаёт новый, возвращает ID.
    
    Returns:
        str: UUID активного диалога
    """
    conn = psycopg2.connect(**db_config)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    try:
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
            # Диалог активен (оркестратор гарантировал, что он не просрочен)
            dialogue_id = active_dialogue['id']
            cur.execute(
                "UPDATE dialogs.dialogues SET last_activity_at = %s WHERE id = %s",
                (now, dialogue_id)
            )
        else:
            # Активного диалога нет → создаём новый
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
            return count
    except Exception as e:
        logger.error(f"Error closing dangling dialogues: {e}", exc_info=True)
        conn.rollback()
        return 0
    finally:
        conn.close()


def _create_dialogue(cur, session_id: str, actor_id: str, agent_version: str) -> str:
    cur.execute("""
        INSERT INTO dialogs.dialogues (session_id, actor_id, agent_version)
        VALUES (%s, %s, %s)
        RETURNING id
    """, (session_id, actor_id, agent_version))
    new_id = str(cur.fetchone()['id'])
    logger.debug(f"New dialogue created: {new_id[:8]}")
    return new_id


def _close_dialogue(cur, dialogue_id: str, reason: str):
    cur.execute("""
        UPDATE dialogs.dialogues
        SET status = 'completed', reason = %s::dialog_close_reason, end_at = NOW()
        WHERE id = %s
    """, (reason, dialogue_id))