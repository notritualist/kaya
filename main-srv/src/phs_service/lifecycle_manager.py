"""
main-srv/src/phs_service/lifecycle_manager.py
A service module for managing the agent's pseudohormonal lifecycle states.
Version: 1.1.0
Fixes:
- Correctly handles open 'off' state on startup (graceful shutdown detection).
- Fixed invalid enum value 'crash' -> 'crash_recovery'.
"""
version = "1.0.0"
description = "Pseudohormonal lifecycle state manager"
import logging
from datetime import datetime, timezone
import psycopg2
from psycopg2.extras import RealDictCursor, Json
from typing import Dict, Any, Optional
# Import global agent version from pyproject.toml
from version import __version__ as agent_version

logger = logging.getLogger(__name__)

class LifecycleManager:
    """
    Управляет жизненным циклом агента в рамках ПГС.
    Все состояния хранятся в БД (схема `state`), кэширования нет.
    """
    def __init__(self, db_config: dict):
        """
        Инициализация менеджера.
        Args:
            db_config (dict): параметры подключения к PostgreSQL
        """
        self.db_config = db_config

    def _get_current_lifecycle(self, actor_id: str) -> dict | None:
        """
        Возвращает текущее активное состояние из state.agent_lifecycle.
        Returns:
            dict | None: запись с ended_at = NULL или None
        """
        with psycopg2.connect(**self.db_config) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, state_type, started_at
                    FROM state.agent_lifecycle
                    WHERE actor_id = %s AND ended_at IS NULL
                """, (actor_id,))
                return cur.fetchone()
        
    def _get_last_lifecycle(self, actor_id: str) -> dict | None:
        """
        Возвращает последнюю запись из state.agent_lifecycle для данного актора
        (независимо от ended_at).
        """
        with psycopg2.connect(**self.db_config) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, state_type, started_at, ended_at
                    FROM state.agent_lifecycle
                    WHERE actor_id = %s
                    ORDER BY started_at DESC
                    LIMIT 1
                """, (actor_id,))
                return cur.fetchone()
    

    def _close_current_lifecycle(self, actor_id: str, reason: str, shutdown_id: str | None = None):
        """
        Завершает текущее активное состояние в state.agent_lifecycle.
        
        ВАЖНО: reason_change не обновляется, так как это причина ВХОДА в состояние.
        Она должна оставаться неизменной. Причина выхода фиксируется через shutdown_reason_id
        или как reason_change следующей записи.
        
        :param shutdown_id: ссылка на state.shutdown_reasons.id (опционально)
        """
        current = self._get_current_lifecycle(actor_id)
        if not current:
            return

        with psycopg2.connect(**self.db_config) as conn:
            current = self._get_current_lifecycle(actor_id)
            if not current: return
            with psycopg2.connect(**self.db_config) as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE state.agent_lifecycle
                        SET ended_at = %s, shutdown_reason_id = %s
                        WHERE id = %s
                    """, (datetime.now(timezone.utc), shutdown_id, current['id']))

    def _get_current_actor_id(self) -> str:
        """Возвращает actor_id текущего пользователя консоли."""
        import os, pwd
        console_user_id = f"console:{pwd.getpwuid(os.getuid()).pw_name}"
        with psycopg2.connect(**self.db_config) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT a.id
                    FROM users.actors a
                    JOIN users.actors_external_ids e ON a.id = e.actor_id
                    WHERE e.source = 'console' AND e.source_id = %s
                    LIMIT 1
                """, (console_user_id,))
                row = cur.fetchone()
                if not row:
                    raise RuntimeError("Actor not found for console user")
                return str(row[0])
        

    def _record_shutdown_reason(self, shutdown_type: str, actor_id: str) -> str:
        """
        Создаёт запись о причине выключения в state.shutdown_reasons.
        :param shutdown_type: значение из ENUM state.shutdown_type
        :param actor_id: UUID актора (пользователя)
        :return: UUID новой записи
        """
        with psycopg2.connect(**self.db_config) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    INSERT INTO state.shutdown_reasons (actor_id, shutdown_type, timestamp)
                    VALUES (%s, %s, %s) RETURNING id
                """, (actor_id, shutdown_type, datetime.now(timezone.utc)))
                row = cur.fetchone()
                logger.info(f"Recorded shutdown: type={shutdown_type}, actor={actor_id[:8]}")
                return str(row['id'])

    def _prompt_shutdown_reason(self) -> str:
        """
        Запрашивает у пользователя причину отключения через консоль.
        Использует sys.stdin для совместимости с prompt_toolkit.
        """
        print("\nAgent was offline. Please specify the reason:")
        reasons = {
            'maintenance':      'Scheduled equipment maintenance',
            'crash':            'Crash',
            'forced_shutdown':  'Forced shutdown',
            'user_absence':     'Long-term absence of the user',
            'agent_modification': 'Agent refinement and testing'
        }
        for i, (enum_val, desc) in enumerate(reasons.items(), start=1):
            print(f"  [{i}] {desc}")
        
        enum_list = list(reasons.keys())
        
        import sys
        while True:
            print("Your choice (1-5):  ", end=" ", flush=True)
            choice = sys.stdin.readline().strip()
            if choice.isdigit() and 1 <= int(choice) <= len(enum_list):
                selected_enum = enum_list[int(choice) - 1]
                logger.info(f"Selected shutdown reason: {selected_enum}")
                return selected_enum
            print("Invalid choice. Please try again.")

    def _insert_off_state(self, actor_id: str, started_at, ended_at, shutdown_id: str):
        with psycopg2.connect(**self.db_config) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO state.agent_lifecycle (
                        actor_id, state_type, started_at, ended_at,
                        reason_change, shutdown_reason_id, agent_version
                    ) VALUES (
                        %s, 'off', %s, %s, 'crash_recovery', %s, %s
                    )
                """, (actor_id, started_at, ended_at, shutdown_id, agent_version))
    
    def _create_phs_drift_task(self, drift_type: str, input_data: dict):
        """Создаёт задачу phs_baseline_drift напрямую через SQL."""
        with psycopg2.connect(**self.db_config) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT id FROM orchestrator.task_types WHERE type_name = 'phs_baseline_drift'")
                task_type_id = cur.fetchone()["id"]
                full_input = {"drift_type": drift_type, **input_data}
                cur.execute("""
                    INSERT INTO orchestrator.orchestrator_tasks (
                        task_type_id, input_data, priority, status, agent_version, created_at
                    ) VALUES (%s, %s, 0.3, 'pending', %s, NOW())
                """, (task_type_id, Json(full_input), agent_version))
                conn.commit()
    
    def _get_last_shutdown_reason(self, actor_id: str) -> Optional[Dict[str, Any]]:
        """Возвращает последнюю запись из state.shutdown_reasons для актора."""
        with psycopg2.connect(**self.db_config) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT shutdown_type, timestamp
                    FROM state.shutdown_reasons
                    WHERE actor_id = %s
                    ORDER BY timestamp DESC
                    LIMIT 1
                """, (actor_id,))
                return cur.fetchone()

    def handle_startup(self):
        """Вызывается при запуске main.py. Обрабатывает восстановление после неграциозного завершения."""
        logger.info("Starting pseudohormonal lifecycle recovery...")
        actor_id = self._get_current_actor_id()

        # === ШАГ 0: Инициализация baseline (если ещё не создан) ===
        from phs_service.baseline_manager import BaselineManager
        baseline_mgr = BaselineManager(self.db_config)
        baseline_mgr.ensure_baseline_initialized()

        # 1. Найти последнее состояние ДЛЯ ЭТОГО АКТОРА
        last = self._get_last_lifecycle(actor_id)
        
        if last is None:
            # Первый запуск – сразу создаём active
            self._start_new_lifecycle(actor_id, 'startup', 'active')
            return

        # 2. Определяем, было ли незакрытое active (крэш) или штатное off
        #    Вычисляем время начала простоя (downtime_start) и причину
        if last['state_type'] == 'active' and last['ended_at'] is None:
            # Оборванная сессия (крэш)
            downtime_start = last['started_at']
            is_crash = True
        elif last['state_type'] == 'off':
            # Штатное выключение: downtime_start = ended_at (если есть) или started_at
            downtime_start = last['ended_at'] or last['started_at']
            is_crash = False
        else:
            # Неожиданное состояние (например, sleep) – обрабатываем как обычный старт
            downtime_start = last['started_at']
            is_crash = False

        downtime_duration = (datetime.now(timezone.utc) - downtime_start).total_seconds()

        # 3. Обработка краша (если есть незакрытое active)
        if is_crash:
            logger.warning("Detected unclean shutdown. Prompting for downtime reason.")
            shutdown_type = self._prompt_shutdown_reason()
            shutdown_id = self._record_shutdown_reason(shutdown_type, actor_id)

            # Применяем коррекцию baseline СРАЗУ
            baseline_mgr.apply_offline_drift(shutdown_type, downtime_duration, step_id=None)

            # Закрываем зависшее активное состояние
            self._close_current_lifecycle(actor_id, 'crash_recovery', shutdown_id)

            # Вставляем состояние 'off' задним числом
            self._insert_off_state(actor_id, downtime_start, datetime.now(timezone.utc), shutdown_id)

            # Создаём новое 'active'
            self._start_new_lifecycle(actor_id, 'startup', 'active')
            return

        # 4. Штатное выключение (последнее состояние было off)
        if last['state_type'] == 'off':
            # Если off не закрыт (rare), закрываем
            if last['ended_at'] is None:
                self._close_current_lifecycle(actor_id, 'startup', None)
            
            # ВСЕГДА проверяем последнюю причину выключения (она могла быть записана)
            last_shutdown = self._get_last_shutdown_reason(actor_id)
            if last_shutdown:
                shutdown_type = last_shutdown['shutdown_type']
                baseline_mgr.apply_offline_drift(shutdown_type, downtime_duration, step_id=None)
            
            self._start_new_lifecycle(actor_id, 'startup', 'active')
            return

        # 5. Любое другое состояние (например, sleep) – просто активируем
        self._start_new_lifecycle(actor_id, 'startup', 'active')

    def handle_graceful_shutdown(self, exit_reason: str):
        """
        Вызывается при выходе через exit / Ctrl+D.
        :param exit_reason: причина из dialogs.sessions.reason (user_command / user_exit)
        """
        logger.info(f"Handling graceful shutdown (reason: {exit_reason})...")
        actor_id = self._get_current_actor_id()

        shutdown_type = self._prompt_shutdown_reason()
        shutdown_id = self._record_shutdown_reason(shutdown_type, actor_id)

        # Закрываем active (только ended_at)
        self._close_current_lifecycle(actor_id, shutdown_id)

        # Создаём off и ПИШЕМ shutdown_reason_id ТУДА, как требует схема
        self._start_new_lifecycle(actor_id, 'shutdown_command', 'off', shutdown_id)
        logger.info("Graceful shutdown completed.")

    
    def _start_new_lifecycle(self, actor_id: str, reason: str, state_type: str = 'active', shutdown_id: str | None = None):
        """
        Создаёт новую запись в state.agent_lifecycle.
        :param actor_id: UUID актора (владельца консоли)
        :param reason: причина из ENUM state.lifecycle_change_reason
        :param state_type: состояние из ENUM state.agent_state_type (по умолчанию 'active')
        """
        with psycopg2.connect(**self.db_config) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO state.agent_lifecycle (
                        actor_id, state_type, reason_change, shutdown_reason_id, agent_version
                    ) VALUES (
                        %s, %s::state.agent_state_type, %s::state.lifecycle_change_reason, %s, %s
                    )
                """, (actor_id, state_type, reason, shutdown_id, agent_version))
                logger.info(f"Started new lifecycle for actor {actor_id[:8]}: {state_type} ({reason})")