"""
main-srv/src/orchestrator/orchestrator.py

The main loop of the AGI task orchestrator.
The current implementation supports only one task type:
- user_answer_generation — generating the final user response.

Architectural principles:
- Singleton loop: one background thread per application.
- Safe task retrieval via FOR UPDATE SKIP LOCKED.
- Concurrency control: only one generation can be running at a time.
- Fault tolerance: errors are logged, but the loop is not terminated.
- Hanging tasks are cleared at startup.

Startup example:
from orchestrator.orchestrator import start_orchestrator
start_orchestrator() # starts a background thread
"""

__version__ = "1.0.0"
__description__ = "AGI Agent Task Orchestrator"

import threading
import time
import logging
import psycopg2
from psycopg2.extras import RealDictCursor

# Локальные импорты
from db_manager.db_manager import load_postgres_config
from services.service_metrics import mark_task_running, complete_task_error

logger = logging.getLogger(__name__)

# =============================================================================
# НАСТРОЙКИ ОРКЕСТРАТОРА
# =============================================================================

# Пульс оркестратора — интервал между проверками очереди задач (в секундах).
# Аналог человеческого пульса: не слишком часто, но достаточно для отзывчивости.
PULSE_SECONDS: int = 1

# Флаг работы основного цикла
_running: bool = False

# =============================================================================
# ФЛАГ ЗАНЯТОСТИ ДЛЯ КОНТРОЛЯ ПАРАЛЛЕЛИЗМА
# =============================================================================

# Разрешаем только одну одновременную генерацию ответа (чтобы не перегружать LLM)
_composer_busy: bool = False
_composer_lock: threading.Lock = threading.Lock()


def _cleanup_dangling_records(db_config: dict):
    """
    Очищает зависшие записи при старте оркестратора.
    Выполняется один раз при запуске.
    Обрабатывает:
    - задачи со статусом pending/running → failed,
    - шаги со статусом pending/running → failed
    """
    with psycopg2.connect(**db_config) as conn:
        with conn.cursor() as cur:
            # Сброс зависших задач
            cur.execute("""
                UPDATE orchestrator.orchestrator_tasks
                SET 
                    status = 'failed'::task_status,
                    completed_at = NOW(),
                    error_module = 'orchestrator_startup',
                    error_message = 'System restart: task interrupted',
                    error_timestamp = NOW(),
                    run_latency = EXTRACT(EPOCH FROM (NOW() - started_at)),
                    total_latency = EXTRACT(EPOCH FROM (NOW() - created_at))
                WHERE status IN ('pending', 'running')
            """)
            tasks_count = cur.rowcount

            # Сброс зависших шагов
            cur.execute("""
                UPDATE orchestrator.orchestrator_steps
                SET 
                    status = 'failed'::task_status,
                    completed_at = NOW(),
                    error_module = 'orchestrator_startup',
                    error_message = 'System restart: step interrupted',
                    error_timestamp = NOW(),
                    latency = EXTRACT(EPOCH FROM (NOW() - created_at))
                WHERE status IN ('pending', 'running')
            """)
            steps_count = cur.rowcount

            conn.commit()

            if tasks_count > 0:
                logger.warning("🔄 Cleared %d dangling tasks on startup", tasks_count)
            if steps_count > 0:
                logger.warning("🔄 Cleared %d dangling steps on startup", steps_count)
            

def _get_pending_task(db_config: dict, task_type_name: str):
    """
    Извлекает следующую ожидающую задачу указанного типа из БД.
    Использует FOR UPDATE SKIP LOCKED для защиты от дублирования при многопоточности.
    
    Args:
        db_config (dict): параметры подключения к PostgreSQL
        task_type_name (str): имя типа задачи (например, 'user_answer_generation')
        
    Returns:
        dict | None: словарь с полями 'id' и 'input_data', или None
    """
    with psycopg2.connect(**db_config) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT t.id, t.input_data
                FROM orchestrator.orchestrator_tasks t
                JOIN orchestrator.task_types tt ON t.task_type_id = tt.id
                WHERE t.status = 'pending'::task_status
                  AND tt.type_name = %s
                ORDER BY t.created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            """, (task_type_name,))
            return cur.fetchone()


def _handle_answer_generation(task_id: str, input_data: dict) -> None:
    """
    Обработчик задачи генерации финального ответа пользователю.
    Запускается в отдельном потоке.
    
    Логика:
    1. Импортирует compose_final_response внутри функции (защита от циклических импортов).
    2. Вызывает композер.
    3. При ошибке — завершает задачу как failed.
    4. Всегда сбрасывает флаг занятости в finally.
    """
    global _composer_busy
    try:
        from orchestrator.response_composer import compose_final_response
        compose_final_response(task_id=task_id, input_data=input_data)
    except Exception as exc:
        logger.exception("Error in response_composer (task_id=%s): %s", task_id[:8], exc)
        complete_task_error(
            task_id=task_id,
            error_module="response_composer",
            error_message=str(exc)
        )
    finally:
        with _composer_lock:
            _composer_busy = False


def _orchestrator_loop():
    """
    Основной цикл оркестратора.
    Работает в фоновом потоке.
    На каждом "пульсе" проверяет очередь задач генерации ответа.
    Если есть задача и композер свободен — запускает её в новом потоке.
    """
    global _composer_busy, _running

    db_config = load_postgres_config()
    logger.info("Orchestrator started. Pulse interval: %d second(s)", PULSE_SECONDS)

    while _running:
        try:
            if not _composer_busy:
                task = _get_pending_task(db_config, "user_answer_generation")
                if task:
                    task_id = task["id"]
                    input_data = task["input_data"]

                    mark_task_running(task_id=task_id)

                    with _composer_lock:
                        _composer_busy = True

                    threading.Thread(
                        target=_handle_answer_generation,
                        args=(task_id, input_data),
                        daemon=True,
                        name=f"Composer-{task_id[:8]}"
                    ).start()

                    logger.debug("Launched answer generation task: %s", task_id[:8])

            time.sleep(PULSE_SECONDS)

        except Exception as exc:
            logger.exception("Critical error in orchestrator loop: %s", exc)
            time.sleep(PULSE_SECONDS)


def start_orchestrator() -> threading.Thread | None:
    """
    Запускает оркестратор в фоновом потоке.
    Выполняет очистку зависших записей перед стартом.
    Защищён от повторного запуска.
    
    Returns:
        threading.Thread | None: ссылка на поток или None, если уже запущен
    """
    global _running
    if _running:
        logger.warning("Orchestrator is already running")
        return None

    db_config = load_postgres_config()
    _cleanup_dangling_records(db_config)

    _running = True
    thread = threading.Thread(target=_orchestrator_loop, daemon=True, name="Orchestrator")
    thread.start()

    logger.info("Orchestrator background thread started")
    return thread


def stop_orchestrator():
    """
    Корректно останавливает оркестратор.
    Устанавливает флаг _running = False, после чего цикл завершится.
    """
    global _running
    _running = False
    logger.info("Orchestrator stopped")