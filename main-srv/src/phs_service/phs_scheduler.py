"""
main-srv/src/phs_service/phs_scheduler.py

PHS Background Task Scheduler.

Features:
- Schedules phs_baseline_drift tasks at phs_hourly_drift_interval_sec.
- Schedules phs_momentary_decay tasks at momentary_decay_interval_sec.
- Runs as daemon thread started from main.py.
- Intervals loaded from state.settings.

Architecture:
- Scheduler loop checks elapsed time for each task type independently.
- Creates tasks via orchestrator_entry.schedule_* functions (no direct SQL).
- No blocking sleeps inside conditionals: loop sleeps short interval (2 sec)
  to remain responsive to both drift and decay schedules.
"""

version = "1.2.0"
description = "PHSScheduler"


import threading
import time
import logging
import psycopg2
from typing import Optional
from datetime import datetime, timezone
from db_manager.db_manager import load_postgres_config
# Import global agent version from pyproject.toml
from version import __version__ as agent_version

logger = logging.getLogger(__name__)

class PHSScheduler:
    """
    Планировщик фоновых задач ПГС.
    
    Отвечает за создание задач дрейфа baseline и затухания momentary.
    Запускается один раз при старте агента.
    Не работает с SQL напрямую — использует orchestrator_entry.
    """
    
    def __init__(self):
        """Инициализация планировщика."""
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_drift_check = datetime.now(timezone.utc)
        self._drift_interval_sec = self._load_drift_interval()
        self._last_decay_check = datetime.now(timezone.utc)
        self._decay_interval_sec = self._load_decay_interval()
    
    def _load_decay_interval(self) -> int:
        """Загружает интервал затухания momentary из state.settings."""
        with psycopg2.connect(**load_postgres_config()) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value_float FROM state.settings WHERE param_name = 'momentary_decay_interval_sec'")
                row = cur.fetchone()
                return int(row[0]) if row and row[0] else 60
    
    def _load_drift_interval(self) -> int:
        """Загружает интервал дрейфа baseline из state.settings."""
        db_config = load_postgres_config()
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT value_float FROM state.settings WHERE param_name = 'phs_hourly_drift_interval_sec'
                """)
                row = cur.fetchone()
                if not row or row[0] is None:
                    raise RuntimeError("Missing 'phs_hourly_drift_interval_sec' in state.settings")
                return int(row[0])
    
    def _get_active_baseline_id(self, db_config: dict) -> Optional[str]:
        """Возвращает ID активного baseline или None."""
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM state.baseline_phs WHERE is_active = TRUE LIMIT 1")
                row = cur.fetchone()
                return str(row[0]) if row else None
    
    def _scheduler_loop(self):
        """
        Основной цикл планировщика.
        
        Логика:
        1. Вычисляет elapsed для drift и decay независимо.
        2. Если elapsed >= интервал — вызывает schedule_* из orchestrator_entry.
        3. Спит коротко (2 сек), чтобы не блокировать проверки.
        
        Важно: никаких длинных sleep внутри if — это блокировало бы другие проверки.
        """
        db_config = load_postgres_config()
        while self._running:
            try:
                now = datetime.now(timezone.utc)
                elapsed_drift = (now - self._last_drift_check).total_seconds()
                elapsed_decay = (now - self._last_decay_check).total_seconds()
                
                # === ДРЕЙФ BASELINE ===
                if elapsed_drift >= self._drift_interval_sec:
                    from orchestrator.orchestrator_entry import schedule_phs_baseline_drift
                    
                    baseline_id = self._get_active_baseline_id(db_config)
                    schedule_phs_baseline_drift(
                        drift_type="hourly",
                        baseline_id=baseline_id,
                        priority=0.3
                    )
                    
                    self._last_drift_check = now
                    logger.info(f"PHS drift scheduled. Next run in {self._drift_interval_sec} sec")
                
                # === ЗАТУХАНИЕ MOMENTARY ===
                if elapsed_decay >= self._decay_interval_sec:
                    from orchestrator.orchestrator_entry import schedule_phs_momentary_decay
                    
                    schedule_phs_momentary_decay(
                        decay_type="natural",
                        priority=0.4
                    )
                    
                    self._last_decay_check = now
                    logger.debug("PHS momentary decay task scheduled.")
                
                # Короткий сон для отзывчивости
                time.sleep(2)
                
            except Exception as e:
                logger.exception("PHS scheduler error")
                time.sleep(2)
        
    def start(self):
        """Запускает планировщик в фоновом потоке."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._scheduler_loop, daemon=True, name="PHS-Scheduler")
        self._thread.start()
        logger.info("PHS scheduler started")
    
    def stop(self):
        """Останавливает планировщик."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        logger.info("PHS scheduler stopped")