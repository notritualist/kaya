"""
main-srv/src/services/service_metrics.py

Helper functions for working with orchestrator metrics and statuses.

Module responsibilities:
- Update task and step statuses in orchestrator.orchestrator_tasks / _steps
- Store LLM query metrics in metrics.llm_internal
- Store reasoning in orchestrator.reasonings
- Bind reasoning and metrics to orchestrator steps

Architecture:
- All functions accept IDs and data, execute SQL queries, and return IDs or None
- agent_version is imported from version.py, as in the entire project
- Logging via logging.getLogger(__name__) → kaya_full.log

Requirements:
1. Migration V001 applied
2. Access to PostgreSQL via psycopg2
3. Imported modules: db_manager, version

Usage example:
step_id = create_orchestrator_step(task_id="...", step_type_name="user_answer_generation", ...)
    save_llm_metrics(orchestrator_step_id=step_id, prompt_id="...", ...)
"""

__version__ = "1.0.0"
__description__ = "Utility module for updating statuses and saving metrics"

import logging
import psycopg2
from psycopg2.extras import Json
from typing import Optional, Dict, Any
from datetime import datetime, timezone

# Локальные импорты
from db_manager.db_manager import load_postgres_config

# Единая версия проекта — как в main.py
from version import __version__ as agent_version

# Логгер модуля
logger = logging.getLogger(__name__)

# =============================================================================
# === УПРАВЛЕНИЕ СТАТУСАМИ ЗАДАЧ И ШАГОВ ===
# =============================================================================

def mark_task_running(task_id: str) -> None:
    """
    Помечает задачу как выполняющуюся (status='running').
    
    Вызывается оркестратором перед запуском обработчика в потоке.
    
    Args:
        task_id (str): UUID задачи из orchestrator.orchestrator_tasks
        
    Returns:
        None
    """
    db_config: dict = load_postgres_config()
    with psycopg2.connect(**db_config) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE orchestrator.orchestrator_tasks
                SET status = 'running'::task_status,
                    started_at = NOW()
                WHERE id = %s
            """, (task_id,))
            conn.commit()
    logger.debug("ЗTask %s is marked as running", task_id[:8])


def complete_task_success(task_id: str, output_data: Optional[Dict[str, Any]] = None) -> None:
    """
    Завершает задачу успешно (status='completed').
    
    Args:
        task_id (str): UUID задачи
        output_data (dict, optional): Результаты выполнения задачи в формате JSON
    """
    db_config: dict = load_postgres_config()
    with psycopg2.connect(**db_config) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE orchestrator.orchestrator_tasks
                SET 
                    status = 'completed'::task_status,
                    completed_at = NOW(),
                    output_data = %s,
                    run_latency = EXTRACT(EPOCH FROM (NOW() - started_at)),
                    total_latency = EXTRACT(EPOCH FROM (NOW() - created_at))
                WHERE id = %s
            """, (Json(output_data) if output_data else None, task_id))
            conn.commit()
    logger.info("Task %s completed successfully", task_id[:8])

def complete_task_error(
        task_id: str,
        error_module: str,
        error_message: str
    ) -> None:
        """
        Завершает задачу с ошибкой (status='failed').
        
        Args:
            task_id (str): UUID задачи
            error_module (str): Имя модуля, где произошла ошибка (для трассировки)
            error_message (str): Текст ошибки
            
        Returns:
            None
        """
        db_config: dict = load_postgres_config()
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE orchestrator.orchestrator_tasks
                    SET status = 'failed'::task_status,
                        completed_at = NOW(),
                        error_module = %s,
                        error_message = %s,
                        error_timestamp = NOW(),
                        total_latency = EXTRACT(EPOCH FROM (NOW() - created_at)),
                        run_latency = EXTRACT(EPOCH FROM (NOW() - started_at))
                    WHERE id = %s
                """, (error_module, error_message, task_id))
                conn.commit()
        logger.warning("Task %s completed with error: %s", task_id[:8], error_message)


def create_orchestrator_step(
    task_id: str,
    step_number: int,
    step_type_name: str,
    input_data: Optional[Dict[str, Any]] = None
) -> str:
    """
    Создаёт новый шаг оркестратора для задачи.
    
    Args:
        task_id (str): UUID родительской задачи
        step_number (int): Порядковый номер шага в задаче (начинается с 1)
        step_type_name (str): Имя типа шага из orchestrator.step_types.step_name
        input_data (dict, optional): Входные данные шага в формате JSON
        
    Returns:
        str: UUID созданного шага
    """
    db_config: dict = load_postgres_config()
    with psycopg2.connect(**db_config) as conn:
        with conn.cursor() as cur:
            # Получаем ID типа шага
            cur.execute("""
                SELECT id FROM orchestrator.step_types 
                WHERE step_name = %s
            """, (step_type_name,))
            row = cur.fetchone()
            if not row:
                raise RuntimeError(f"Step type '{step_type_name}' not found in orchestrator.step_types")
            step_type_id = row[0]
            
            # Создаём шаг
            cur.execute("""
                INSERT INTO orchestrator.orchestrator_steps (
                    task_id,
                    step_number,
                    step_type_id,
                    status,
                    input_data,
                    agent_version,
                    created_at
                ) VALUES (
                    %s, %s, %s, 'pending'::task_status, %s, %s, NOW()
                )
                RETURNING id
            """, (
                task_id,
                step_number,
                step_type_id,
                Json(input_data) if input_data else None,
                agent_version
            ))
            step_id = str(cur.fetchone()[0])
            conn.commit()
            
    logger.debug("Step %s created for task %s (type: %s)", step_id[:8], task_id[:8], step_type_name)
    return step_id


def complete_step_success(step_id: str, output_data: Optional[Dict[str, Any]] = None) -> None:
    """
    Завершает шаг успешно (status='completed').
    
    Args:
        step_id (str): UUID шага
        output_data (dict, optional): Результаты выполнения шага
    """
    db_config: dict = load_postgres_config()
    with psycopg2.connect(**db_config) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE orchestrator.orchestrator_steps
                SET 
                    status = 'completed'::task_status,
                    completed_at = NOW(),
                    output_data = %s,
                    latency = EXTRACT(EPOCH FROM (NOW() - created_at))
                WHERE id = %s
            """, (Json(output_data) if output_data else None, step_id))
            conn.commit()
    logger.debug("Step %s completed successfully", step_id[:8])


def complete_step_error(
    step_id: str,
    error_module: str,
    error_message: str
) -> None:
    """
    Завершает шаг с ошибкой (status='failed').
    
    Args:
        step_id (str): UUID шага
        error_module (str): Имя модуля, где произошла ошибка
        error_message (str): Текст ошибки
    """
    db_config: dict = load_postgres_config()
    with psycopg2.connect(**db_config) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE orchestrator.orchestrator_steps
                SET 
                    status = 'failed'::task_status,
                    completed_at = NOW(),
                    error_module = %s,
                    error_message = %s,
                    error_timestamp = NOW(),
                    latency = EXTRACT(EPOCH FROM (NOW() - created_at))
                WHERE id = %s
            """, (error_module, error_message, step_id))
            conn.commit()
    logger.warning("Step %s completed with error: %s", step_id[:8], error_message)


# =============================================================================
# === СОХРАНЕНИЕ МЕТРИК И РАССУЖДЕНИЙ ===
# =============================================================================

def save_llm_metrics(
    orchestrator_step_id: str,
    prompt_id: str,
    host: str,
    model: str,
    param: Dict[str, Any],
    cache_n: int,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    host_nctx: int,
    prompt_ms: float,
    prompt_per_token_ms: float,
    prompt_per_second: float,
    predicted_per_second: float,
    resp_time: float,
    net_latency: float,
    full_time: float,
    error_status: bool = False,
    error_message: Optional[str] = None
) -> str:
    """
    Сохраняет метрики LLM-запроса в metrics.llm_internal.
    
    Args:
        orchestrator_step_id (str): UUID шага оркестратора, инициировавшего запрос
        prompt_id (str): UUID использованного промпта
        host (str): Имя хоста, где выполнялся запрос
        model (str): Название модели
        param (dict): Параметры генерации (temperature, top_p и т.д.)
        cache_n (int): Количество токенов, взятых из кэша
        prompt_tokens (int): Токены во входном промпте
        completion_tokens (int): Токены в сгенерированном ответе
        total_tokens (int): Общее количество обработанных токенов
        host_nctx (int): Размер контекста (n_ctx) на хосте
        prompt_ms (float): Время обработки промпта в мс
        prompt_per_token_ms (float): Среднее время на токен промпта
        prompt_per_second (float): Скорость обработки промпта (токенов/сек)
        predicted_per_second (float): Скорость генерации ответа (токенов/сек)
        resp_time (float): Общее время генерации ответа в секундах
        net_latency (float): Сетевая задержка в секундах
        full_time (float): Полное время выполнения запроса в секундах
        error_status (bool): Флаг ошибки (по умолчанию False)
        error_message (str, optional): Текст ошибки, если была
        
    Returns:
        str: UUID записи метрики
    """
    db_config: dict = load_postgres_config()
    with psycopg2.connect(**db_config) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO metrics.llm_internal (
                    orchestrator_step_id,
                    prompt_id,
                    host,
                    model,
                    param,
                    cache_n,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    host_nctx,
                    prompt_ms,
                    prompt_per_token_ms,
                    prompt_per_second,
                    predicted_per_second,
                    resp_time,
                    net_latency,
                    full_time,
                    error_status,
                    error_message,
                    error_time,
                    agent_version,
                    timestamp
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW()
                )
                RETURNING id
            """, (
                orchestrator_step_id,
                prompt_id,
                host,
                model,
                Json(param),
                cache_n,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                host_nctx,
                prompt_ms,
                prompt_per_token_ms,
                prompt_per_second,
                predicted_per_second,
                resp_time,
                net_latency,
                full_time,
                error_status,
                error_message,
                datetime.now(timezone.utc) if error_status else None,
                agent_version
            ))
            metric_id = str(cur.fetchone()[0])
            conn.commit()
            
    logger.debug("LLM metrics saved: %s (step: %s)", metric_id[:8], orchestrator_step_id[:8])
    return metric_id


def save_reasoning(
    orchestrator_step_id: str,
    content: str,
    content_type: str,  # 'messages', 'reflection', 'second_reflection'
    for_actor_id: Optional[str] = None
) -> Optional[str]:
    """
    Сохраняет рассуждение (Chain of Thought) в orchestrator.reasonings.
    
    Args:
        orchestrator_step_id (str): UUID шага, в рамках которого сгенерировано рассуждение
        content (str): Текст рассуждения
        content_type (str): Тип рассуждения из ENUM reasoning_content_type
        for_actor_id (str, optional): ID актора, для которого предназначено рассуждение
        
    Returns:
        str | None: UUID записи рассуждения или None, если не сохранено
    """
    if not content or not content.strip():
        return None
        
    db_config: dict = load_postgres_config()
    with psycopg2.connect(**db_config) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO orchestrator.reasonings (
                    orchestrator_step_id,
                    reasoning_content,
                    reasoning_content_type,
                    agent_version,
                    timestamp
                ) VALUES (
                    %s, %s, %s, %s, NOW()
                )
                RETURNING id
            """, (
                orchestrator_step_id,
                content,
                content_type,
                agent_version
            ))
            reasoning_id = str(cur.fetchone()[0])
            conn.commit()
            
    logger.debug("Reasoning saved: %s (step: %s)", reasoning_id[:8], orchestrator_step_id[:8])
    return reasoning_id


def set_step_llm_metric_id(step_id: str, llm_metric_id: str) -> None:
    """
    Привязывает запись метрики LLM к шагу оркестратора.
    
    Args:
        step_id (str): UUID шага в orchestrator.orchestrator_steps
        llm_metric_id (str): UUID метрики в metrics.llm_internal
    """
    db_config: dict = load_postgres_config()
    with psycopg2.connect(**db_config) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE orchestrator.orchestrator_steps
                SET llm_metric_id = %s
                WHERE id = %s
            """, (llm_metric_id, step_id))
            conn.commit()
    logger.debug("Linked llm_metric_id %s to step %s", llm_metric_id[:8], step_id[:8])


def set_step_reasoning_id(step_id: str, reasoning_id: str) -> None:
    """
    Привязывает запись рассуждения к шагу оркестратора.
    
    Примечание: в текущей схеме рассуждение уже ссылается на шаг через 
    orchestrator.reasonings.orchestrator_step_id. Эта функция может использоваться
    для дополнительной индексации или кэширования, если потребуется в будущем.
    
    В текущей реализации — заглушка для совместимости с интерфейсом.
    
    Args:
        step_id (str): UUID шага
        reasoning_id (str): UUID рассуждения
    """
    # В текущей схеме V001 связь идёт "снизу вверх" (reasoning → step),
    # поэтому обратная ссылка не требуется. Функция оставлена для будущего расширения.
    logger.debug("Reasoning %s already linked to step %s via FK", reasoning_id[:8], step_id[:8])
    pass