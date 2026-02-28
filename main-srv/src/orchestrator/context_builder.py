"""
main-srv/src/orchestrator/context_builder.py

Модуль сбора контекста для генерации ответа.

Отвечает за:
- Загрузка истории сообщений из ТЕКУЩЕЙ комнаты пользователя
- (Потом) Поиск релевантных сообщений через векторные эмбендинги (Qdrant)
- Грубая оценка размера контекста в токенах (точный подсчёт — в response_composer)

Схема БД: миграция V001
Таблицы: dialogs.messages
"""

__version__ = "1.0.0"
__description__ = "Сбор контекста диалога для генерации ответа"

import logging
import psycopg2
from psycopg2.extras import RealDictCursor
from typing import List, Dict, Any, Optional, Tuple

from db_manager.db_manager import load_postgres_config
from services.tokens_counter import count_tokens_qwen

logger = logging.getLogger(__name__)

# =============================================================================
# === КОНСТАНТЫ НАСТРОЕК (изменять здесь) =====================================
# =============================================================================

# Сколько последних сообщений из сессии подтягивать в контекст
# Это сообщения ДО текущего вопроса пользователя (не включая его)
CONTEXT_MESSAGES_COUNT: int = 7  # ← МЕНЯТЬ ЗДЕСЬ (сейчас: 7 сообщений, лучше ставить нечетное число для логики ответа)

# Грубый лимит для истории диалога (токены)
# Почему 3600:
# - n_ctx сервера = 8192 токенов
# - DEFAULT_MAX_TOKENS = 4096 (лимит на ответ + <think>)
# - Доступно под контекст = 8192 - 4096 = 4096 токенов
# - 3600 = 90% от доступного (запас 10% на system_prompt + погрешность)
ROUGH_CONTEXT_LIMIT_TOKENS: int = 3600  # должно коррелироваться с response_composer DEFAULT_MAX_TOKENS в пределах n_ctx = серверная


# =============================================================================
# === БЛОК 1: ИСТОРИЯ СООБЩЕНИЙ ПО СЕССИИ (session_id) ========================
# =============================================================================
# Примечание: Берём историю из ТЕКУЩЕЙ СЕССИИ пользователя, а не просто комнаты.
# Это гарантирует, что у 5 онлайн-юзеров в "open_dialogue" не смешается контекст.
# В будущем здесь появится блок "векторный поиск по эмбендингам" для поиска
# релевантных сообщений из ДРУГИХ сессий/комнат.


def get_room_message_history_for_user(
    room_id: str,
    user_actor_id: str,
    current_message_id: str,
    limit: int = CONTEXT_MESSAGES_COUNT  # = 7
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Загружает ровно N сообщений ВСЕГО (user + system вместе).
    """
    db_config = load_postgres_config()
    messages: List[Dict[str, Any]] = []
    message_ids: List[str] = []
    
    try:
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # CTE: находим ВСЕ релевантные сообщения (user + system ответы ему)
                # Затем: берём последние N из них (LIMIT на финальном результате!)
                cur.execute("""
                    WITH user_msg_ids AS (
                        -- Все сообщения этого пользователя в комнате (кроме текущего)
                        SELECT id FROM dialogs.messages 
                        WHERE room_id = %s 
                          AND actor_id = %s 
                          AND id != %s
                    )
                    SELECT 
                        m.id, m.actor_type, m.row_text, m.timestamp
                    FROM dialogs.messages m
                    WHERE m.room_id = %s
                      AND (
                          m.actor_id = %s  -- сообщения пользователя
                          OR (
                              m.actor_type = 'system'  -- ответы системы
                              AND m.parent_message_id IN (SELECT id FROM user_msg_ids)
                          )
                      )
                    ORDER BY m.timestamp DESC
                    LIMIT %s  -- ← LIMIT НА ФИНАЛЬНОМ РЕЗУЛЬТАТЕ!
                """, (
                    room_id, user_actor_id, current_message_id,  # CTE params
                    room_id, user_actor_id,                        # Main query params
                    limit  # = 7 ВСЕГО
                ))
                
                rows = cur.fetchall()
                
                # Переворачиваем: старые → новые
                for row in reversed(rows):
                    role = "user" if row["actor_type"] in ("owner", "user") else "assistant"
                    messages.append({"role": role, "content": row["row_text"]})
                    message_ids.append(str(row["id"]))
                
                total_tokens = sum(count_tokens_qwen(msg["content"]) for msg in messages)
                logger.debug("Загружено %d сообщений из комнаты %s для user %s (%d токенов)",
                           len(messages), room_id[:8], user_actor_id[:8], total_tokens)
                
    except Exception as e:
        logger.error("Ошибка загрузки истории: %s", e, exc_info=True)
    
    return messages, message_ids


# =============================================================================
# === БЛОК 2: ВЕКТОРНЫЙ ПОИСК (будет добавлен позже) ===========================
# =============================================================================
# Примечание: Здесь будет функция get_relevant_messages_by_embedding()
# для поиска релевантных сообщений через Qdrant/PgVector.
# Пока заглушка — вернуть пустой список.


def get_relevant_messages_by_embedding(
    current_message: str,
    session_id: Optional[str] = None,
    limit: int = 3
) -> List[Dict[str, Any]]:
    """
    (Заглушка) Поиск релевантных сообщений через векторные эмбендинги.
    
    Будет реализовано в следующей итерации с интеграцией Qdrant.
    """
    logger.debug("Векторный поиск пока не реализован — возвращаем пустой список")
    return []


# =============================================================================
# === ОСНОВНАЯ ФУНКЦИЯ: Сбор полного контекста =================================
# =============================================================================

def build_context(
    room_id: str,
    user_actor_id: str,
    current_message_id: str,
    current_message_text: str
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Собирает полный контекст для генерации ответа.
    
    Возвращает:
    1. Список сообщений в формате OpenAI messages
    2. Список UUID этих сообщений (для отладки в orchestrator_steps.input_data)
    
    Порядок:
    1. История сообщений из ТЕКУЩЕЙ КОМНАТЫ пользователя (room_id)
    2. (Потом) Релевантные сообщения из векторного поиска
    
    ВАЖНО: Точный подсчёт токенов и контроль n_ctx — в response_composer.py!
    
    Args:
        room_id (str): UUID текущей комнаты
        user_actor_id (str): UUID пользователя (для фильтрации его сообщений)
        current_message_id (str): UUID текущего сообщения (исключается из истории)
        current_message_text (str): Текст текущего вопроса (для векторного поиска)
    
    Returns:
        tuple: (context_messages, context_message_ids)
    """
    context_messages: List[Dict[str, Any]] = []
    context_message_ids: List[str] = []
    
    # 1. История из комнаты для пользователя (РОВНО CONTEXT_MESSAGES_COUNT)
    room_history, room_ids = get_room_message_history_for_user(
        room_id=room_id,
        user_actor_id=user_actor_id,
        current_message_id=current_message_id,
        limit=CONTEXT_MESSAGES_COUNT  # = 7
    )
    context_messages.extend(room_history)
    context_message_ids.extend(room_ids)
    
    # 2. Векторный поиск (заглушка)
    relevant = get_relevant_messages_by_embedding(current_message_text, room_id)
    context_messages.extend(relevant)
    
    logger.debug("Контекст: %d сообщений (комната: %d, векторы: %d)",
                len(context_messages), len(room_history), len(relevant))
    
    return context_messages, context_message_ids