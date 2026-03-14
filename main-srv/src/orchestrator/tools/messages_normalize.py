"""
main-srv/src/orchestrator/tools/messages_normalize.py
Модуль фоновой нормализации текста сообщений.
Логика:
- Получить задачу от оркестратора с ID пары сообщений (user + system)
- Извлечь raw-тексты из dialogs.messages (row_text)
- Вызвать ModelService.generate() с промптом нормализации
- Сохранить нормализованный текст в dialogs.messages.processed_text
- Записать метрики LLM, шаг оркестратора, время обработки
- Завершить задачу оркестратора с успехом/ошибкой

Требования:
- Применены миграции V001, V003
- Доступ к PostgreSQL через psycopg2
- ModelService доступен из model_service.model_service
- Метрики через services.service_metrics

Пример input_data задачи:
{
    "user_message_id": "<uuid>",
    "system_message_id": "<uuid>",
    "session_id": "<uuid>"
}
"""
__version__ = "1.1.0"
__description__ = "Фоновая нормализация текста сообщений диалога"

import logging
import psycopg2
from psycopg2.extras import RealDictCursor, Json
from datetime import datetime, timezone
from typing import Optional, Dict, Any
import json

from version import __version__ as kaya_version
from db_manager.db_manager import load_postgres_config
from model_service.model_service import ModelService
from services.service_metrics import (
    mark_task_running,
    complete_task_success,
    complete_task_error,
    create_orchestrator_step,
    complete_step_success,
    complete_step_error,
    save_llm_metrics,
)
from services.tokens_counter import count_tokens_qwen

logger = logging.getLogger(__name__)

# =============================================================================
# КОНСТАНТЫ
# =============================================================================
NORMALIZATION_TASK_PRIORITY: float = 0.5
NORMALIZATION_PROMPT_NAME: str = "messages_normalization"


def normalize_messages(task_id: str, input_data: Dict[str, Any]) -> None:
    """
    Основная функция нормализации пары сообщений (user + system).
    """
    db_config: Dict[str, Any] = load_postgres_config()

    # === 0. Помечаем задачу как выполняющуюся ===
    mark_task_running(task_id)
    logger.info("🔧 Задача нормализации %s помечена как running", task_id[:8])

    # === 1. Валидация входных данных ===
    user_message_id: Optional[str] = input_data.get("user_message_id")
    system_message_id: Optional[str] = input_data.get("system_message_id")
    session_id: Optional[str] = input_data.get("session_id")

    if not all([user_message_id, system_message_id, session_id]):
        error = "Отсутствуют обязательные поля в input_data (user_message_id, system_message_id, session_id)"
        logger.error(error)
        complete_task_error(task_id, error_module="messages_normalize", error_message=error)
        return

    # === 2. Получаем исходные сообщения из БД ===
    user_row_text: str = ""
    system_row_text: str = ""

    try:
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT row_text FROM dialogs.messages WHERE id = %s
                """, (user_message_id,))
                user_msg = cur.fetchone()
                if not user_msg:
                    error = f"Сообщение пользователя {user_message_id} не найдено в БД"
                    logger.error(error)
                    complete_task_error(task_id, error_module="messages_normalize", error_message=error)
                    return
                user_row_text = user_msg["row_text"]

                cur.execute("""
                    SELECT row_text FROM dialogs.messages WHERE id = %s
                """, (system_message_id,))
                system_msg = cur.fetchone()
                if not system_msg:
                    error = f"Сообщение системы {system_message_id} не найдено в БД"
                    logger.error(error)
                    complete_task_error(task_id, error_module="messages_normalize", error_message=error)
                    return
                system_row_text = system_msg["row_text"]

                logger.debug(
                    "📝 Сообщения для нормализации: user=%d симв., system=%d симв.",
                    len(user_row_text), len(system_row_text)
                )

    except Exception as e:
        error = f"Ошибка чтения сообщений из БД: {str(e)}"
        logger.error(error, exc_info=True)
        complete_task_error(task_id, error_module="messages_normalize", error_message=error)
        return

    # === 3. Получаем промпт нормализации из БД (active → testing, без версии) ===
    prompt_id: str = ""
    system_prompt: str = ""
    model_params: Dict[str, Any] = {}

    try:
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # 1. Пробуем получить промпт со статусом 'active'
                cur.execute("""
                    SELECT id, text, params
                    FROM orchestrator.prompts
                    WHERE name = %s AND status = 'active'::prompt_status
                    ORDER BY created_at DESC
                    LIMIT 1
                """, (NORMALIZATION_PROMPT_NAME,))
                prompt = cur.fetchone()

                # 2. Если нет активного — берём последний 'testing'
                if not prompt:
                    cur.execute("""
                        SELECT id, text, params
                        FROM orchestrator.prompts
                        WHERE name = %s AND status = 'testing'::prompt_status
                        ORDER BY created_at DESC
                        LIMIT 1
                    """, (NORMALIZATION_PROMPT_NAME,))
                    prompt = cur.fetchone()

                if not prompt:
                    error = f"Промпт '{NORMALIZATION_PROMPT_NAME}' не найден в orchestrator.prompts"
                    logger.error(error)
                    complete_task_error(task_id, error_module="messages_normalize", error_message=error)
                    return

                prompt_id = prompt["id"]
                system_prompt = prompt["text"]
                model_params = prompt["params"] or {}
                logger.debug("✅ Промпт нормализации найден: %s", prompt_id[:8])

    except Exception as e:
        error = f"Ошибка чтения промпта из БД: {str(e)}"
        logger.error(error, exc_info=True)
        complete_task_error(task_id, error_module="messages_normalize", error_message=error)
        return

    # === 4. Формируем input JSON для промпта ===
    input_json: Dict[str, str] = {
        "user_message": user_row_text,
        "system_message": system_row_text
    }
    full_prompt: str = system_prompt
    full_prompt = full_prompt.replace("{{input_json}}", json.dumps(input_json, ensure_ascii=False))

    logger.debug("✅ Промпт сформирован: %d симв.", len(full_prompt))
    logger.debug("📝 Full prompt:\n%s", full_prompt[:500])  # первые 500 симв.
    
    # === 4.1: Проверка на переполнение контекстного окна ===
    model_service = ModelService()
    n_ctx = model_service.host_nctx
    max_tokens = model_params.get("max_tokens")
    system_tokens = count_tokens_qwen(full_prompt)
    user_tokens = count_tokens_qwen("Выполни нормализацию согласно инструкциям выше.")
    total_input_tokens = system_tokens + user_tokens
    available_for_input = n_ctx - max_tokens

    if total_input_tokens > available_for_input:
        logger.warning(
            "⚠️ Промпт нормализации превышает лимит: %d токенов (доступно: %d)",
            total_input_tokens, available_for_input
        )

    # === 5. Создаём шаг оркестратора (только ID, без текстов) ===
    step_input: Dict[str, Any] = {
        "user_message_id": user_message_id,
        "system_message_id": system_message_id,
        "session_id": session_id,
        "prompt_id": prompt_id
    }

    step_id: str = create_orchestrator_step(
        task_id=task_id,
        step_number=1,
        step_type_name="messages_normalization",
        input_data=step_input
    )
    logger.info("✅ Шаг нормализации %s создан", step_id[:8])

    # === 6. Вызов модели ===
    messages = [
        {"role": "system", "content": full_prompt},
        {"role": "user", "content": "Выполни нормализацию согласно инструкциям выше."}
    ]

    result = model_service.generate(
        messages=messages,
        temperature=model_params.get("temperature"),
        top_p=model_params.get("top_p"),
        top_k=model_params.get("top_k"),
        min_p=model_params.get("min_p"),
        max_tokens=model_params.get("max_tokens"),
        presence_penalty=model_params.get("presence_penalty"),
        stop=model_params.get("stop")
    )

    # === 7. Обработка результата ===
    if not result.get("success", False):
        error = result.get("error", "Неизвестная ошибка модели при нормализации")
        logger.error("❌ Ошибка нормализации: %s", error)
        complete_step_error(step_id, error_module="ModelService", error_message=error)
        complete_task_error(task_id, error_module="messages_normalize", error_message=error)
        return

    normalized_response: str = result.get("response", "")
    logger.debug("📝 Ответ модели: %d симв.", len(normalized_response))

    # === 8. Парсим JSON ответа ===
    normalized_user: str = user_row_text
    normalized_system: str = system_row_text

    try:
        json_start = normalized_response.find("{")
        json_end = normalized_response.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            json_str = normalized_response[json_start:json_end]
            parsed_json: Dict[str, str] = json.loads(json_str)
            normalized_user = parsed_json.get("user_message", user_row_text)
            normalized_system = parsed_json.get("system_message", system_row_text)
            logger.info("✅ JSON распарсен успешно")
        else:
            logger.warning("⚠️ JSON не найден в ответе, используем оригинальный текст")
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("⚠️ Ошибка парсинга JSON ответа: %s. Используем оригинальный текст.", str(e))

    # === 9. Сохраняем метрики LLM (БЕЗ reasoning — no_think промпт) ===
    metrics: Dict[str, Any] = result.get("metrics", {})

    llm_metric_id: str = save_llm_metrics(
        orchestrator_step_id=step_id,
        prompt_id=prompt_id,
        host="main-srv",
        model=metrics.get("model", "Qwen3-8B"),
        param=model_params,
        cache_n=metrics.get("timings", {}).get("cache_n", 0),
        prompt_tokens=metrics.get("usage", {}).get("prompt_tokens", 0),
        completion_tokens=metrics.get("usage", {}).get("completion_tokens", 0),
        total_tokens=metrics.get("usage", {}).get("total_tokens", 0),
        host_nctx=metrics.get("host_nctx", 8192),
        prompt_ms=metrics.get("timings", {}).get("prompt_ms", 0.0),
        prompt_per_token_ms=metrics.get("timings", {}).get("prompt_per_token_ms", 0.0),
        prompt_per_second=metrics.get("timings", {}).get("prompt_per_second", 0.0),
        predicted_per_second=metrics.get("timings", {}).get("predicted_per_second", 0.0),
        resp_time=metrics.get("timings", {}).get("predicted_ms", 0.0) / 1000,
        net_latency=0.0,
        full_time=0.0,
        error_status=False
    )
    logger.debug("📊 Метрики LLM сохранены: %s", llm_metric_id[:8])

    # === 10. Обновляем сообщения в БД (processed_text) ===
    processed_timestamp: datetime = datetime.now(timezone.utc)

    try:
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE dialogs.messages
                    SET processed_text = %s,
                        processed_orch_step_id = %s,
                        processed_llm_metric_id = %s,
                        processed_timestamp = %s
                    WHERE id = %s
                """, (normalized_user, step_id, llm_metric_id, processed_timestamp, user_message_id))

                cur.execute("""
                    UPDATE dialogs.messages
                    SET processed_text = %s,
                        processed_orch_step_id = %s,
                        processed_llm_metric_id = %s,
                        processed_timestamp = %s
                    WHERE id = %s
                """, (normalized_system, step_id, llm_metric_id, processed_timestamp, system_message_id))

                conn.commit()
                logger.info(
                    "✅ Нормализация завершена: user=%s..., system=%s...",
                    user_message_id[:8], system_message_id[:8]
                )

    except Exception as e:
        error = f"Ошибка сохранения нормализованного текста в БД: {str(e)}"
        logger.error(error, exc_info=True)
        complete_step_error(step_id, error_module="messages_normalize", error_message=error)
        complete_task_error(task_id, error_module="messages_normalize", error_message=error)
        return

    # === 11. Завершаем шаг и задачу ===
    step_output: Dict[str, Any] = {
        "user_message_id": user_message_id,
        "system_message_id": system_message_id,
        "normalized_user_length": len(normalized_user),
        "normalized_system_length": len(normalized_system),
        "llm_metric_id": llm_metric_id
    }

    complete_step_success(step_id, output_data=step_output)
    complete_task_success(task_id, output_data=step_output)
    logger.info("✅ Задача нормализации %s завершена успешно", task_id[:8])


def create_normalization_task(
    user_message_id: str,
    system_message_id: str,
    session_id: str,
    priority: float = NORMALIZATION_TASK_PRIORITY
) -> Optional[str]:
    """Создаёт задачу оркестратора на нормализацию сообщений. Только ID, без текстов."""
    db_config: Dict[str, Any] = load_postgres_config()

    try:
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id FROM orchestrator.task_types 
                    WHERE type_name = 'messages_normalization'
                """)
                task_type = cur.fetchone()
                if not task_type:
                    logger.error("Тип задачи 'messages_normalization' не найден в БД")
                    return None

                task_type_id: str = task_type["id"]
                input_data: Dict[str, str] = {
                    "user_message_id": user_message_id,
                    "system_message_id": system_message_id,
                    "session_id": session_id
                }

                cur.execute("""
                    INSERT INTO orchestrator.orchestrator_tasks (
                        task_type_id, input_data, priority, status, kaya_version, created_at
                    ) VALUES (%s, %s, %s, 'pending'::task_status, %s, NOW())
                    RETURNING id
                """, (task_type_id, Json(input_data), priority, kaya_version))

                conn.commit()
                task_id: str = cur.fetchone()["id"]
                logger.info("📋 Задача нормализации создана: %s (приоритет=%s)", task_id[:8], priority)
                return task_id

    except Exception as e:
        logger.error("❌ Ошибка создания задачи нормализации: %s", str(e), exc_info=True)
        return None