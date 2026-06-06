"""
phs_service/phs_scheduler.py
Планировщик фоновых задач ПГС: ежечасный дрейф baseline.
Запускается один раз при старте агента.
"""
import threading
import time
import logging
import psycopg2
from typing import Optional
from datetime import datetime, timezone
from db_manager.db_manager import load_postgres_config
from psycopg2.extras import Json, RealDictCursor
# Import global agent version from pyproject.toml
from version import __version__ as agent_version

logger = logging.getLogger(__name__)

class PHSScheduler:
    def __init__(self):
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_drift_check = datetime.now(timezone.utc)
        self._drift_interval_sec = self._load_drift_interval()

    def _load_drift_interval(self) -> int:
        db_config = load_postgres_config()
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT value_float FROM state.settings 
                    WHERE param_name = 'phs_hourly_drift_interval_sec'
                """)
                row = cur.fetchone()
                if not row or row[0] is None:
                    raise RuntimeError("Missing 'phs_hourly_drift_interval_sec' in state.settings")
                return int(row[0])

    def _get_active_baseline_id(self, db_config: dict) -> Optional[str]:
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM state.baseline_phs WHERE is_active = TRUE LIMIT 1")
                row = cur.fetchone()
                return str(row[0]) if row else None

    def _scheduler_loop(self):
        db_config = load_postgres_config()
        while self._running:
            try:
                now = datetime.now(timezone.utc)
                elapsed = (now - self._last_drift_check).total_seconds()
                
                # Если прошло достаточно времени -> создаём задачу
                if elapsed >= self._drift_interval_sec:
                    with psycopg2.connect(**db_config) as conn:
                        with conn.cursor(cursor_factory=RealDictCursor) as cur:
                            cur.execute("SELECT id FROM orchestrator.task_types WHERE type_name = 'phs_baseline_drift'")
                            row = cur.fetchone()
                            if not row:
                                raise RuntimeError("Task type 'phs_baseline_drift' not found in orchestrator.task_types")
                            task_type_id = row["id"]
                            
                            baseline_id = self._get_active_baseline_id(db_config)
                            input_data = {"drift_type": "hourly"}
                            if baseline_id:
                                input_data["baseline_id"] = baseline_id
                                
                            cur.execute("""
                                INSERT INTO orchestrator.orchestrator_tasks (
                                    task_type_id, input_data, priority, status, agent_version, created_at
                                ) VALUES (%s, %s, 0.3, 'pending', %s, NOW())
                            """, (task_type_id, Json(input_data), agent_version))
                            conn.commit()
                    
                    self._last_drift_check = now
                    logger.info(f"PHS drift scheduled. Next run in {self._drift_interval_sec} sec")
                    
                    # 🔑 ЖДЁМ ровно столько, сколько указано в БД
                    time.sleep(self._drift_interval_sec)
                else:
                    # ⏱ Если время не пришло, спим коротко (2 сек), чтобы быстро реагировать и не грузить CPU
                    time.sleep(2)
                    
            except Exception as e:
                logger.exception("PHS scheduler error")
                time.sleep(2) # При ошибке не блокируем цикл надолго

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._scheduler_loop, daemon=True, name="PHS-Scheduler")
        self._thread.start()
        logger.info("PHS scheduler started")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        logger.info("PHS scheduler stopped")