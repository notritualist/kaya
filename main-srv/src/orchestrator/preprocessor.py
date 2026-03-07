"""
main-srv/src/orchestrator/preprocessor.py
Модуль предобработки сообщений пользователя.

Назначение:
- Анализирует входящее сообщение через LLM
- Определяет веса комнат диалогов (какая тема ближе)
- Определяет, хочет ли пользователь явно сменить комнату
- Сохраняет результаты в orchestrator.preprocessed_results
- Обновляет dialogs.messages.processed_text

Схема БД: миграции V001, V002
Таблицы: 
- orchestrator.prompts (промпт предразбора)
- orchestrator.orchestrator_steps (шаг обработки)
- orchestrator.preprocessed_results (результат предразбора)
- dialogs.messages (обновление processed_text)
- metrics.llm_internal (метрики LLM-запроса)
"""
version = "1.1.0"
description = "Предобработка сообщений пользователя для классификации комнат"

import logging
import json
from datetime import datetime, timezone
from typing import Optional, Dict, Any
import psycopg2
from psycopg2.extras import RealDictCursor

# === ЛОКАЛЬНЫЕ ИМПОРТЫ ПРОЕКТА ===
# Загружаем конфигурацию БД из центрального модуля
from db_manager.db_manager import load_postgres_config
# Сервис для вызова LLM-модели
from model_service.model_service import ModelService
# Утилиты для работы с метриками и статусами оркестратора
from services.service_metrics import (
    mark_task_running,          # Пометить задачу как выполняющуюся
    create_orchestrator_step,   # Создать шаг оркестратора
    complete_step_success,      # Завершить шаг успешно
    complete_step_error,        # Завершить шаг с ошибкой
    complete_task_success,      # Завершить задачу успешно
    complete_task_error,        # Завершить задачу с ошибкой
    save_llm_metrics,           # Сохранить метрики LLM-запроса
)

# Единая версия проекта — как в main.py
from version import __version__ as kaya_version

# === НАСТРОЙКА ЛОГГЕРА ===
# Логгер автоматически подхватит настройки из main.py (файл + консоль)
logger = logging.getLogger(__name__)

# =============================================================================
# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =================================================
# =============================================================================

def parse_room_weights_json(raw_text: str) -> Optional[Dict[str, Any]]:
    """
    Парсит JSON-ответ от модели с весами комнат.
    
    Args:
        raw_text (str): Сырой ответ от модели
        
    Returns:
        dict | None: Распарсенный JSON или None при ошибке
    """
    if not raw_text or not isinstance(raw_text, str):
        logger.warning("Пустой или некорректный ответ от модели")
        return None
    
    start_idx = raw_text.find('{')
    end_idx = raw_text.rfind('}')
    
    if start_idx == -1 or end_idx == -1 or start_idx >= end_idx:
        logger.error(f"Не найдены фигурные скобки в ответе: {raw_text[:100]}")
        return None
    
    json_str = raw_text[start_idx:end_idx + 1]
    
    try:
        parsed = json.loads(json_str)
        
        if not isinstance(parsed, dict):
            logger.error("JSON не является объектом")
            return None
        
        if 'room_weights' not in parsed:
            logger.error("Отсутствует поле 'room_weights' в JSON")
            return None
        
        if 'user_rejected' not in parsed:
            logger.error("Отсутствует поле 'user_rejected' в JSON")
            return None
        
        # === НОВОЕ: Проверяем confidence ===
        if 'confidence' not in parsed:
            logger.error("Отсутствует поле 'confidence' в JSON")
            return None
        
        # Валидируем диапазон confidence (0.0–1.0)
        confidence = parsed.get('confidence', 0.0)
        if not isinstance(confidence, (int, float)) or confidence < 0.0 or confidence > 1.0:
            logger.error(f"confidence должен быть числом от 0.0 до 1.0, получено: {confidence}")
            return None
        
        logger.debug("✅ JSON успешно распарсен")
        return parsed
        
    except json.JSONDecodeError as e:
        logger.error(f"❌ Ошибка парсинга JSON: {e}")
        return None


def get_preprocessing_prompt(db_config: dict) -> Optional[Dict[str, Any]]:
    """
    Получает активный промпт для предразбора вопросов.
    """
    try:
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, text, params, version
                    FROM orchestrator.prompts
                    WHERE name = 'preprocess_user_question'
                      AND status IN ('testing'::prompt_status, 'active'::prompt_status)
                    ORDER BY created_at DESC
                    LIMIT 1
                """)
                
                prompt = cur.fetchone()
                
                if not prompt:
                    logger.error("Промпт 'preprocess_user_question' не найден в БД")
                    return None
                
                logger.info(
                    f"✅ Промпт предразбора получен: id={prompt['id'][:8]}, "
                    f"версия={prompt['version']}"
                )
                return dict(prompt)
                
    except psycopg2.Error as e:
        logger.error(f"Ошибка БД при получении промпта: {e}", exc_info=True)
        return None
    except Exception as e:
        logger.error(f"Неожиданная ошибка: {e}", exc_info=True)
        return None
                
    except psycopg2.Error as e:
        logger.error(f"Ошибка БД при получении промпта: {e}", exc_info=True)
        return None
    except Exception as e:
        logger.error(f"Неожиданная ошибка: {e}", exc_info=True)
        return None


def get_user_message_data(db_config: dict, message_id: str) -> Optional[Dict[str, Any]]:
    """
    Получает данные сообщения пользователя из БД.
    
    Args:
        db_config (dict): Параметры подключения к PostgreSQL
        message_id (str): UUID сообщения в dialogs.messages
        
    Returns:
        dict | None: Данные сообщения или None
    """
    try:
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT 
                        m.id, m.row_text, m.session_id, m.room_id, m.actor_id,
                        s.current_room, s.actor_external_id
                    FROM dialogs.messages m
                    JOIN dialogs.sessions s ON s.id = m.session_id
                    WHERE m.id = %s
                """, (message_id,))
                
                msg_data = cur.fetchone()
                
                if not msg_data:
                    logger.error(f"Сообщение {message_id} не найдено")
                    return None
                
                logger.debug(
                    f"✅ Данные сообщения получены: "
                    f"session={msg_data['session_id'][:8]}, "
                    f"room={msg_data['room_id'][:8]}"
                )
                return dict(msg_data)
                
    except Exception as e:
        logger.error(f"Ошибка при получении сообщения: {e}", exc_info=True)
        return None


def save_preprocessed_result(
    db_config: dict,
    message_id: str,
    preprocessed_data: Dict[str, Any],
    llm_metric_id: str
) -> Optional[str]:
    """
    Сохраняет результат предразбора в orchestrator.preprocessed_results.
    
    Args:
        db_config (dict): Параметры подключения к PostgreSQL
        message_id (str): UUID исходного сообщения
        preprocessed_data (dict): Результат анализа (веса комнат, user_rejected)
        llm_metric_id (str): UUID метрик LLM-запроса
        
    Returns:
        str | None: UUID сохранённой записи или None
    """
    try:
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    INSERT INTO orchestrator.preprocessed_results (
                        message_id,
                        preprocessed_result,
                        llm_metric_id,
                        kaya_version,
                        timestamp
                    ) VALUES (%s, %s, %s, %s, %s)
                    RETURNING id
                """, (
                    message_id,
                    json.dumps(preprocessed_data),  # Сохраняем как JSONB
                    llm_metric_id,
                    kaya_version,
                    datetime.now(timezone.utc)
                ))
                
                result = cur.fetchone()
                conn.commit()
                
                if result:
                    logger.debug(
                        f"✅ Результат предразбора сохранён: {result['id'][:8]}"
                    )
                    return str(result['id'])
                else:
                    logger.error("Не удалось получить ID сохранённого результата")
                    return None
                    
    except Exception as e:
        logger.error(f"Ошибка сохранения результата предразбора: {e}", exc_info=True)
        return None


def update_message_processed_text(
    db_config: dict,
    message_id: str,
    processed_text: str,
    preprocess_result_id: str,
    llm_metric_id: str,
    orchestrator_step_id: str
) -> bool:
    """
    Обновляет сообщение пользователя: processed_text + ссылки на метрики.
    
    Args:
        db_config (dict): Параметры подключения к PostgreSQL
        message_id (str): UUID сообщения
        processed_text (str): Нормализованный текст (пока = row_text)
        preprocess_result_id (str): UUID результата предразбора
        llm_metric_id (str): UUID метрик LLM
        
    Returns:
        bool: True при успехе
    """
    try:
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE dialogs.messages
                    SET 
                        processed_text = %s,
                        preprocess_result_id = %s,
                        llm_metric_id = %s,
                        orchestrator_step_id = %s
                    WHERE id = %s
                """, (
                    processed_text,
                    preprocess_result_id,
                    llm_metric_id,
                    orchestrator_step_id,
                    message_id
                ))
                conn.commit()
                
                logger.debug(
                    f"✅ Сообщение {message_id[:8]} обновлено: "
                    f"processed_text + ссылки на метрики"
                )
                return True
                
    except Exception as e:
        logger.error(f"Ошибка обновления сообщения: {e}", exc_info=True)
        return False


# =============================================================================
# === ОСНОВНАЯ ФУНКЦИЯ ПРЕДОБРАБОТКИ ==========================================
# =============================================================================

def preprocess_user_message(task_id: str, input_data: Dict[str, Any]) -> None:
    """
    Главная функция предобработки сообщения пользователя.
    
    Пайплайн:
    1. Пометить задачу как running
    2. Получить message_id из input_data
    3. Прочитать сообщение и промпт из БД
    4. Сформировать запрос к LLM
    5. Вызвать ModelService.generate()
    6. Распарсить JSON с весами комнат
    7. Сохранить результат в preprocessed_results
    8. Обновить dialogs.messages (processed_text, метрики)
    9. Создать задачу генерации ответа (user_answer_generation)
    10. Завершить текущую задачу и шаг
    """
    db_config = load_postgres_config()
    message_id: Optional[str] = input_data.get("message_id")
    
    # === ПРОВЕРКА: message_id обязателен ===
    if not message_id:
        error = f"Отсутствует message_id в input_data задачи {task_id}"
        logger.error(f"❌ {error}")
        complete_task_error(
            task_id=task_id,
            error_module="preprocessor",
            error_message=error
        )
        return
    
    # === ШАГ 0: Помечаем задачу как running ===
    mark_task_running(task_id=task_id)
    
    logger.info(f"🔍 Начало предобработки сообщения {message_id[:8]}...")
    
    # === ШАГ 1: Получаем данные сообщения из БД ===
    msg_data = get_user_message_data(db_config, message_id)
    if not msg_data:
        error = f"Сообщение {message_id} не найдено в БД"
        logger.error(f"❌ {error}")
        complete_task_error(
            task_id=task_id,
            error_module="preprocessor",
            error_message=error
        )
        return
    
    user_content: str = msg_data["row_text"]
    session_id: str = msg_data["session_id"]
    current_room: str = msg_data["current_room"]
    
    # === ШАГ 2: Получаем промпт предразбора ===
    prompt_data = get_preprocessing_prompt(db_config)
    if not prompt_data:
        error = "Промпт 'preprocess_user_question' не найден"
        logger.error(f"❌ {error}")
        complete_task_error(
            task_id=task_id,
            error_module="preprocessor",
            error_message=error
        )
        return
    
    prompt_id: str = prompt_data["id"]
    system_prompt: str = prompt_data["text"]
    model_params: Dict[str, Any] = prompt_data["params"] or {}
    
    # === ШАГ 3: Создаём шаг оркестратора ===
    step_input = {
        "message_id": message_id,
        "prompt_id": prompt_id,
        "session_id": session_id,
        "current_room": current_room
    }
    
    step_id = create_orchestrator_step(
        task_id=task_id,
        step_number=1,
        step_type_name="user_question_preprocessing",
        input_data=step_input
    )
    logger.info(f"✅ Шаг предразбора создан: {step_id[:8]}")
    
    # === ШАГ 4: Формируем messages для LLM ===
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content}
    ]
    
    # === ШАГ 5: Вызываем модель (параметры ТОЛЬКО из промпта) ===
    logger.debug(f"Вызов ModelService.generate для предразбора...")
    
    model_service = ModelService()
    
    # Проверяем обязательные параметры из промпта
    required_params = ["temperature", "top_p", "top_k", "min_p", "max_tokens", "presence_penalty", "stop"]
    missing_params = [p for p in required_params if p not in model_params]
    
    if missing_params:
        error = f"Отсутствуют обязательные параметры в промпте: {missing_params}"
        logger.error(f"❌ {error}")
        complete_step_error(
            step_id=step_id,
            error_module="preprocessor",
            error_message=error
        )
        complete_task_error(
            task_id=task_id,
            error_module="preprocessor",
            error_message=error
        )
        return
    
    result = model_service.generate(
        messages=messages,
        temperature=model_params["temperature"],
        top_p=model_params["top_p"],
        top_k=model_params["top_k"],
        min_p=model_params["min_p"],
        max_tokens=model_params["max_tokens"],
        presence_penalty=model_params["presence_penalty"],
        stop=model_params["stop"]
    )
    
    # === ШАГ 6: Проверяем успех генерации ===
    if not result["success"]:
        error = result.get("error", "Неизвестная ошибка модели")
        logger.error(f"❌ Ошибка генерации: {error}")
        complete_step_error(
            step_id=step_id,
            error_module="ModelService",
            error_message=error
        )
        complete_task_error(
            task_id=task_id,
            error_module="preprocessor",
            error_message=error
        )
        return
    
    # === ШАГ 7: Парсим JSON-ответ ===
    raw_response: str = result["response"]
    logger.debug(f"Сырой ответ модели: {raw_response[:200]}...")
    
    parsed_data = parse_room_weights_json(raw_response)
    
    if not parsed_data:
        # === ОШИБКА: не удалось распарсить → завершаем задачу с ошибкой ===
        error = "Не удалось распарсить JSON-ответ модели с весами комнат"
        logger.error(f"❌ {error}")
        complete_step_error(
            step_id=step_id,
            error_module="preprocessor",
            error_message=error
        )
        complete_task_error(
            task_id=task_id,
            error_module="preprocessor",
            error_message=error
        )
        return
    
    # === ШАГ 8: Сохраняем метрики LLM ===
    metrics: Dict[str, Any] = result["metrics"]
    
    llm_metric_id: str = save_llm_metrics(
        orchestrator_step_id=step_id,
        prompt_id=prompt_id,
        host="main-srv",
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
    logger.debug(f"✅ Метрики LLM сохранены: {llm_metric_id[:8]}")
    
    # === ШАГ 9: Сохраняем результат предразбора ===
    preprocess_result_id = save_preprocessed_result(
        db_config=db_config,
        message_id=message_id,
        preprocessed_data=parsed_data,
        llm_metric_id=llm_metric_id
    )
    
    if not preprocess_result_id:
        logger.error("⚠️ Не удалось сохранить результат предразбора, продолжаем...")
    
    # === ШАГ 10: Обновляем сообщение пользователя ===
    update_success = update_message_processed_text(
        db_config=db_config,
        message_id=message_id,
        processed_text=user_content,
        preprocess_result_id=preprocess_result_id or "",
        llm_metric_id=llm_metric_id,
        orchestrator_step_id=step_id 
    )
    
    if not update_success:
        logger.warning("⚠️ Не удалось обновить processed_text в сообщении")
    
    # === ШАГ 11: Создаём задачу генерации ответа (приоритет 0.8) ===
    try:
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id FROM orchestrator.task_types 
                    WHERE type_name = %s
                """, ("user_answer_generation",))
                
                row = cur.fetchone()
                if not row:
                    raise RuntimeError("Тип задачи 'user_answer_generation' не найден")
                
                task_type_id = row[0]
                
                cur.execute("""
                    INSERT INTO orchestrator.orchestrator_tasks (
                        task_type_id,
                        input_data,
                        priority,
                        status,
                        kaya_version,
                        created_at
                    ) VALUES (%s, %s, %s, %s, %s, NOW())
                """, (
                    task_type_id,
                    json.dumps({"message_id": message_id}),
                    0.8,  # Приоритет для генерации ответа (выше чем предразбор)
                    "pending",
                    kaya_version
                ))
                conn.commit()
                
                logger.info(
                    f"✅ Задача генерации ответа создана для {message_id[:8]}..."
                )
                
    except Exception as e:
        logger.error(f"Ошибка создания задачи генерации: {e}", exc_info=True)
    
    # === ШАГ 12: Завершаем шаг и задачу предразбора ===
    step_output = {
        "preprocess_result_id": preprocess_result_id,
        "llm_metric_id": llm_metric_id
    }

    complete_step_success(step_id=step_id, output_data=step_output)
    complete_task_success(task_id=task_id, output_data=step_output)