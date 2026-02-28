"""
main-srv/src/services/service_metrics.py

Вспомогательные функции для работы с метриками и статусами оркестратора.

Задачи модуля:
- Обновлять статусы задач и шагов в orchestrator.orchestrator_tasks / _steps
- Сохранять метрики LLM-запросов в metrics.llm_internal
- Сохранять рассуждения в orchestrator.reasonings
- Привязывать рассуждения к шагам оркестратора

Архитектура:
- Все функции принимают ID и данные, выполняют SQL-запросы, возвращают ID или None
- kaya_version импортируется из version.py, как во всём проекте
- Логирование через logging.getLogger(__name__) → kaya_full.log
- Нет записи в БД из ModelService — только здесь, в сервисном слое

Требования:
1. Применённая миграция V001
2. Доступ к PostgreSQL через psycopg2
3. Импортируемые модули: db_manager, version

Пример использования:
    step_id = create_orchestrator_step(task_id="...", step_type_name="user_answer_generation", ...)
    save_llm_metrics(orchestrator_step_id=step_id, prompt_id="...", ...)
"""

__version__ = "1.0.0"
__description__ = "Утилиты для обновления статусов задач/шагов и сохранения метрик"

import logging
import psycopg2
from psycopg2.extras import RealDictCursor, Json
from datetime import datetime, timezone
from typing import Optional

# Локальные импорты
from db_manager.db_manager import load_postgres_config

# Единая версия проекта — как в main.py
from version import __version__ as kaya_version

# Логгер модуля
logger = logging.getLogger(__name__)


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
    logger.debug("Задача %s помечена как running", task_id[:8])


def complete_task_success(task_id: str, output_data: dict) -> None:
    """
    Завершает задачу с успехом (status='completed').
    
    Args:
        task_id (str): UUID задачи
        output_data (dict): Результат выполнения задачи (сохраняется в JSONB)
        
    Returns:
        None
    """
    db_config: dict = load_postgres_config()
    with psycopg2.connect(**db_config) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE orchestrator.orchestrator_tasks
                SET status = 'completed'::task_status,
                    completed_at = NOW(),
                    output_data = %s,
                    total_latency = EXTRACT(EPOCH FROM (NOW() - created_at)),
                    run_latency = EXTRACT(EPOCH FROM (NOW() - started_at))
                WHERE id = %s
            """, (Json(output_data), task_id))
            conn.commit()
    logger.debug("Задача %s завершена успешно", task_id[:8])


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
    logger.warning("Задача %s завершена с ошибкой: %s", task_id[:8], error_message)


def create_orchestrator_step(
    task_id: str,
    step_number: int,
    step_type_name: str,
    input_data: dict,
    parent_step_id: Optional[str] = None
) -> str:
    """
    Создаёт шаг оркестратора и возвращает его ID.
    
    Args:
        task_id (str): UUID родительской задачи
        step_number (int): Порядковый номер шага внутри задачи
        step_type_name (str): Имя типа шага из orchestrator.step_types.step_name
        input_data (dict): Входные данные шага (сохраняются в JSONB)
        parent_step_id (str, optional): UUID родительского шага (для вложенных шагов)
        
    Returns:
        str: UUID созданного шага
        
    Raises:
        ValueError: Если тип шага не найден в справочнике
    """
    db_config: dict = load_postgres_config()
    with psycopg2.connect(**db_config) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Получаем ID типа шага из справочника
            cur.execute("""
                SELECT id FROM orchestrator.step_types 
                WHERE step_name = %s
            """, (step_type_name,))
            step_type = cur.fetchone()
            
            if not step_type:
                raise ValueError(f"Тип шага '{step_type_name}' не найден в orchestrator.step_types")
            
            # Создаём шаг
            cur.execute("""
                INSERT INTO orchestrator.orchestrator_steps (
                    task_id,
                    step_number,
                    step_type_id,
                    parent_step_id,
                    input_data,
                    status,
                    kaya_version,
                    created_at
                ) VALUES (%s, %s, %s, %s, %s, 'pending'::task_status, %s, NOW())
                RETURNING id
            """, (
                task_id,
                step_number,
                step_type["id"],
                parent_step_id,
                Json(input_data),
                kaya_version  # ← Версия из version.py, не хардкод!
            ))
            conn.commit()
            step_id: str = cur.fetchone()["id"]
            logger.debug("Создан шаг %s (задача %s, тип %s)", step_id[:8], task_id[:8], step_type_name)
            return step_id


def complete_step_success(step_id: str, output_data: dict) -> None:
    """
    Завершает шаг оркестратора с успехом.
    
    Args:
        step_id (str): UUID шага
        output_data (dict): Результат выполнения шага (JSONB)
        
    Returns:
        None
    """
    db_config: dict = load_postgres_config()
    with psycopg2.connect(**db_config) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE orchestrator.orchestrator_steps
                SET status = 'completed'::task_status,
                    completed_at = NOW(),
                    output_data = %s,
                    latency = EXTRACT(EPOCH FROM (NOW() - created_at))
                WHERE id = %s
            """, (Json(output_data), step_id))
            conn.commit()
    logger.debug("Шаг %s завершён успешно", step_id[:8])


def complete_step_error(
    step_id: str,
    error_module: str,
    error_message: str
) -> None:
    """
    Завершает шаг оркестратора с ошибкой.
    
    Args:
        step_id (str): UUID шага
        error_module (str): Имя модуля с ошибкой
        error_message (str): Текст ошибки
        
    Returns:
        None
    """
    db_config: dict = load_postgres_config()
    with psycopg2.connect(**db_config) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE orchestrator.orchestrator_steps
                SET status = 'failed'::task_status,
                    completed_at = NOW(),
                    error_module = %s,
                    error_message = %s,
                    error_timestamp = NOW(),
                    latency = EXTRACT(EPOCH FROM (NOW() - created_at))
                WHERE id = %s
            """, (error_module, error_message, step_id))
            conn.commit()
    logger.warning("Шаг %s завершён с ошибкой: %s", step_id[:8], error_message)


def set_step_reasoning_id(step_id: str, reasoning_id: str) -> None:
    """
    Привязывает рассуждение к шагу оркестратора.
    
    Args:
        step_id (str): UUID шага
        reasoning_id (str): UUID рассуждения из orchestrator.reasonings
        
    Returns:
        None
    """
    db_config: dict = load_postgres_config()
    with psycopg2.connect(**db_config) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE orchestrator.orchestrator_steps
                SET reasoning_id = %s
                WHERE id = %s
            """, (reasoning_id, step_id))
            conn.commit()
    logger.debug("Рассуждение %s привязано к шагу %s", reasoning_id[:8], step_id[:8])


def save_reasoning(
    orchestrator_step_id: str,
    content: str,
    content_type: str = "messages"
) -> Optional[str]:
    """
    Сохраняет рассуждение модели и возвращает его ID.
    
    Args:
        orchestrator_step_id (str): UUID шага, в рамках которого сгенерировано рассуждение
        content (str): Текст рассуждения (содержимое <think>...</think>)
        content_type (str): Тип источника: "messages" (из диалога) или "reflection" (саморефлексия)
        
    Returns:
        str | None: UUID сохранённого рассуждения или None, если content пустой
    """
    if not content:
        return None
    
    db_config: dict = load_postgres_config()
    with psycopg2.connect(**db_config) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO orchestrator.reasonings (
                    orchestrator_step_id,
                    reasoning_content,
                    reasoning_content_type,
                    kaya_version,
                    timestamp
                ) VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """, (
                orchestrator_step_id,
                content,
                content_type,
                kaya_version,  # ← Версия из version.py
                datetime.now(timezone.utc)
            ))
            conn.commit()
            reasoning_id: str = cur.fetchone()["id"]
            logger.debug("Рассуждение сохранено: %s", reasoning_id[:8])
            return reasoning_id


def save_llm_metrics(
    orchestrator_step_id: str,
    prompt_id: str,
    host: str,
    model: str,
    param: dict,
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
    error_status: bool,
    error_message: Optional[str] = None
) -> str:
    """
    Сохраняет метрики LLM-запроса и возвращает ID записи.
    
    Все поля соответствуют колонкам таблицы metrics.llm_internal из миграции V001.
    
    Args:
        orchestrator_step_id (str): UUID шага оркестратора
        prompt_id (str): UUID использованного промпта
        host (str): Имя хоста, где выполнялся запрос
        model (str): Название модели (из ответа llama-server)
        param (dict): Параметры генерации (temperature, top_p и т.д.)
        cache_n (int): Количество токенов из кэша
        prompt_tokens (int): Токены в промпте
        completion_tokens (int): Сгенерированные токены
        total_tokens (int): Всего токенов
        host_nctx (int): Размер контекста на хосте (из конфига)
        prompt_ms (float): Время обработки промпта (мс)
        prompt_per_token_ms (float): Среднее время на токен промпта
        prompt_per_second (float): Скорость обработки промпта (ток/сек)
        predicted_per_second (float): Скорость генерации ответа (ток/сек)
        resp_time (float): Время генерации ответа (сек)
        net_latency (float): Задержка сети (сек)
        full_time (float): Полное время выполнения (сек)
        error_status (bool): Флаг ошибки
        error_message (str, optional): Текст ошибки
        
    Returns:
        str: UUID записи метрик
    """
    db_config: dict = load_postgres_config()
    with psycopg2.connect(**db_config) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
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
                    kaya_version,
                    timestamp
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                kaya_version,  # ← Версия из version.py
                datetime.now(timezone.utc)
            ))
            conn.commit()
            metric_id: str = cur.fetchone()["id"]
            logger.debug("Метрики LLM сохранены: %s", metric_id[:8])
            return metric_id