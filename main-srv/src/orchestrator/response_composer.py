"""
main-srv/src/orchestrator/response_composer.py

Модуль генерации финального ответа пользователю.

Логика:
1. Получить сообщение пользователя из БД
2. Получить промпт и параметры генерации из orchestrator.prompts.params
3. Вызвать ModelService.generate() с параметрами из промпта
4. Обработать ответ:
   - Извлечь <think>...</think> → сохранить в orchestrator.reasonings
   - Очистить ответ от <think> → сохранить в dialogs.messages
5. Записать метрики в metrics.llm_internal
6. Завершить задачу/шаг оркестратора
"""

__version__ = "1.0.0"
__description__ = "Генерация ответа через ModelService + сохранение в БД"

import logging
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from orchestrator.context_builder import build_context
from services.tokens_counter import count_tokens_qwen

# Единая версия проекта — как в main.py
from version import __version__ as kaya_version

# Локальные импорты
from db_manager.db_manager import load_postgres_config
from model_service.model_service import ModelService
from services.service_metrics import (
    create_orchestrator_step,
    complete_step_success,
    complete_step_error,
    complete_task_success,
    complete_task_error,
    save_llm_metrics,
    save_reasoning,
    set_step_reasoning_id,
)

logger = logging.getLogger(__name__)

# =============================================================================
# === КОНСТАНТЫ (единый источник для max_tokens) ==============================
# =============================================================================
# Почему 4096:
# - Стандартный дефолт для Qwen3/llama-server при отсутствии параметра в промпте
# - Баланс: достаточно для развёрнутых ответов, оставляет место под контекст
# - Используется ЕСЛИ max_tokens не указан в params промпта
#
# Математика лимитов:
# - n_ctx сервера = 8192 токенов (из model_config.yaml)
# - max_tokens = 4096 (лимит на генерацию: ответ + <think> вместе)
# - Доступно под контекст = 8192 - 4096 = 4096 токенов
# - ROUGH_CONTEXT_LIMIT_TOKENS = 3600 (90% от доступного, запас 10%)
# =============================================================================
DEFAULT_MAX_TOKENS: int = 4096

def compose_final_response(task_id: str, input_data: Dict[str, Any]) -> None:
    """
    Генерация финального ответа пользователю.
    
    Args:
        task_id (str): UUID задачи оркестратора
        input_data (dict): {"message_id": "<uuid>"}
    """
    db_config: Dict[str, Any] = load_postgres_config()
    message_id: Optional[str] = input_data.get("message_id")
    
    if not message_id:
        error = f"Отсутствует message_id в input_data задачи {task_id}"
        logger.error(error)
        complete_task_error(task_id, error_module="response_composer", error_message=error)
        return
    
    # === 1. Получаем исходное сообщение пользователя ===
    with psycopg2.connect(**db_config) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT row_text, session_id, room_id, actor_id
                FROM dialogs.messages
                WHERE id = %s
            """, (message_id,))
            msg = cur.fetchone()
            if not msg:
                error = f"Сообщение {message_id} не найдено"
                logger.error(error)
                complete_task_error(task_id, error_module="response_composer", error_message=error)
                return
            user_content: str = msg["row_text"]
            session_id: str = msg["session_id"]
            room_id: str = msg["room_id"]
            user_actor_id: str = msg["actor_id"]  # ← НОВОЕ: ID актора пользователя
    
    # === 2. Получаем промпт и параметры генерации ===
    with psycopg2.connect(**db_config) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id, text, params
                FROM orchestrator.prompts
                WHERE name = 'kaya_core_identity'
                  AND status = 'testing'::prompt_status
                ORDER BY created_at DESC
                LIMIT 1
            """)
            prompt = cur.fetchone()
            if not prompt:
                error = "Промпт 'kaya_core_identity' не найден"
                logger.error(error)
                complete_task_error(task_id, error_module="response_composer", error_message=error)
                return
            prompt_id: str = prompt["id"]
            system_prompt: str = prompt["text"]
            # Параметры из JSONB-поля промпта (не хардкод!)
            model_params: Dict[str, Any] = prompt["params"] or {}
        
    # === 3. Формируем messages для API ===
    # 3.1: Собираем контекст: история сессии + (потом) векторный поиск
    context_messages, context_message_ids = build_context(
        session_id=session_id,      # ← сессия
        room_id=room_id,
        user_actor_id=user_actor_id,  # ← НОВОЕ: вместо session_id
        current_message_id=message_id,
        current_message_text=user_content
    )
    
    # 3.2: Формируем messages
    messages = [
        {"role": "system", "content": system_prompt},
        *context_messages,
        {"role": "user", "content": user_content}
    ]
    
    # 3.3: Проверка на переполнение
    model_service = ModelService()
    n_ctx = model_service.host_nctx
    
    # Единое значение max_tokens для РАСЧЁТА и ВЫЗОВА модели
    max_tokens = model_params.get("max_tokens") or DEFAULT_MAX_TOKENS
    
    # Считаем токены всех компонентов
    system_tokens = count_tokens_qwen(system_prompt)
    context_tokens = sum(count_tokens_qwen(msg["content"]) for msg in context_messages)
    user_tokens = count_tokens_qwen(user_content)
    
    # <think> + ответ вместе укладываются в max_tokens
    # n_ctx = входной промпт + выход (max_tokens)
    available_for_context = n_ctx - max_tokens # 8192 - 4096 = 4096 пример
    total_input_tokens = system_tokens + context_tokens + user_tokens
    
    if total_input_tokens > available_for_context:
        logger.warning(
            "⚠️ Контекст превышает лимит: %d токенов (доступно: %d, n_ctx=%d, max_tokens=%d)",
            total_input_tokens, available_for_context, n_ctx, max_tokens
        )
        while context_messages and total_input_tokens > available_for_context:
            removed = context_messages.pop(0)
            context_message_ids.pop(0)
            removed_tokens = count_tokens_qwen(removed["content"])
            total_input_tokens -= removed_tokens
        
        messages = [
            {"role": "system", "content": system_prompt},
            *context_messages,
            {"role": "user", "content": user_content}
        ]
        logger.info("✅ Контекст обрезан: осталось %d сообщений, %d токенов", 
                len(context_messages), total_input_tokens)
    
    # 3.4: Создаём шаг ОДИН РАЗ с финальными данными
    step_input = {
        "message_id": message_id,
        "prompt_id": prompt_id,
        "user_content": user_content,
        "context_message_ids": context_message_ids
    }
    
    step_id = create_orchestrator_step(
        task_id=task_id,
        step_number=1,
        step_type_name="user_answer_generation",
        input_data=step_input
    )
    logger.info("✅ Шаг %s создан: контекст=%d сообщений, токенов=%d", 
               step_id[:8], len(context_message_ids), total_input_tokens)
    
    # 4. Вызов модели
    logger.debug("Вызов ModelService.generate: %d элементов в messages", len(messages))
    model = ModelService()
    result = model.generate(
        messages=messages,
        temperature=model_params.get("temperature"),
        top_p=model_params.get("top_p"),
        top_k=model_params.get("top_k"),
        min_p=model_params.get("min_p"),
        max_tokens=max_tokens,  # ← ИСПОЛЬЗУЕМ ТО ЖЕ ЗНАЧЕНИЕ, что в расчёте выше
        presence_penalty=model_params.get("presence_penalty"),
        stop=model_params.get("stop")
    )
    
    # === 5. Обработка результата ===
    if not result["success"]:
        error = result.get("error", "Неизвестная ошибка модели")
        logger.error(f"❌ Ошибка генерации: {error}")
        complete_step_error(step_id, error_module="ModelService", error_message=error)
        complete_task_error(task_id, error_module="response_composer", error_message=error)
        return
    
     # === 6. Извлекаем рассуждение и чистый ответ ===
    clean_response = result["response"]        # уже чистый, без <think>
    think_content = result.get("reasoning", "")  # ← берём из отдельного поля
    logger.debug("Ответ: %d симв., рассуждение: %d симв.",len(clean_response), len(think_content))

    # === 7. Сохраняем рассуждение в orchestrator.reasonings (если есть) ===
    reasoning_id = None
    if think_content:
        reasoning_id = save_reasoning(
            orchestrator_step_id=step_id,
            content=think_content,
            content_type="messages"
        )
        set_step_reasoning_id(step_id, reasoning_id)
        logger.debug("Рассуждение сохранено: %s", reasoning_id[:8] if reasoning_id else "N/A")
    
    # === 8. Сохраняем метрики LLM в metrics.llm_internal ===
    metrics: Dict[str, Any] = result["metrics"]
    llm_metric_id: str = save_llm_metrics(
        orchestrator_step_id=step_id,
        prompt_id=prompt_id,
        host="main-srv",  # можно вынести в конфиг
        model=metrics.get("model", ""),
        param=model_params,
        cache_n=metrics.get("timings", {}).get("cache_n", 0),
        prompt_tokens=metrics.get("usage", {}).get("prompt_tokens", 0),
        completion_tokens=metrics.get("usage", {}).get("completion_tokens", 0),
        total_tokens=metrics.get("usage", {}).get("total_tokens", 0),
        host_nctx=metrics.get("host_nctx", 0),
        prompt_ms=metrics.get("timings", {}).get("prompt_ms", 0.0),
        prompt_per_token_ms=metrics.get("timings", {}).get("prompt_per_token_ms", 0.0),
        prompt_per_second=metrics.get("timings", {}).get("prompt_per_second", 0.0),
        predicted_per_second=metrics.get("timings", {}).get("predicted_per_second", 0.0),
        resp_time=metrics.get("timings", {}).get("predicted_ms", 0.0) / 1000,
        net_latency=0.0,
        full_time=0.0,
        error_status=False
    )
    
    # === 9. Вычисляем answer_latency ===
    with psycopg2.connect(**db_config) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Получаем timestamp родительского сообщения
            cur.execute("""
                SELECT timestamp FROM dialogs.messages WHERE id = %s
            """, (message_id,))
            parent_row = cur.fetchone()
            if not parent_row:
                raise ValueError(f"Родительское сообщение {message_id} не найдено")
                
            parent_timestamp = parent_row['timestamp']  # TIMESTAMPTZ
            answer_timestamp = datetime.now(timezone.utc)
            answer_latency = (answer_timestamp - parent_timestamp).total_seconds()

    # === 10. Сохраняем ЧИСТЫЙ ответ в dialogs.messages ===
    try:
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor() as cur:
                # Получаем ID актора системы (Кая)
                cur.execute("""
                    SELECT id FROM users.actors WHERE type = 'system'::actor_type LIMIT 1
                """)
                system_actor = cur.fetchone()
                if not system_actor:
                    raise RuntimeError("Актор 'system' не найден")
                system_actor_id: str = system_actor[0]
                
                # Считаем токены чистого ответа (для статистики)
                token_count: int = count_tokens_qwen(clean_response)
                
                # Вставляем ответ с parent_message_id = сообщение пользователя
                cur.execute("""
                    INSERT INTO dialogs.messages (
                        parent_message_id,
                        actor_id,
                        actor_type,
                        session_id,
                        room_id,
                        row_text,
                        token_count,
                        answer_latency,
                        kaya_version,
                        timestamp,
                        orchestrator_step_id,
                        llm_metric_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (
                    message_id,              # ← parent = сообщение пользователя
                    system_actor_id,
                    "system",
                    session_id,
                    room_id,
                    clean_response,          # ← БЕЗ <think>, только чистый ответ
                    token_count,
                    answer_latency,
                    kaya_version,            # ← из version.py, не хардкод
                    datetime.now(timezone.utc),
                    step_id,
                    llm_metric_id
                ))
                conn.commit()
                logger.info(
                    "✅ Ответ сохранён: parent=%s..., чистый=%d симв., токены=%d",
                    message_id[:8], len(clean_response), token_count
                )
                
    except Exception as e:
        logger.error("❌ Ошибка сохранения ответа в БД: %s", e, exc_info=True)
        complete_step_error(step_id, error_module="response_composer", error_message=str(e))
        complete_task_error(task_id, error_module="response_composer", error_message=str(e))
        return
    
    # === 11. Завершаем шаг и задачу ===
    step_output: Dict[str, Any] = {
        "response": clean_response,
        "reasoning_id": reasoning_id,
        "llm_metric_id": llm_metric_id
    }
    complete_step_success(step_id, output_data=step_output)
    complete_task_success(task_id, output_data=step_output)
    logger.info("✅ Задача %s... завершена успешно", task_id[:8])