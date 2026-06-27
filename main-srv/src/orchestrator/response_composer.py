"""
main-srv/src/orchestrator/response_composer.py

Module for generating the final response to the user.

Logic:
1. Get the user's message from the database
2. Get the prompt and generation parameters from orchestrator.prompts.params
3. Load settings from state.settings:
   - use_momentary_state_in_generation: whether to substitute {{my_state}}
   - use_affective_gen_params: whether to use parameters from affective analysis
4. If use_momentary_state_in_generation=1.0:
   - Read state.self_knowledge.content via momentary.state_id
   - Substitute into {{my_state}} placeholder
5. If use_affective_gen_params=1.0:
   - Read recommended_gen_params from latest affective_analyses
   - Merge over prompt parameters
6. Build dialogue history (last N messages)
7. Calculate token limits and truncate history if needed
8. Call ModelService.generate()
9. Save full artifacts to metrics.llm_artifacts
10. Save response to dialogs.row_messages (with PHS stamps)
11. Trigger PHS momentary shift 'agent_response' after successful save
12. Complete the orchestrator task/step

PHS Integration:
- Stamps agent responses with current baseline_id and momentary_id via get_current_phs_snapshot().
- Triggers momentary shift 'agent_response' via phs_cache after saving the response.
- Loads momentary state text from state.self_knowledge for {{my_state}} placeholder.
- Merges affective generation parameters over prompt defaults (affective has priority).
- Does NOT perform PHS calculations directly.
"""

__version__ = "1.3.0"
__description__ = "Module for generating the final response to the user"


import logging
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timezone
from typing import Optional, Dict, Any

# Локальные импорты проекта
from db_manager.db_manager import load_postgres_config
from model_service.model_service import ModelService
from services.tokens_counter import count_tokens_qwen
from services.service_metrics import (
    mark_task_running,
    create_orchestrator_step,
    complete_step_success,
    complete_step_error,
    complete_task_success,
    complete_task_error,
    save_llm_metrics,
    save_reasoning,
    set_step_reasoning_id,
    save_llm_artifacts,
)
from version import __version__ as agent_version

logger = logging.getLogger(__name__)


# =============================================================================
# === КОНСТАНТЫ (единый источник для max_tokens) ==============================
# =============================================================================
# Математика лимитов:
# - n_ctx сервера = 262144 токенов (из model_config.yaml)
# - DEFAULT_MAX_TOKENS = 65536 (лимит на генерацию: ответ + рассуждение вместе)
# - Доступно под контекст = n_ctx - max_tokens
# - ROUGH_CONTEXT_LIMIT_TOKENS = 90% от доступного (запас 10% на ошибку округления и overhead)
# - HISTORY_MESSAGE_LIMIT - Лимит сообщений истории для контекста
# =============================================================================
DEFAULT_MAX_TOKENS: int = 65536
CONTEXT_SAFETY_MARGIN_PERCENT: float = 0.9  # 10% запас
HISTORY_MESSAGE_LIMIT: int = 10


def _render_system_prompt(prompt_text: str, my_state_text: str = "Ничего не чувствую.") -> str:
    """
    Подставляет данные в системный промпт.
    
    Плейсхолдеры:
        {{my_state}}           →  Текстовое описание текущего состояния из self_knowledge
        {{knowledge_self}}      →  "Ничего не знаю о себе."
        {{knowledge_user}}      →  "Ничего не знаю о пользователе."
        {{knowledge_topic}}     →  "Не знаю, опираюсь на то что есть в диалоге."
    """
    replacements = {
        "{{my_state}}": my_state_text,
        "{{knowledge_self}}": "Ничего не знаю о себе.",
        "{{knowledge_user}}": "Ничего не знаю о пользователе.",
        "{{knowledge_topic}}": "Не знаю, опираюсь на то что есть в диалоге."
    }
    
    rendered = prompt_text
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered

def _build_history_context(db_config: dict, session_id: str, current_message_id: str) -> tuple[list[dict], list[str]]: 
    """
    Собирает историю сообщений сессии для контекста.
    Берёт последние N сообщений в хронологическом порядке.
    Возвращает кортеж: (список сообщений, список их ID).
    """
    with psycopg2.connect(**db_config) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id, actor_type, row_text
                FROM dialogs.row_messages
                WHERE session_id = %s
                  AND id != %s
                ORDER BY timestamp DESC
                LIMIT %s
            """, (session_id, current_message_id, HISTORY_MESSAGE_LIMIT))

            rows = cur.fetchall()
            # Разворачиваем в хронологическом порядке (от старых к новым
            rows.reverse()

            messages = []
            message_ids = []
            for row in rows:
                # В нашей схеме агент имеет тип 'system'
                role = "assistant" if row["actor_type"] == "system" else "user"
                messages.append({"role": role, "content": row["row_text"]})
                message_ids.append(str(row["id"]))  # ← сохраняем UUID как строку

            return messages, message_ids

def compose_final_response(task_id: str, input_data: Dict[str, Any]) -> None:
    """
    Генерирует финальный ответ пользователю.
    
    Логика:
    1. Читает промпт и параметры генерации из orchestrator.prompts
    2. Загружает настройки из state.settings:
       - use_momentary_state_in_generation: подставлять ли {{my_state}}
       - use_affective_gen_params: использовать ли параметры из анализа
    3. Если use_momentary_state_in_generation=1.0:
       - Читает state.self_knowledge.content через momentary.state_id
       - Подставляет в плейсхолдер {{my_state}}
    4. Если use_affective_gen_params=1.0:
       - Читает recommended_gen_params из последнего affective_analyses
       - Мерджит поверх промптовых параметров
    5. Строит историю диалога (последние N сообщений)
    6. Вызывает ModelService.generate()
    7. Сохраняет полные артефакты в metrics.llm_artifacts
    8. Сохраняет ответ в dialogs.row_messages
    9. Завершает задачу
    """
    db_config = load_postgres_config()
    mark_task_running(task_id)
    message_id = input_data.get("message_id")
    momentary_id = None

    if not message_id:
        error_msg = f"Missing message_id in task {task_id} input_data"
        logger.error(error_msg)
        complete_task_error(task_id, error_module="response_composer", error_message=error_msg)
        return

    # === 1. Загрузка исходного сообщения пользователя ===
    with psycopg2.connect(**db_config) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id, row_text, session_id, actor_id, timestamp
                FROM dialogs.row_messages
                WHERE id = %s
            """, (message_id,))
            msg = cur.fetchone()
            if not msg:
                error_msg = f"Message {message_id} not found in dialogs.row_messages"
                logger.error(error_msg)
                complete_task_error(task_id, error_module="response_composer", error_message=error_msg)
                return

            user_content = msg["row_text"]
            session_id = msg["session_id"]
            user_actor_id = msg["actor_id"]
            user_msg_timestamp = msg["timestamp"]

            logger.debug(f"Loaded user message {message_id[:8]} from session {session_id[:8]}")

    # === 1.1 Получаем ID активного диалога ===
    from dialog_services.dialogue_manager import ensure_active_dialogue

    dialogue_id = ensure_active_dialogue(
        db_config=db_config,
        session_id=session_id,
        actor_id=user_actor_id,
        agent_version=agent_version
    )
    logger.debug(f"Active dialogue ID: {dialogue_id[:8]}")

    # === 1.2 Получаем штампы ПГС ( РАНЬШЕ, чем нужны для подстановки состояния) ===
    from phs_service.phs_cache import get_current_phs_snapshot
    baseline_id, momentary_id = get_current_phs_snapshot(db_config, user_actor_id)
    logger.debug(f"PHS snapshot: baseline={baseline_id[:8] if baseline_id else 'None'}, momentary={momentary_id[:8] if momentary_id else 'None'}")
    
    # === 2. Загрузка промпта и параметров генерации ===
    with psycopg2.connect(**db_config) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Ищем актуальный системный промпт (testing или active)
            cur.execute("""
                SELECT id, text, params
                FROM orchestrator.prompts
                WHERE name = 'agent_core_identity'
                  AND status IN ('testing'::prompt_status, 'active'::prompt_status)
                ORDER BY created_at DESC
                LIMIT 1
            """)
            prompt = cur.fetchone()
            if not prompt:
                error_msg = "Prompt 'agent_core_identity' not found in database"
                logger.error(error_msg)
                complete_task_error(task_id, error_module="response_composer", error_message=error_msg)
                return

            prompt_id = prompt["id"]
            raw_system_prompt = prompt["text"]
            model_params = prompt["params"] or {}

    # === 2.1 Загружаем настройки из state.settings ===
        use_momentary_state = False
        use_affective_params = False
        
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value_float FROM state.settings WHERE param_name = %s",
                ("use_momentary_state_in_generation",)
            )
            row = cur.fetchone()
            use_momentary_state = bool(row and row[0] and float(row[0]) > 0.0)
            
            cur.execute(
                "SELECT value_float FROM state.settings WHERE param_name = %s",
                ("use_affective_gen_params",)
            )
            row = cur.fetchone()
            use_affective_params = bool(row and row[0] and float(row[0]) > 0.0)
        
        logger.info(
            f"Generation settings: use_momentary_state={use_momentary_state}, "
            f"use_affective_params={use_affective_params}"
        )
        
    # === 2.2 Подстановка состояния из self_knowledge ===
    my_state_text = "Ничего не чувствую."
    if use_momentary_state and momentary_id:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT sk.content
                FROM state.momentary m
                JOIN state.self_knowledge sk ON m.state_id = sk.id
                WHERE m.id = %s
            """, (momentary_id,))
            row = cur.fetchone()
            if row and row[0]:
                my_state_text = row[0]
                logger.debug(f"Loaded momentary state: {len(my_state_text)} chars")
        
    # === 2.3 Загрузка параметров из аффективного анализа ===
    affective_params = {}
    if use_affective_params:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT aa.recommended_gen_params
                FROM dialogs.row_messages rm
                JOIN state.affective_analyses aa ON rm.phs_affective_analysis_id = aa.id
                WHERE rm.id = %s
            """, (message_id,))
            row = cur.fetchone()
            if row and row.get('recommended_gen_params'):
                affective_params = dict(row['recommended_gen_params'])
                logger.info(f"Loaded affective gen_params: {affective_params}")

    # === 3 Подстановка плейсхолдеров в системный промпт ===
    system_prompt = _render_system_prompt(raw_system_prompt, my_state_text)
    logger.debug("System prompt rendered with momentary state")

    # === 4. Формирование контекста (только история) ===
    history_messages, history_message_ids = _build_history_context(db_config, session_id, message_id)

    # Формируем messages для LLM API
    messages = [
        {"role": "system", "content": system_prompt},
        *history_messages,
        {"role": "user", "content": user_content}
    ]

    # === 4.1 Расчёт лимитов токенов ===
    model = ModelService()

    model_name = model_params.get("model_name")
    if not model_name:
        error_msg = "Missing 'model_name' in prompt params"
        logger.error(error_msg)
        complete_task_error(task_id, error_module="response_composer", error_message=error_msg)
        return

    # Получаем n_ctx через роутер
    model_info = model.get_model_info(model_name)
    n_ctx = model_info.get("n_ctx", 32768)

    # Получаем max_tokens из промпта или используем дефолт
    max_tokens = model_params.get("max_tokens") or DEFAULT_MAX_TOKENS

    # Максимально допустимое количество токенов под контекст (с запасом)
    available_for_context = int((n_ctx - max_tokens) * CONTEXT_SAFETY_MARGIN_PERCENT)

    # Считаем токены
    system_tokens = count_tokens_qwen(system_prompt)
    history_tokens = sum(count_tokens_qwen(m["content"]) for m in history_messages)
    user_tokens = count_tokens_qwen(user_content)
    total_input_tokens = system_tokens + history_tokens + user_tokens

    # Проверка переполнения
    if total_input_tokens > available_for_context:
        logger.warning(
            "Input exceeds context limit: %d tokens (available: %d, n_ctx=%d, max_tokens=%d)",
            total_input_tokens, available_for_context, n_ctx, max_tokens
        )
        # Обрезаем историю до тех пор, пока не уложимся в лимит
        while history_messages and total_input_tokens > available_for_context:
            removed = history_messages.pop(0)  # Удаляем самое старое сообщение
            removed_tokens = count_tokens_qwen(removed["content"])
            total_input_tokens -= removed_tokens
        
        # Пересобираем messages
        messages = [
            {"role": "system", "content": system_prompt},
            *history_messages,
            {"role": "user", "content": user_content}
        ]
        logger.info("Context truncated: %d messages left, %d tokens", len(history_messages), total_input_tokens)

    # Обновляем total_input_tokens для шага
    total_input_tokens = (
        count_tokens_qwen(system_prompt) +
        sum(count_tokens_qwen(m["content"]) for m in history_messages) +
        count_tokens_qwen(user_content)
    )

    # === 5. Создание шага оркестратора ===
    step_input = {
        "message_id": message_id,
        "prompt_id": prompt_id,
        "token_count": total_input_tokens,
        "history_messages_count": len(history_messages),
        "history_message_ids": history_message_ids,
        "use_momentary_state": use_momentary_state,
        "use_affective_params": use_affective_params,
        "my_state_length": len(my_state_text),
    }
    step_id = create_orchestrator_step(
        task_id=task_id,
        step_number=1,
        step_type_name="user_answer_generation",
        input_data=step_input,
        baseline_id=baseline_id,      # ← PHS штамп
        momentary_id=momentary_id     # ← PHS штамп
    )

    # === 6. Вызов модели ===
    logger.debug(f"Calling ModelService.generate with {len(messages)} messages")

    # Извлекаем model_name из параметров промпта
    model_name = model_params.get("model_name")
    if not model_name:
        error_msg = "Missing 'model_name' in prompt params"
        logger.error(error_msg)
        complete_step_error(step_id, error_module="response_composer", error_message=error_msg)
        complete_task_error(task_id, error_module="response_composer", error_message=error_msg)
        return

    # Фильтруем остальные параметры (без model_name)
    safe_params = {
        k: v for k, v in model_params.items()
        if k in [
            "temperature", "top_p", "top_k", "min_p", "max_tokens",
            "presence_penalty", "repetition_penalty", "stop", "chat_template_kwargs"
        ]
    }

    # Мердж параметров из аффективного анализа поверх промптовых
    if affective_params:
        for key in ["temperature", "top_p", "top_k", "min_p", "presence_penalty", "repetition_penalty"]:
            if key in affective_params:
                safe_params[key] = affective_params[key]
        logger.info(f"Final params after affective merge: {safe_params}")

    model = ModelService()

    try:
        result = model.generate(
            messages=messages,
            model_name=model_name,  # ← ЯВНО ПЕРЕДАЁМ
            **safe_params
        )
    except Exception as e:
        logger.exception(f"ModelService generation failed: {e}")
        complete_step_error(step_id, error_module="ModelService", error_message=str(e))
        complete_task_error(task_id, error_module="response_composer", error_message=str(e))
        return

    if not result.get("success"):
        error_msg = result.get("error", "Unknown model generation error")
        logger.error(f"Model returned failure: {error_msg}")
        complete_step_error(step_id, error_module="response_composer", error_message=error_msg)
        complete_task_error(task_id, error_module="response_composer", error_message=error_msg)
        return

    # === 7. Обработка ответа модели ===
    if not result.get("success"):
        error = result.get("error", "Unknown model generation error")
        logger.error(f"Model generation failed: {error}")
        complete_step_error(step_id, error_module="ModelService", error_message=error)
        complete_task_error(task_id, error_module="response_composer", error_message=error)
        return
    
    # === 8. Извлечение ответа и рассуждения ===
    # llama-server с Qwen3.5 возвращает reasoning_content как отдельное поле
    clean_response: str = result.get("response", "") or result.get("content", "")
    reasoning_text: Optional[str] = result.get("reasoning_content") or result.get("reasoning")

    if not clean_response:
        clean_response = "[Empty response]"
        logger.warning("Generated response is empty")

    # === 8.1 Получаем штампы ПГС для ответа и рассуждений ===
    from phs_service.phs_cache import get_current_phs_snapshot
    baseline_id, momentary_id = get_current_phs_snapshot(db_config, user_actor_id)
    
    # === 9. Сохранение рассуждений (если есть) ===
    reasoning_id = None
    if reasoning_text and reasoning_text.strip():
        # Рассуждение штампуется тем же PHS-срезом, что и ответ
        reasoning_id = save_reasoning(
            orchestrator_step_id=step_id,
            content=reasoning_text.strip(),
            content_type="messages",
            baseline_id=baseline_id,      # ← PHS штамп (получен на шаге 12)
            momentary_id=momentary_id     # ← PHS штамп
        )
        if reasoning_id:
            set_step_reasoning_id(step_id, reasoning_id)
            logger.debug(f"Reasoning saved: {reasoning_id[:8]}")

    # === 10. Запись метрик LLM ===
    metrics = result.get("metrics", {})
    timings = metrics.get("timings", {})
    usage = metrics.get("usage", {})

    llm_metric_id = save_llm_metrics(
        orchestrator_step_id=step_id,
        prompt_id=prompt_id,
        host="main-srv",
        model=metrics.get("model", "unknown"),
        param=model_params,
        cache_n=timings.get("cache_n", 0),
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        total_tokens=usage.get("total_tokens", 0),
        host_nctx=metrics.get("host_nctx", 0),
        prompt_ms=timings.get("prompt_ms", 0.0),
        prompt_per_token_ms=timings.get("prompt_per_token_ms", 0.0),
        prompt_per_second=timings.get("prompt_per_second", 0.0),
        predicted_per_second=timings.get("predicted_per_second", 0.0),
        resp_time=timings.get("predicted_ms", 0.0) / 1000,
        net_latency=0.0,
        full_time=0.0,
        error_status=False
    )

    # === 10.1 Сохранение артефактов (полный промпт + ответ + параметры) ===
    save_llm_artifacts(
        llm_metric_id=llm_metric_id,
        orchestrator_step_id=step_id,
        messages=messages,
        raw_response=clean_response,
        final_params=safe_params
    )

    # === 11. Расчёт задержки ответа (answer_latency) ===
    answer_timestamp = datetime.now(timezone.utc)
    if user_msg_timestamp.tzinfo is None:
        user_msg_timestamp = user_msg_timestamp.replace(tzinfo=timezone.utc)
    answer_latency = (answer_timestamp - user_msg_timestamp).total_seconds()

    # === 12. Сохранение ответа в dialogs.row_messages ===
    try:
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM users.actors WHERE type = 'system'::actor_type LIMIT 1")
                sys_actor = cur.fetchone()
                if not sys_actor:
                    raise RuntimeError("System actor not found in users.actors")
                system_actor_id = sys_actor[0]

                cur.execute("""
                    INSERT INTO dialogs.row_messages (
                        parent_message_id,
                        actor_id,
                        actor_type,
                        responded_by_actor_id,
                        session_id,
                        dialogue_id,
                        row_text,
                        answer_latency,
                        orchestrator_step_id,
                        baseline_id,
                        momentary_id,
                        agent_version,
                        timestamp
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (
                    message_id,
                    system_actor_id,
                    "system",
                    user_actor_id,
                    session_id,
                    dialogue_id,
                    clean_response,
                    answer_latency,
                    step_id,
                    baseline_id,           # ← PHS штамп
                    momentary_id,          # ← PHS штамп
                    agent_version,
                    answer_timestamp
                ))
                response_id = str(cur.fetchone()[0])
                conn.commit()
                logger.info(f"Agent response saved: {response_id[:8]}, latency={answer_latency:.2f}s")

                # === PHS INTEGRATION: Применяем сдвиг momentary (agent_response) ===
                try:
                    from phs_service.phs_cache import get_momentary_manager
                    momentary_mgr = get_momentary_manager(db_config)
                    momentary_mgr.apply_dialogue_event_shift('agent_response', user_actor_id)
                except Exception as e:
                    # Ошибка в ПГС не должна ломать генерацию ответа
                    logger.warning(f"Failed to apply agent_response PHS shift: {e}")

    except Exception as e:
        logger.error(f"Failed to save response to DB: {e}", exc_info=True)
        complete_step_error(step_id, error_module="response_composer", error_message=str(e))
        complete_task_error(task_id, error_module="response_composer", error_message=str(e))
        return

    # === 13. Привязка метрик к шагу оркестратора ===
    # llm_metric_id хранится в orchestrator.orchestrator_steps, а не в сообщениях
    try:
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE orchestrator.orchestrator_steps SET llm_metric_id = %s WHERE id = %s", (llm_metric_id, step_id))
                conn.commit()
    except Exception as e:
        logger.warning(f"Failed to link llm_metric_id to step: {e}")

    # === 14. Завершение шага и задачи ===
    step_output = {
        "response_message_id": response_id,
        "llm_metric_id": llm_metric_id,
        "reasoning_id": reasoning_id
    }
    complete_step_success(step_id, output_data=step_output)
    complete_task_success(task_id, output_data=step_output)
    logger.info(f"Task {task_id[:8]} and step {step_id[:8]} completed successfully")