"""
main-srv/src/orchestrator/orchestrator.py

Main loop of the AGI task orchestrator.

Features:
- Singleton background thread with safe task retrieval (FOR UPDATE SKIP LOCKED).
- Concurrency control: one task at a time via _composer_busy flag.
- Task dependency enforcement: tasks with parent_task_id wait for parent completion.
- Task handlers: phs_affective_analysis, user_answer_generation, phs_baseline_drift, phs_momentary_decay.
- Fault tolerance: errors logged, loop continues.
- Lifecycle integration: check_inactivity and dialogue timeouts on each pulse.

Architecture:
- Orchestrator runs as daemon thread started from main.py.
- Tasks created by orchestrator_entry or phs_scheduler.
- Handlers dispatched via mapping dict, executed in separate threads.
- Metrics updated via service_metrics module.
- Task execution order enforced by priority and parent_task_id checks.
"""

__version__ = "1.3.0"
__description__ = "AGI Agent Task Orchestrator"

import threading
import time
import logging
import psycopg2
from typing import Dict, Callable
from psycopg2.extras import RealDictCursor

# Локальные импорты
from db_manager.db_manager import load_postgres_config
from services.service_metrics import mark_task_running, complete_task_error
from phs_service.lifecycle_manager import LifecycleManager
from dialog_services.dialogue_manager import check_dialogue_timeouts

logger = logging.getLogger(__name__)

# =============================================================================
# НАСТРОЙКИ ОРКЕСТРАТОРА
# =============================================================================

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
                logger.warning("Cleared %d dangling tasks on startup", tasks_count)
            if steps_count > 0:
                logger.warning("Cleared %d dangling steps on startup", steps_count)

def _get_pending_task(db_config: dict, task_type_name: str):
    """
    Извлекает следующую ожидающую задачу указанного типа из БД.
    Пропускает задачи, у которых parent_task_id не завершён.
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
                SELECT t.id, t.input_data, t.parent_task_id
                FROM orchestrator.orchestrator_tasks t
                JOIN orchestrator.task_types tt ON t.task_type_id = tt.id
                WHERE t.status = 'pending'::task_status
                  AND tt.type_name = %s
                ORDER BY t.priority DESC, t.created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            """, (task_type_name,))
            task = cur.fetchone()
            
            if not task:
                return None

            # === ПРОВЕРКА ЗАВИСИМОСТИ ===
            parent_id = task.get('parent_task_id')
            if parent_id:
                cur.execute("""
                    SELECT status FROM orchestrator.orchestrator_tasks WHERE id = %s
                """, (parent_id,))
                parent_row = cur.fetchone()
                
                # Если родитель не завершён — пропускаем задачу в этом пульсе
                if not parent_row or parent_row['status'] != 'completed':
                    logger.debug(
                        f"Task {task['id'][:8]} skipped: parent {parent_id[:8]} "
                        f"status={parent_row['status'] if parent_row else 'missing'}"
                    )
                    # Снимаем блокировку, возвращая задачу в очередь
                    conn.rollback()
                    return None
            
            return task

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

def _handle_momentary_decay(task_id: str, input_data: dict) -> None:
    """
    Обработчик задачи затухания momentary к baseline.
    
    Вызывает MomentaryManager.handle_decay_task, завершает задачу, сбрасывает флаг.
    """
    global _composer_busy
    try:
        from phs_service.momentary_manager import MomentaryManager
        from db_manager.db_manager import load_postgres_config
        mgr = MomentaryManager(load_postgres_config())
        mgr.handle_decay_task(task_id, input_data)
    except Exception as e:
        logger.exception("PHS momentary decay task failed")
        from services.service_metrics import complete_task_error
        complete_task_error(task_id, "phs_service", str(e))
    finally:
        with _composer_lock:
            _composer_busy = False
        
def _handle_phs_drift(task_id: str, input_data: dict):
    """
    Обработчик задачи естественного дрейфа baseline.
    
    Вызывает BaselineManager.handle_drift_task, завершает задачу, сбрасывает флаг.
    """
    global _composer_busy
    try:
        from phs_service.baseline_manager import BaselineManager
        mgr = BaselineManager(load_postgres_config())
        mgr.handle_drift_task(task_id, input_data)
    except Exception as e:
        logger.exception("PHS drift task failed")
        complete_task_error(task_id, "phs_service", str(e))
    finally:
        with _composer_lock:
            _composer_busy = False

def _handle_affective_analysis(task_id: str, input_data: dict) -> None:
    """
    Обработчик задачи пре-рефлексивного аффективного анализа.
    
    Вызывает affective_analyzer.handle_affective_analysis, завершает задачу, сбрасывает флаг.
    Запускается в отдельном потоке.
    """
    global _composer_busy
    try:
        from phs_service.affective_analyzer import handle_affective_analysis
        handle_affective_analysis(task_id=task_id, input_data=input_data)
    except Exception as e:
        logger.exception("PHS affective analysis task failed (task_id=%s): %s", task_id[:8], e)
        from services.service_metrics import complete_task_error
        complete_task_error(task_id, "affective_analyzer", str(e))
    finally:
        with _composer_lock:
            _composer_busy = False

def _get_task_type_name(db_config: dict, task_id: str) -> str:
    """Возвращает type_name задачи по её ID."""
    with psycopg2.connect(**db_config) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT tt.type_name
                FROM orchestrator.orchestrator_tasks t
                JOIN orchestrator.task_types tt ON t.task_type_id = tt.id
                WHERE t.id = %s
            """, (task_id,))
            row = cur.fetchone()
            return row[0] if row else "unknown"
 
def load_pulse_seconds(db_config: dict) -> int:
    """Загружает orchestrator_pulse_seconds из state.settings."""
    try:
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT value_float 
                    FROM state.settings 
                    WHERE param_name = 'orchestrator_pulse_seconds'
                """)
                row = cur.fetchone()
                return int(row[0]) if row and row[0] is not None else 1
    except Exception:
        logger.warning("Failed to load orchestrator_pulse_seconds from DB, using default=1")
        return 1

def _orchestrator_loop():
    """
    Основной цикл оркестратора.
    
    На каждом пульсе:
    1. Проверяет таймаут бездействия (lifecycle).
    2. Проверяет таймауты диалогов.
    3. Если не занят — извлекает задачу по приоритету:
       - phs_affective_analysis (prio 0.85)
       - user_answer_generation (prio 0.7)
       - phs_baseline_drift (prio 0.3)
       - phs_momentary_decay (prio 0.4)
    4. Запускает обработчик в отдельном потоке.
    
    Задачи с parent_task_id пропускаются, если родитель не завершён.
    """
    global _composer_busy, _running

    db_config = load_postgres_config()
    pulse_seconds = load_pulse_seconds(db_config)
    # Инициализация lifecycle manager
    lifecycle_mgr = LifecycleManager(db_config)

    logger.info("Orchestrator started. Pulse interval: %d second(s)", pulse_seconds)

    while _running:
        try:
            # === ПРОВЕРКА ТАЙМАУТОВ ===
            lifecycle_mgr.check_inactivity()
            check_dialogue_timeouts(db_config)
            
            if not _composer_busy:
                # === ИЗВЛЕЧЕНИЕ ЗАДАЧИ ПО ПРИОРИТЕТУ ===
                # 1. Аффективный анализ (высший приоритет)
                task = _get_pending_task(db_config, "phs_affective_analysis")
                
                # 2. Ответ пользователю
                if not task:
                    task = _get_pending_task(db_config, "user_answer_generation")
                
                # 3. Дрейф baseline
                if not task:
                    task = _get_pending_task(db_config, "phs_baseline_drift")
                
                # 4. Затухание momentary ← ДОБАВЛЕНО
                if not task:
                    task = _get_pending_task(db_config, "phs_momentary_decay")
                
                if task:
                    task_id = task["id"]
                    input_data = task["input_data"]
                    task_type = _get_task_type_name(db_config, task_id)
                    
                    mark_task_running(task_id)
                    
                    with _composer_lock:
                        _composer_busy = True
                    
                    # Маппинг типов → обработчиков
                    handlers: Dict[str, Callable] = {
                        "phs_affective_analysis": _handle_affective_analysis,
                        "user_answer_generation": _handle_answer_generation,
                        "phs_baseline_drift": _handle_phs_drift,
                        "phs_momentary_decay": _handle_momentary_decay,
                    }
                    
                    target = handlers.get(task_type)
                    if not target:
                        complete_task_error(task_id, "orchestrator", f"Unknown task type: {task_type}")
                        with _composer_lock:
                            _composer_busy = False
                        continue
                    
                    threading.Thread(
                        target=target,
                        args=(task_id, input_data),
                        daemon=True,
                        name=f"Orch-{task_type[:10]}-{task_id[:8]}"
                    ).start()
                    
                    logger.debug("Launched task %s: %s", task_type, task_id[:8])
            
            time.sleep(pulse_seconds)

        except Exception as exc:
            logger.exception("Critical error in orchestrator loop: %s", exc)
            time.sleep(pulse_seconds)

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