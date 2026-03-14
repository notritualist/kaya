"""
main-srv/src/orchestrator/orchestrator.py
Основной цикл оркестратора задач AGI-системы Kaya.

Задачи модуля:
- Выхватывать pending-задачи из orchestrator.orchestrator_tasks
- Запускать обработчики в отдельных потоках (threading)
- Контролировать параллелизм через флаги занятости
- Логировать ошибки в kaya_full.log

Архитектура:
- Singleton-цикл: один поток _orchestrator_loop() на приложение
- Типы задач обрабатываются независимо (можно добавить новые без правки ядра)
- FOR UPDATE SKIP LOCKED в SQL — защита от дублирования задач при нескольких воркерах

Требования:
- БД PostgreSQL с применённой миграцией V001
- Запущенный llama-server (scripts/model_orchestrator.sh)
- Импортируемые модули: db_manager, services.service_metrics, orchestrator.response_composer

Пример запуска:
    from orchestrator.orchestrator import start_orchestrator
    start_orchestrator()  # Запускает фоновый поток
"""
version = "1.1.0"
description = "Оркестратор задач AGI-системы Kaya"

import threading
import time
import logging
import psycopg2
from psycopg2.extras import RealDictCursor
from typing import Dict, Any

# Локальные импорты в рамках проекта
from db_manager.db_manager import load_postgres_config
from services.service_metrics import (
    mark_task_running,
    complete_task_error
)
from orchestrator.preprocessor import preprocess_user_message

# Логгер модуля — подхватит настройки из main.py (файл + консоль, уровни)
logger = logging.getLogger(__name__)

# =============================================================================
# НАСТРОЙКИ ОРКЕСТРАТОРА
# =============================================================================
# Интервал проверки очередей задач (секунды)
CHECK_INTERVAL: int = 1
# Флаг работы основного цикла
_running: bool = False

# =============================================================================
# ФЛАГИ ЗАНЯТОСТИ ДЛЯ КОНТРОЛЯ ПАРАЛЛЕЛИЗМА
# =============================================================================
# Ограничиваем параллелизм предразбора (чтобы не перегрузить модель)
_preprocessor_busy: bool = False
_preprocessor_lock: threading.Lock = threading.Lock()

# Ограничиваем параллелизм финальных генераций
_composer_busy: bool = False
_composer_lock: threading.Lock = threading.Lock()

# Ограничемваем параллелизм нормальзации row_messages и рекласификации комнат у сообщений
_normalization_busy: bool = False
_normalization_lock: threading.Lock = threading.Lock()

_reclassification_busy: bool = False
_reclassification_lock: threading.Lock = threading.Lock()


def _cleanup_dangling_records(db_config: dict):
    """
    Очищает зависшие записи при старте оркестратора.
    Вызывается один раз при запуске.
    """
    with psycopg2.connect(**db_config) as conn:
        with conn.cursor() as cur:
            # 1. Сброс задач (pending/running → failed)
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
            conn.commit()
            
            # 2. Сброс шагов (pending/running → failed)
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
            
            # 3. Завершение сессий (active → completed)
            cur.execute("""
                UPDATE dialogs.sessions
                SET 
                    status = 'completed'::session_status,
                    closed_at = NOW(),
                    updated_at = NOW()
                WHERE status = 'active'
            """)
            sessions_count = cur.rowcount
            conn.commit()
            
            # Логируем результаты
            if tasks_count > 0:
                logger.warning("🔄 Сброшено %d зависших задач", tasks_count)
            if steps_count > 0:
                logger.warning("🔄 Сброшено %d зависших шагов", steps_count)
            if sessions_count > 0:
                logger.warning("🔄 Завершено %d зависших сессий", sessions_count)


def _get_pending_task(db_config: dict, task_type_name: str) -> RealDictCursor | None:
    """
    Получает следующую pending-задачу указанного типа из БД.
    
    Использует FOR UPDATE SKIP LOCKED для безопасной работы при нескольких воркерах:
    - Если задача уже захвачена другим потоком — она пропускается
    - Гарантируется, что одна задача не будет обработана дважды
    
    Args:
        db_config (dict): Параметры подключения к PostgreSQL
        task_type_name (str): Имя типа задачи из orchestrator.task_types.type_name
        
    Returns:
        RealDictCursor | None: Задача с полями id, input_data или None, если задач нет
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


def _handle_question_preprocessing(task_id: str, input_data: dict) -> None:
    """
    Обработчик задачи предразбора вопроса пользователя.
    """
    global _preprocessor_busy
    try:
        logger.debug(f"🔍 Запуск предразбора для задачи {task_id[:8]}...")
        preprocess_user_message(task_id=task_id, input_data=input_data)
        
    except Exception as exc:
        logger.exception(
            f"❌ Ошибка в preprocessor (task_id={task_id[:8]}...): {exc}"
        )
        complete_task_error(
            task_id=task_id,
            error_module="preprocessor",
            error_message=str(exc)
        )
    finally:
        with _preprocessor_lock:
            _preprocessor_busy = False


def _handle_answer_generation(task_id: str, input_data: dict) -> None:
    """
    Обработчик задачи генерации ответа пользователю.
    
    Запускается в отдельном потоке, чтобы не блокировать основной цикл оркестратора.
    
    Логика:
    1. Импортирует compose_final_response (чтобы избежать циклических импортов при старте)
    2. Вызывает композер с task_id и input_data
    3. При ошибке — логирует и завершает задачу с статусом failed
    4. Сбрасывает флаг занятости _composer_busy в finally
    
    Args:
        task_id (str): UUID задачи из orchestrator.orchestrator_tasks
        input_data (dict): Входные данные задачи (ожидается {"message_id": "<uuid>"})
    """
    global _composer_busy
    try:
        # Импорт внутри функции — защита от циклических зависимостей при инициализации
        from orchestrator.response_composer import compose_final_response
        compose_final_response(task_id=task_id, input_data=input_data)
        
    except Exception as exc:
        # Логируем полную трассировку ошибки для отладки
        logger.exception(
            f"❌ Ошибка в response_composer (task_id={task_id[:8]}...): {exc}"
        )
        # Завершаем задачу с ошибкой, чтобы она не висела в running
        complete_task_error(
            task_id=task_id,
            error_module="response_composer",
            error_message=str(exc)
        )
    finally:
        # Гарантированно сбрасываем флаг, даже если произошла ошибка
        with _composer_lock:
            _composer_busy = False


def _handle_messages_normalization(task_id: str, input_data: Dict[str, Any]) -> None:
    """
    Обработчик задачи фоновой нормализации сообщений.
    Выполняется с низким приоритетом после основных задач.
    
    Args:
        task_id (str): UUID задачи из orchestrator.orchestrator_tasks
        input_data (dict): {"user_message_id": "...", "system_message_id": "...", "session_id": "..."}
    """
    global _normalization_busy
    try:
        logger.debug(f"🧹 Запуск нормализации для задачи {task_id[:8]}...")
        # Импорт внутри функции — защита от циклических зависимостей
        from orchestrator.tools.messages_normalize import normalize_messages
        normalize_messages(task_id=task_id, input_data=input_data)
        
    except Exception as exc:
        logger.exception(
            f"❌ Ошибка в messages_normalize (task_id={task_id[:8]}...): {exc}"
        )
        complete_task_error(
            task_id=task_id,
            error_module="messages_normalize",
            error_message=str(exc)
        )
    finally:
        with _normalization_lock:
            _normalization_busy = False


def _handle_message_reclassification(task_id: str, input_data: Dict[str, Any]) -> None:
    """
    Обработчик задачи фоновой реклассификации сообщений по комнатам.
    Выполняется с очень низким приоритетом (0.2) после всех основных задач.
    
    Args:
        task_id (str): UUID задачи из orchestrator.orchestrator_tasks
        input_data (dict): {"user_message_id": "...", "system_message_id": "...", "session_id": "..."}
    """
    global _reclassification_busy
    try:
        logger.debug(f"🔍 Запуск реклассификации для задачи {task_id[:8]}...")
        # Импорт внутри функции — защита от циклических зависимостей
        from orchestrator.tools.reclassification_rooms import reclassify_message_room
        reclassify_message_room(task_id=task_id, input_data=input_data)
        
    except Exception as exc:
        logger.exception(
            f"❌ Ошибка в reclassification_rooms (task_id={task_id[:8]}...): {exc}"
        )
        complete_task_error(
            task_id=task_id,
            error_module="reclassification_rooms",
            error_message=str(exc)
        )
    finally:
        with _reclassification_lock:
            _reclassification_busy = False


def _orchestrator_loop() -> None:
    """
    Основной бесконечный цикл оркестратора.
    Работает в фоновом потоке, запускается через start_orchestrator().
    Цикл:
    1. Проверяет очередь задач типа 'user_question_preprocessing', 'user_answer_generation',
       'messages_normalization', 'message_room_reclassification'
    2. Если задача есть и обработчик свободен — запускает её в потоке
    3. Ждёт CHECK_INTERVAL секунд и повторяет
    4. При любой ошибке — логирует и продолжает работу (устойчивость к сбоям)
    
    Остановка: при установке _running = False (через stop_orchestrator())
    """
    global _preprocessor_busy, _composer_busy, _normalization_busy, _reclassification_busy, _running

    # Загружаем конфиг БД один раз при старте цикла
    db_config: dict = load_postgres_config()
    logger.info("🔄 Оркестратор запущен: проверка задач каждые %d сек", CHECK_INTERVAL)

    while _running:
        try:
            # === Обработка задач ПРЕДРАЗБОРА (приоритет 0.7) ===
            if not _preprocessor_busy:
                task = _get_pending_task(
                    db_config=db_config,
                    task_type_name="user_question_preprocessing"
                )
                
                if task:
                    task_id: str = task["id"]
                    input_data: dict = task["input_data"]
                    
                    mark_task_running(task_id=task_id)
                    
                    with _preprocessor_lock:
                        _preprocessor_busy = True
                     
                    threading.Thread(
                        target=_handle_question_preprocessing,
                        args=(task_id, input_data),
                        daemon=True,
                        name=f"Preprocessor-{task_id[:8]}"
                    ).start()
                    
                    logger.debug(
                        f"🚀 Запущена задача предразбора: {task_id[:8]}..."
                    )
            
            # === Обработка задач ГЕНЕРАЦИИ ОТВЕТА (приоритет 0.8) ===
            if not _composer_busy:
                task = _get_pending_task(
                    db_config=db_config,
                    task_type_name="user_answer_generation"
                )
                
                if task:
                    task_id: str = task["id"]
                    input_data: dict = task["input_data"]
                    
                    mark_task_running(task_id=task_id)
                    
                    with _composer_lock:
                        _composer_busy = True
                     
                    threading.Thread(
                        target=_handle_answer_generation,
                        args=(task_id, input_data),
                        daemon=True,
                        name=f"Composer-{task_id[:8]}"
                    ).start()
                    
                    logger.debug(
                        f"🚀 Запущена задача генерации: {task_id[:8]}..."
                    )
            
           # === НОРМАЛИЗАЦИЯ (приоритет 0.5, фоновый режим) ===
            if not _normalization_busy:
                task = _get_pending_task(db_config, "messages_normalization")
                if task:
                    task_id: str = task["id"]
                    input_data: Dict[str, Any] = task["input_data"]
                    mark_task_running(task_id=task_id)
                    with _normalization_lock:
                        _normalization_busy = True
                    threading.Thread(
                        target=_handle_messages_normalization,
                        args=(task_id, input_data),
                        daemon=True,
                        name=f"Normalizer-{task_id[:8]}"
                    ).start()
                    logger.debug(f"🧹 Запущена задача нормализации: {task_id[:8]}... (приоритет=0.5)")

            # === РЕКЛАССИФИКАЦИЯ (приоритет 0.2, фоновый режим) ===
            if not _reclassification_busy:
                task = _get_pending_task(db_config, "message_room_reclassification")
                if task:
                    task_id: str = task["id"]
                    input_data: Dict[str, Any] = task["input_data"]
                    mark_task_running(task_id=task_id)
                    with _reclassification_lock:
                        _reclassification_busy = True
                    threading.Thread(
                        target=_handle_message_reclassification,
                        args=(task_id, input_data),
                        daemon=True,
                        name=f"Reclassifier-{task_id[:8]}"
                    ).start()
                    logger.debug(f"🔍 Запущена задача реклассификации: {task_id[:8]}... (приоритет=0.2)")

            # ← ✅ СОН В КОНЦЕ ЦИКЛА, после всех проверок
            time.sleep(CHECK_INTERVAL)

        except Exception as exc:
            logger.exception(f"❌ Ошибка в цикле оркестратора: {exc}")
            time.sleep(CHECK_INTERVAL)



def start_orchestrator() -> threading.Thread | None:
    """
    Запускает оркестратор в фоновом потоке.
    
    Вызывается из main.py после инициализации всех сервисов.
    
    Returns:
        threading.Thread | None: Ссылка на поток оркестратора или None, если уже запущен
    """
    global _running

    # Защита от повторного запуска
    if _running:
        logger.warning("⚠️ Оркестратор уже запущен, пропускаю повторный старт")
        return None

    # === ОЧИСТКА ЗАВИСШИХ ЗАДАЧ/ШАГОВ ПРИ СТАРТЕ ===
    db_config = load_postgres_config()
    _cleanup_dangling_records(db_config)

    _running = True
    thread = threading.Thread(target=_orchestrator_loop, daemon=True, name="Orchestrator")
    thread.start()

    logger.info("✅ Оркестратор запущен в фоновом потоке")
    return thread


def stop_orchestrator() -> None:
    """
    Останавливает оркестратор.
    
    Вызывается при завершении приложения (в блоке finally main.py).
    Устанавливает _running = False, после чего цикл _orchestrator_loop() завершится.
    """
    global _running
    _running = False
    logger.info("🛑 Оркестратор остановлен")