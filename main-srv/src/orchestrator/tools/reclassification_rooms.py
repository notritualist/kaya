"""
main-srv/src/orchestrator/tools/reclassification_rooms.py
Фоновая реклассификация сообщений по комнатам диалога.
Логика:
- Получить пару сообщений (user + system) по ID из задачи оркестратора
- Получить все активные комнаты из dialogs.rooms (name + description)
- Получить текущую комнату из сообщений (физическая room_id)
- Сформировать промпт с описаниями комнат и текущей комнатой
- Вызвать модель для классификации пары сообщений
- Сохранить результат в dialogs.messages.effective_room_id
- Записать историю реклассификации в dialogs.messages_rooms_reclassifications
- Записать метрики LLM и завершить задачу

Требования:
- Промпт 'room_reclassification' должен существовать в orchestrator.prompts
- Используется та же модель что и для генерации ответа (Qwen3-8B)

Пример input_data задачи:
{
    "user_message_id": "<uuid>",
    "system_message_id": "<uuid>",
    "session_id": "<uuid>"
}
"""
__version__ = "1.1.0"
__description__ = "Фоновая реклассификация сообщений по комнатам диалога"

import logging
import psycopg2
from psycopg2.extras import RealDictCursor, Json
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
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
RECLASSIFICATION_TASK_PRIORITY: float = 0.2
RECLASSIFICATION_PROMPT_NAME: str = "room_reclassification"


def reclassify_message_room(task_id: str, input_data: Dict[str, Any]) -> None:
    """
    Основная функция реклассификации пары сообщений (user + system) по комнатам диалога.
    """
    db_config: Dict[str, Any] = load_postgres_config()

    # === 0. Помечаем задачу как выполняющуюся ===
    mark_task_running(task_id)
    logger.info("🔍 Задача реклассификации %s помечена как running", task_id[:8])

    # === 1. Валидация входных данных ===
    user_message_id: Optional[str] = input_data.get("user_message_id")
    system_message_id: Optional[str] = input_data.get("system_message_id")
    session_id: Optional[str] = input_data.get("session_id")

    if not all([user_message_id, system_message_id, session_id]):
        error = "Отсутствуют обязательные поля в input_data (user_message_id, system_message_id, session_id)"
        logger.error(error)
        complete_task_error(task_id, error_module="reclassification_rooms", error_message=error)
        return

    # === 2. Получаем исходные сообщения и текущую комнату из БД ===
    user_row_text: str = ""
    system_row_text: str = ""
    current_room_id: Optional[str] = None
    current_room_name: str = "open_dialogue"

    try:
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT row_text, room_id FROM dialogs.messages WHERE id = %s
                """, (user_message_id,))
                user_msg = cur.fetchone()
                if not user_msg:
                    error = f"Сообщение пользователя {user_message_id} не найдено в БД"
                    logger.error(error)
                    complete_task_error(task_id, error_module="reclassification_rooms", error_message=error)
                    return
                user_row_text = user_msg["row_text"]
                current_room_id = user_msg["room_id"]

                if current_room_id:
                    cur.execute("""
                        SELECT name FROM dialogs.rooms WHERE id = %s
                    """, (current_room_id,))
                    room = cur.fetchone()
                    if room:
                        current_room_name = room["name"]

                cur.execute("""
                    SELECT row_text FROM dialogs.messages WHERE id = %s
                """, (system_message_id,))
                system_msg = cur.fetchone()
                if not system_msg:
                    error = f"Сообщение системы {system_message_id} не найдено в БД"
                    logger.error(error)
                    complete_task_error(task_id, error_module="reclassification_rooms", error_message=error)
                    return
                system_row_text = system_msg["row_text"]

                logger.debug(
                    "📝 Сообщения для реклассификации: user=%d симв., system=%d симв., текущая комната=%s",
                    len(user_row_text), len(system_row_text), current_room_name
                )

    except Exception as e:
        error = f"Ошибка чтения сообщений из БД: {str(e)}"
        logger.error(error, exc_info=True)
        complete_task_error(task_id, error_module="reclassification_rooms", error_message=error)
        return

    # === 3. Получаем все активные комнаты из БД ===
    rooms_list: List[Dict[str, str]] = []

    try:
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT name, description FROM dialogs.rooms 
                    WHERE status = 'used'::room_status
                    ORDER BY name
                """)
                rooms = cur.fetchall()
                for room in rooms:
                    rooms_list.append({
                        "name": room["name"],
                        "description": room["description"] or ""
                    })
                if not rooms_list:
                    error = "Не найдено активных комнат для реклассификации"
                    logger.error(error)
                    complete_task_error(task_id, error_module="reclassification_rooms", error_message=error)
                    return
                logger.debug("✅ Найдено %d активных комнат для реклассификации", len(rooms_list))

    except Exception as e:
        error = f"Ошибка чтения комнат из БД: {str(e)}"
        logger.error(error, exc_info=True)
        complete_task_error(task_id, error_module="reclassification_rooms", error_message=error)
        return

    # === 4. Формируем описание комнат для промпта ===
    rooms_descriptions: str = ""
    for room in rooms_list:
        rooms_descriptions += f"- {room['name']}: {room['description']}\n"

    # === 5. Получаем промпт реклассификации из БД (active → testing, без версии) ===
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
                """, (RECLASSIFICATION_PROMPT_NAME,))
                prompt = cur.fetchone()

                # 2. Если нет активного — берём последний 'testing'
                if not prompt:
                    cur.execute("""
                        SELECT id, text, params
                        FROM orchestrator.prompts
                        WHERE name = %s AND status = 'testing'::prompt_status
                        ORDER BY created_at DESC
                        LIMIT 1
                    """, (RECLASSIFICATION_PROMPT_NAME,))
                    prompt = cur.fetchone()

                if not prompt:
                    error = f"Промпт '{RECLASSIFICATION_PROMPT_NAME}' не найден в orchestrator.prompts"
                    logger.error(error)
                    complete_task_error(task_id, error_module="reclassification_rooms", error_message=error)
                    return

                prompt_id = prompt["id"]
                system_prompt = prompt["text"]
                model_params = prompt["params"] or {}
                logger.debug("✅ Промпт реклассификации найден: %s", prompt_id[:8])

    except Exception as e:
        error = f"Ошибка чтения промпта из БД: {str(e)}"
        logger.error(error, exc_info=True)
        complete_task_error(task_id, error_module="reclassification_rooms", error_message=error)
        return

    # === 6. Формируем полный промпт с подстановкой данных ===
    full_prompt: str = system_prompt
    full_prompt = full_prompt.replace("{{current_room_name}}", current_room_name)
    full_prompt = full_prompt.replace("{{rooms_descriptions}}", rooms_descriptions)
    full_prompt = full_prompt.replace("{{user_message}}", user_row_text)
    full_prompt = full_prompt.replace("{{system_message}}", system_row_text)

    # === 6.1: Проверка на переполнение контекстного окна ===
    model_service = ModelService()
    n_ctx = model_service.host_nctx
    max_tokens = model_params.get("max_tokens")
    system_tokens = count_tokens_qwen(full_prompt)
    user_tokens = count_tokens_qwen("Определи комнату диалога для этой пары сообщений.")
    total_input_tokens = system_tokens + user_tokens
    available_for_input = n_ctx - max_tokens

    if total_input_tokens > available_for_input:
        logger.warning(
            "⚠️ Промпт реклассификации превышает лимит: %d токенов (доступно: %d)",
            total_input_tokens, available_for_input
        )

    # === 7. Создаём шаг оркестратора (только ID, без текстов) ===
    step_input: Dict[str, Any] = {
        "user_message_id": user_message_id,
        "system_message_id": system_message_id,
        "session_id": session_id,
        "current_room_name": current_room_name,
        "rooms_count": len(rooms_list),
        "prompt_id": prompt_id
    }

    step_id: str = create_orchestrator_step(
        task_id=task_id,
        step_number=1,
        step_type_name="message_room_reclassification",
        input_data=step_input
    )
    logger.info("✅ Шаг реклассификации %s создан", step_id[:8])

    # === 8. Вызов модели ===
    messages = [
        {"role": "system", "content": full_prompt},
        {"role": "user", "content": "Определи комнату диалога для этой пары сообщений."}
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

    # === 9. Обработка результата ===
    if not result.get("success", False):
        error = result.get("error", "Неизвестная ошибка модели при реклассификации")
        logger.error("❌ Ошибка реклассификации: %s", error)
        complete_step_error(step_id, error_module="ModelService", error_message=error)
        complete_task_error(task_id, error_module="reclassification_rooms", error_message=error)
        return

    classification_response: str = result.get("response", "")
    logger.debug("📝 Ответ модели: %d симв.", len(classification_response))

    # === 10. Парсим JSON ответа ===
    selected_room: str = current_room_name
    confidence: float = 0.0

    try:
        json_start = classification_response.find("{")
        json_end = classification_response.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            json_str = classification_response[json_start:json_end]
            parsed_json: Dict[str, Any] = json.loads(json_str)
            selected_room = parsed_json.get("selected_room", current_room_name)
            confidence = float(parsed_json.get("confidence", 0.0))
            logger.info("✅ JSON распарсен успешно: комната=%s, уверенность=%.2f", selected_room, confidence)
        else:
            logger.warning("⚠️ JSON не найден в ответе, используем текущую комнату")
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("⚠️ Ошибка парсинга JSON ответа: %s. Используем текущую комнату.", str(e))

    # === 11. Получаем ID выбранной комнаты из БД ===
    selected_room_id: Optional[str] = None

    try:
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id FROM dialogs.rooms WHERE name = %s AND status = 'used'::room_status
                """, (selected_room,))
                room = cur.fetchone()
                if room:
                    selected_room_id = room["id"]
                else:
                    cur.execute("""
                        SELECT id FROM dialogs.rooms WHERE name = 'open_dialogue' AND status = 'used'::room_status
                    """)
                    room = cur.fetchone()
                    if room:
                        selected_room_id = room["id"]
                        selected_room = "open_dialogue"
                        logger.warning("⚠️ Выбранная комната '%s' не найдена, используем 'open_dialogue'", selected_room)
                    else:
                        error = "Комната 'open_dialogue' не найдена в БД"
                        logger.error(error)
                        complete_step_error(step_id, error_module="reclassification_rooms", error_message=error)
                        complete_task_error(task_id, error_module="reclassification_rooms", error_message=error)
                        return

    except Exception as e:
        error = f"Ошибка получения ID комнаты из БД: {str(e)}"
        logger.error(error, exc_info=True)
        complete_step_error(step_id, error_module="reclassification_rooms", error_message=error)
        complete_task_error(task_id, error_module="reclassification_rooms", error_message=error)
        return

    # === 12. Сохраняем метрики LLM (БЕЗ reasoning — no_think промпт) ===
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

    # === 13. Обновляем effective_room_id в сообщениях ===
    reclassification_timestamp: datetime = datetime.now(timezone.utc)

    try:
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE dialogs.messages
                    SET effective_room_id = %s,
                        reclassification_step_id = %s,
                        effective_room_updated_at = %s
                    WHERE id = %s
                """, (selected_room_id, step_id, reclassification_timestamp, user_message_id))

                cur.execute("""
                    UPDATE dialogs.messages
                    SET effective_room_id = %s,
                        reclassification_step_id = %s,
                        effective_room_updated_at = %s
                    WHERE id = %s
                """, (selected_room_id, step_id, reclassification_timestamp, system_message_id))

                # Записываем историю реклассификации
                cur.execute("""
                    INSERT INTO dialogs.messages_rooms_reclassifications (
                        message_id, from_room_id, to_room_id, orchestrator_step_id,
                        agent_confidence, model_name, reclassification_type, kaya_version, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    user_message_id, current_room_id, selected_room_id, step_id,
                    confidence, metrics.get("model", "Qwen3-8B"), "internal_model", kaya_version, reclassification_timestamp
                ))

                cur.execute("""
                    INSERT INTO dialogs.messages_rooms_reclassifications (
                        message_id, from_room_id, to_room_id, orchestrator_step_id,
                        agent_confidence, model_name, reclassification_type, kaya_version, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    system_message_id, current_room_id, selected_room_id, step_id,
                    confidence, metrics.get("model", "Qwen3-8B"), "internal_model", kaya_version, reclassification_timestamp
                ))

                conn.commit()
                logger.info(
                    "✅ Реклассификация завершена: комната=%s (уверенность=%.2f), сообщения: user=%s..., system=%s...",
                    selected_room, confidence, user_message_id[:8], system_message_id[:8]
                )

    except Exception as e:
        error = f"Ошибка сохранения результатов реклассификации в БД: {str(e)}"
        logger.error(error, exc_info=True)
        complete_step_error(step_id, error_module="reclassification_rooms", error_message=error)
        complete_task_error(task_id, error_module="reclassification_rooms", error_message=error)
        return

    # === 14. Завершаем шаг и задачу ===
    step_output: Dict[str, Any] = {
        "user_message_id": user_message_id,
        "system_message_id": system_message_id,
        "selected_room": selected_room,
        "selected_room_id": selected_room_id,
        "confidence": confidence,
        "llm_metric_id": llm_metric_id
    }

    complete_step_success(step_id, output_data=step_output)
    complete_task_success(task_id, output_data=step_output)
    logger.info("✅ Задача реклассификации %s завершена успешно", task_id[:8])


def create_reclassification_task(
    user_message_id: str,
    system_message_id: str,
    session_id: str,
    priority: float = RECLASSIFICATION_TASK_PRIORITY
) -> Optional[str]:
    """Создаёт задачу оркестратора на реклассификацию сообщений по комнатам. Только ID, без текстов."""
    db_config: Dict[str, Any] = load_postgres_config()

    try:
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id FROM orchestrator.task_types 
                    WHERE type_name = 'message_room_reclassification'
                """)
                task_type = cur.fetchone()
                if not task_type:
                    logger.error("Тип задачи 'message_room_reclassification' не найден в БД")
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
                logger.info("📋 Задача реклассификации создана: %s (приоритет=%s)", task_id[:8], priority)
                return task_id

    except Exception as e:
        logger.error("❌ Ошибка создания задачи реклассификации: %s", str(e), exc_info=True)
        return None