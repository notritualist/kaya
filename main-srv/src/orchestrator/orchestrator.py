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
1. БД PostgreSQL с применённой миграцией V001
2. Запущенный llama-server (scripts/model_orchestrator.sh)
3. Импортируемые модули: db_manager, services.service_metrics, orchestrator.response_composer

Пример запуска:
    from orchestrator.orchestrator import start_orchestrator
    start_orchestrator()  # Запускает фоновый поток
"""

__version__ = "1.0.0"
__description__ = "Оркестратор задач AGI-системы Kaya"

import threading
import time
import logging
import psycopg2
from psycopg2.extras import RealDictCursor

# Локальные импорты в рамках проекта
from db_manager.db_manager import load_postgres_config
from services.service_metrics import (
    mark_task_running,
    complete_task_error
)

# Логгер модуля — подхватит настройки из main.py (файл + консоль, уровни)
logger = logging.getLogger(__name__)

# === НАСТРОЙКИ ОРКЕСТРАТОРА ===
# Интервал проверки очередей задач (секунды)
CHECK_INTERVAL: int = 1

# Флаг работы основного цикла
_running: bool = False

# Флаги занятости для задач с ограниченным параллелизмом
# (например, генерация ответа — не запускать 10 одновременно, чтобы не перегрузить модель)
_composer_busy: bool = False
_composer_lock: threading.Lock = threading.Lock()


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
            tasks_count = cur.rowcount  # ← ИСПРАВЛЕНО: используем rowcount
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
            steps_count = cur.rowcount  # ← ИСПРАВЛЕНО
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
            sessions_count = cur.rowcount  # ← ИСПРАВЛЕНО
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
        logger.exception(f"❌ Ошибка в response_composer (task_id={task_id[:8]}...): {exc}")
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


def _orchestrator_loop() -> None:
    """
    Основной бесконечный цикл оркестратора.
    
    Работает в фоновом потоке, запускается через start_orchestrator().
    
    Цикл:
    1. Проверяет очередь задач типа 'user_answer_generation'
    2. Если задача есть и обработчик свободен — запускает её в потоке
    3. Ждёт CHECK_INTERVAL секунд и повторяет
    4. При любой ошибке — логирует и продолжает работу (устойчивость к сбоям)
    
    Остановка: при установке _running = False (через stop_orchestrator())
    """
    global _composer_busy, _running
    
    # Загружаем конфиг БД один раз при старте цикла
    db_config: dict = load_postgres_config()
    logger.info("🔄 Оркестратор запущен: проверка задач каждые %d сек", CHECK_INTERVAL)
    
    while _running:
        try:
            # === Обработка задач генерации ответа (с ограничением параллелизма) ===
            # Проверяем, свободен ли обработчик ответов
            if not _composer_busy:
                # Пытаемся получить pending-задачу
                task = _get_pending_task(
                    db_config=db_config,
                    task_type_name="user_answer_generation"
                )
                
                if task:
                    task_id: str = task["id"]
                    input_data: dict = task["input_data"]
                    
                    # Помечаем задачу как выполняющуюся в БД
                    mark_task_running(task_id=task_id)
                    
                    # Блокируем обработчик для других потоков
                    with _composer_lock:
                        _composer_busy = True
                    
                    # Запускаем обработку в фоновом потоке
                    threading.Thread(
                        target=_handle_answer_generation,
                        args=(task_id, input_data),
                        daemon=True,  # Поток завершится автоматически при выходе из приложения
                        name=f"Composer-{task_id[:8]}"  # Имя потока для отладки в логах
                    ).start()
                    
                    logger.debug("🚀 Запущена задача генерации: %s...", task_id[:8])
            
            # === Здесь можно добавить обработку других типов задач ===
            # Пример для предразбора (без ограничения параллелизма):
            # task = _get_pending_task(db_config, "user_question_preprocessing")
            # if task:
            #     threading.Thread(target=_handle_preprocessing, args=(task["id"], task["input_data"]), daemon=True).start()
            
            # Пауза перед следующей итерацией цикла
            time.sleep(CHECK_INTERVAL)
            
        except Exception as exc:
            # Логируем ошибку, но не останавливаем цикл — устойчивость к сбоям
            logger.exception("❌ Ошибка в цикле оркестратора: %s", exc)
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