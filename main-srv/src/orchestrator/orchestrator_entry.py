"""
main-srv/src/orchestrator/orchestrator_entry.py

Входной интерфейс оркестратора.
Вызывается из session_manager после сохранения сообщения пользователя.
Изменения в версии 1.1.0:
- Теперь создаётся ДВЕ задачи:
  1. user_question_preprocessing (приоритет 0.7) — предразбор сообщения
  2. user_answer_generation (приоритет 0.8) — генерация ответа
"""

__version__ = "1.1.0"
__description__ = "Создание задач оркестратора в БД"

import logging
from pathlib import Path
import psycopg2
from psycopg2.extras import Json
from db_manager.db_manager import load_postgres_config

# Единая версия проекта — как в main.py
from version import __version__ as kaya_version

logger = logging.getLogger(__name__)


def on_user_message(message_id: str, task_type: str = "user_question_preprocessing"):
    """
    Создаёт задачу оркестратору на обработку сообщения пользователя.
    
    В версии 1.1.0:
    - По умолчанию создаётся задача ПРЕДРАЗБОРА (user_question_preprocessing)
    - Приоритет 0.7 (задачи генерации финального ответа = 0.8)
    
    Args:
        message_id (str): UUID сообщения в dialogs.messages
        task_type (str): Тип задачи 
            - "user_question_preprocessing" — предразбор (по умолчанию)
            - "user_answer_generation" — генерация ответа
    
    Raises:
        ValueError: Если message_id пустой
        RuntimeError: Если тип задачи не найден в БД
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
                raise RuntimeError(
                    f"Тип задачи '{task_type}' не найден в orchestrator.task_types"
                )
            task_type_id = row[0]
            
            # Определяем приоритет
            # Предразбор = 0.7, генерация ответа = 0.8
            priority = 0.7 if task_type == "user_question_preprocessing" else 0.8
            
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
                priority,
                "pending",
                kaya_version
            ))
            conn.commit()
            logger.info(
                f"✅ Задача {task_type} создана для message_id={message_id[:8]}... "
                f"(приоритет={priority})"
            )
            
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