"""
main-srv/src/session_services/room_switch_manager.py
Модуль управления переключением комнат диалогов.

Назначение:
    Централизованное принятие решений о переключении между тематическими комнатами
    на основе анализа сообщений пользователя и текущего контекста сессии.

Архитектурная роль:
    Является связующим звеном между модулем предразбора сообщений и системой
    управления сессиями, реализуя бизнес-логику переключения контекстов диалога.

Основные функции:
    1. process_room_switch_decision() - главная функция принятия решения
    2. Принятие решения на основе confidence + веса комнаты + explicit_request
    4. Логирование переходов в dialogs.room_transitions
    5. Обновление dialogs.sessions.current_room

Интеграция с БД (миграция V002):
    - dialogs.sessions (current_room, pending_room_switch_message_count, last_room_switch_at)
    - dialogs.messages (effective_room_id)
    - dialogs.room_transitions (история переключений)
    - orchestrator.orchestrator_steps (ссылка на шаг предразбора)

Логика принятия решений:
    1. Если лучшая комната == текущая → оставляем
    2. Явный запрос + вес комнаты >= 80 → переключаем сразу
    3. Авто-переключение: confidence >= 0.85 + вес комнаты >= 80 → переключаем (если разрешено)
    4. Иначе → не переключаем
"""

version = "1.1.0"
description = "Менеджер переключения комнат диалогов"

# =============================================================================
# === КОНСТАНТЫ НАСТРОЕК ======================================================
# =============================================================================

# Порог уверенности для авто-переключения (0.0–1.0)
CONFIDENCE_THRESHOLD_AUTO_SWITCH: float = 0.9

# Минимальный вес комнаты для переключения (0–100) в том числе по запрсосу пользователя
MIN_ROOM_WEIGHT_THRESHOLD: int = 70

# Разрешить авто-переключение комнат (без явного запроса пользователя)
AUTO_SWITCH_ENABLED: bool = False  # False = только явные запросы, True = разрешить авто-переключение

# =============================================================================
# === ИМПОРТЫ =================================================================
# =============================================================================

import logging
import psycopg2
from psycopg2.extras import RealDictCursor, Json
from typing import Optional, Dict, Any, Tuple, List
from db_manager.db_manager import load_postgres_config
from version import __version__ as kaya_version

logger = logging.getLogger(__name__)


# =============================================================================
# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =================================================
# =============================================================================

def get_session_room_state(db_config: dict, session_id: str) -> Optional[Dict[str, Any]]:
    """Получает текущее состояние комнаты сессии."""
    try:
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT s.current_room, r.name AS current_room_name
                    FROM dialogs.sessions s
                    LEFT JOIN dialogs.rooms r ON r.id = s.current_room
                    WHERE s.id = %s
                """, (session_id,))
                row = cur.fetchone()
                if not row:
                    logger.error(f"Сессия {session_id} не найдена")
                    return None
                return {
                    "current_room_id": str(row["current_room"]) if row["current_room"] else None,
                    "current_room_name": row["current_room_name"],
                }
    except Exception as e:
        logger.error(f"Ошибка получения состояния сессии: {e}", exc_info=True)
        return None


def get_room_id_by_name(db_config: dict, room_name: str) -> Optional[str]:
    """Получает UUID комнаты по имени."""
    try:
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id FROM dialogs.rooms
                    WHERE name = %s AND status = 'used'::room_status
                """, (room_name,))
                row = cur.fetchone()
                if row:
                    logger.debug(f"✅ Комната '{room_name}' найдена: {row['id'][:8]}")
                    return str(row["id"])
                logger.warning(f"⚠️ Комната '{room_name}' не найдена")
                return None
    except Exception as e:
        logger.error(f"Ошибка получения комнаты: {e}", exc_info=True)
        return None


def get_rooms_descriptions(db_config: dict) -> str:
    """Получает описания всех комнат из БД для промпта."""
    try:
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT name, description FROM dialogs.rooms
                    WHERE status = 'used'::room_status
                    ORDER BY name
                """)
                rows = cur.fetchall()
                return "\n".join([f"- {r['name']}: {r['description']}" for r in rows])
    except Exception as e:
        logger.error(f"Ошибка получения описаний комнат: {e}", exc_info=True)
        return ""


def get_user_message_history(
    db_config: dict,
    session_id: str,
    user_actor_id: str,
    current_message_id: str,
    limit: int = 2
) -> List[str]:
    """Получает последние N сообщений пользователя для контекста."""
    try:
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT row_text FROM dialogs.messages
                    WHERE session_id = %s AND actor_id = %s AND id != %s
                    ORDER BY timestamp DESC LIMIT %s
                """, (session_id, user_actor_id, current_message_id, limit))
                rows = cur.fetchall()
                return [r["row_text"] for r in reversed(rows)]
    except Exception as e:
        logger.error(f"Ошибка получения истории: {e}", exc_info=True)
        return []


def save_room_transition(
    db_config: dict,
    session_id: str,
    triggering_message_id: str,
    from_room_id: Optional[str],
    to_room_id: str,
    trigger_type: str,
    confidence_score: float,
    model_weights: Dict[str, Any]
) -> Optional[str]:
    """Сохраняет запись о переключении в dialogs.room_transitions."""
    try:
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    INSERT INTO dialogs.room_transitions (
                        session_id, triggering_message_id, from_room_id, to_room_id,
                        trigger_type, confidence_score, model_weights, kaya_version, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    RETURNING id
                """, (
                    session_id, triggering_message_id, from_room_id, to_room_id,
                    trigger_type, confidence_score, Json(model_weights), kaya_version
                ))
                row = cur.fetchone()
                conn.commit()
                if row:
                    logger.info(f"✅ Переход сохранён: {row['id'][:8]}")
                    return str(row["id"])
                logger.error("Не удалось получить ID перехода")
                return None
    except Exception as e:
        logger.error(f"Ошибка сохранения перехода: {e}", exc_info=True)
        return None


def update_session_current_room(db_config: dict, session_id: str, new_room_id: str) -> bool:
    """Обновляет current_room в сессии."""
    try:
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE dialogs.sessions SET current_room = %s WHERE id = %s", (new_room_id, session_id))
                conn.commit()
                logger.info(f"✅ current_room сессии {session_id[:8]} обновлён")
                return True
    except Exception as e:
        logger.error(f"Ошибка обновления current_room: {e}", exc_info=True)
        return False


def _execute_switch(
    db_config: dict,
    message_id: str,
    session_id: str,
    current_room_id: Optional[str],
    current_room_name: str,
    new_room_name: str,
    trigger_type: str,
    confidence: float,
    model_weights: Dict[str, Any]
) -> Tuple[bool, Optional[str], str]:
    """Выполняет переключение комнаты."""
    new_room_id = get_room_id_by_name(db_config, new_room_name)
    if not new_room_id:
        logger.error(f"❌ Комната '{new_room_name}' не найдена")
        return False, None, current_room_name
    
    save_room_transition(
        db_config, session_id, message_id, current_room_id, new_room_id,
        trigger_type, confidence, model_weights
    )
    update_session_current_room(db_config, session_id, new_room_id)
    
    logger.info(f"✅ Переключение завершено: {current_room_name} → {new_room_name}")
    return True, new_room_id, new_room_name


# =============================================================================
# === ОСНОВНАЯ ФУНКЦИЯ ========================================================
# =============================================================================

def process_room_switch_decision(
    message_id: str,
    session_id: str,
    preprocess_result: Dict[str, Any],
    orchestrator_step_id: Optional[str] = None
) -> Tuple[bool, Optional[str], str]:
    """
    Главная функция принятия решения о переключении комнаты.
    
    Логика:
    1. best_room == current_room → оставляем
    2. explicit_request + вес >= 80 → переключаем сразу (работает всегда)
    3. !explicit_request + уверенность >= 0.85 + вес >= 80 → переключаем (только если AUTO_SWITCH_ENABLED=True)
    4. иначе → не переключаем
    """
    db_config = load_postgres_config()
    
    # === ШАГ 1: Состояние сессии ===
    session_state = get_session_room_state(db_config, session_id)
    if not session_state:
        logger.error(f"Не удалось получить состояние сессии {session_id}")
        return False, None, "unknown"
    
    current_room_id = session_state["current_room_id"]
    current_room_name = session_state["current_room_name"] or "unknown"
    
    # === ШАГ 2: Данные предразбора ===
    room_weights = preprocess_result.get("room_weights", {})
    confidence = preprocess_result.get("confidence", 0.0)
    explicit_request = preprocess_result.get("explicit_request", False)
    
    logger.info(f"📊 Предразбор: room_weights={room_weights}, confidence={confidence}, explicit_request={explicit_request}")
    
    # === ШАГ 3: Лучшая комната ===
    if not room_weights:
        logger.warning("⚠️ room_weights пуст")
        return False, None, current_room_name
    
    best_room_name = max(room_weights, key=room_weights.get)
    best_room_weight = room_weights[best_room_name]
    
    logger.debug(f"🏆 Лучшая комната: {best_room_name} (вес={best_room_weight})")
    
    # === ПРОВЕРКА 1: Та же комната → оставляем ===
    if best_room_name == current_room_name:
        logger.info(f"✅ Оставляем текущую комнату: {current_room_name}")
        return False, None, current_room_name
    
    # === ПРОВЕРКА 2: Явный запрос + вес >= порога → сразу (работает независимо от AUTO_SWITCH_ENABLED) ===
    if explicit_request and best_room_weight >= MIN_ROOM_WEIGHT_THRESHOLD:
        logger.info(f"🚀 ЯВНЫЙ ЗАПРОС: {current_room_name} → {best_room_name}")
        return _execute_switch(
            db_config, message_id, session_id, current_room_id, current_room_name,
            best_room_name, "explicit_user_request", confidence, room_weights
        )
    
    # === ПРОВЕРКА 3: Авто-переключение (только если разрешено константой) ===
    if AUTO_SWITCH_ENABLED and confidence >= CONFIDENCE_THRESHOLD_AUTO_SWITCH and best_room_weight >= MIN_ROOM_WEIGHT_THRESHOLD:
        logger.info(f"🚀 АВТО-ПЕРЕКЛЮЧЕНИЕ: {current_room_name} → {best_room_name}")
        return _execute_switch(
            db_config, message_id, session_id, current_room_id, current_room_name,
            best_room_name, "auto_high_confidence", confidence, room_weights
        )
    
    # === НЕ ПРОШЛИ → не переключаем ===
    logger.info(f"⛔ БЛОК: явный={explicit_request}, вес={best_room_weight}, conf={confidence}, auto_switch_enabled={AUTO_SWITCH_ENABLED}")
    return False, None, current_room_name


# =============================================================================
# === ВСПОМОГАТЕЛЬНАЯ: Для консоли ============================================
# =============================================================================

def get_current_room_name(db_config: dict, session_id: str) -> str:
    """Получает имя текущей комнаты для отображения."""
    try:
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT r.name FROM dialogs.sessions s
                    JOIN dialogs.rooms r ON r.id = s.current_room
                    WHERE s.id = %s
                """, (session_id,))
                row = cur.fetchone()
                return row["name"] if row and row["name"] else "unknown"
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        return "unknown"