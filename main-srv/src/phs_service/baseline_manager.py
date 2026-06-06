"""
phs_service/baseline_manager.py
Менеджер долговременного гормонального фона (baseline).

Функции:
- Инициализация baseline при холодном старте.
- Чтение и обновление активной записи.
- Применение естественного дрейфа (OU-процесс с возвратом к уставке).
- Защита от выхода за физиологические границы.
"""

import logging
import random
import psycopg2
from typing import Dict, Any, Optional
from psycopg2.extras import RealDictCursor

# Локальные импорты
from phs_service.vector_encoder import HormonalVectorEncoder
from phs_service.valence_calculator import compute_valence
from version import __version__ as agent_version

logger = logging.getLogger(__name__)


class BaselineManager:
    def __init__(self, db_config: Dict[str, Any]):
        self.db_config = db_config
        self.encoder = HormonalVectorEncoder(db_config)
        self.setpoints: Dict[str, float] = {}
        self.mins: Dict[str, float] = {}
        self.alpha = 0.0
        self.noise = 0.0
        self._load_settings()
        logger.debug("BaselineManager initialized.")

    def _load_settings(self):
        required_params = [
            "cortisol_setpoint", "dopamine_setpoint", "oxytocin_setpoint",
            "min_cortisol", "min_dopamine", "min_oxytocin",
            "alpha_hourly_drift", # остаётся для будущего осаждения
            "baseline_drift_noise",
            "baseline_ou_speed",
        ]
        
        with psycopg2.connect(**self.db_config) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT param_name, value_float 
                    FROM state.settings 
                    WHERE param_name = ANY(%s)
                """, (required_params,))
                settings = {row["param_name"]: row["value_float"] for row in cur.fetchall()}

        missing = [p for p in required_params if p not in settings or settings[p] is None]
        if missing:
            raise RuntimeError(f"Missing required settings in state.settings: {missing}")

        self.setpoints = {
            "cortisol": float(settings["cortisol_setpoint"]),
            "dopamine": float(settings["dopamine_setpoint"]),
            "oxytocin": float(settings["oxytocin_setpoint"]),
        }
        self.mins = {
            "min_cortisol": float(settings["min_cortisol"]),
            "min_dopamine": float(settings["min_dopamine"]),
            "min_oxytocin": float(settings["min_oxytocin"]),
        }
        self.alpha = float(settings["alpha_hourly_drift"])
        self.noise = float(settings["baseline_drift_noise"])
        self.ou_speed = float(settings["baseline_ou_speed"])

    def ensure_baseline_initialized(self) -> bool:
        current = self.get_current_baseline()
        if current:
            return False

        logger.info("Initializing baseline from setpoints (cold_start)...")
        hormones = {
            "cortisol": self.setpoints["cortisol"],
            "dopamine": self.setpoints["dopamine"],
            "oxytocin": self.setpoints["oxytocin"],
        }
        valence = compute_valence(**hormones)
        vector = self.encoder.encode(**hormones, valence=valence)
        self._insert_baseline(
            hormones["cortisol"], hormones["dopamine"], hormones["oxytocin"],
            valence, vector, "cold_start", step_id=None
        )
        return True

    def get_current_baseline(self) -> Optional[Dict[str, Any]]:
        with psycopg2.connect(**self.db_config) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, cortisol, dopamine, oxytocin, valence
                    FROM state.baseline_phs WHERE is_active = TRUE LIMIT 1
                """)
                return cur.fetchone()

    def handle_drift_task(self, task_id: str, input_data: dict):
        from services.service_metrics import (
            create_orchestrator_step, complete_step_success,
            complete_task_success, complete_task_error
        )

        try:
            drift_type = input_data.get("drift_type")
            
            # Шаг создаётся строго внутри. Никаких внешних step_id.
            # step_id передаётся извне для связывания с baseline
            step_id = create_orchestrator_step(task_id, 1, "phs_baseline_drift", input_data)

            if drift_type == "hourly":
                result = self.apply_natural_drift(step_id=step_id)
                output = {"baseline_id": result.get("baseline_id"), "drift_type": "hourly"}
                
            elif drift_type == "offline":
                shutdown_type = input_data.get("shutdown_type")
                downtime_sec = input_data.get("downtime_sec", 0)
                result = self.apply_offline_drift(shutdown_type, downtime_sec, step_id=step_id)
                output = {
                "baseline_id": result.get("baseline_id"),
                "drift_type": "offline",
                "shutdown_type": shutdown_type,
                "downtime_hours": round(downtime_sec / 3600, 2)
                }
            else:
                raise ValueError(f"Unknown drift_type: {drift_type}")

            complete_step_success(step_id, output)
            complete_task_success(task_id, output)
            logger.info(f"PHS drift task completed: baseline_id={output.get('baseline_id')}")

        except Exception as e:
            logger.exception("PHS drift task failed")
            complete_task_error(task_id, "phs_service", str(e))

    def apply_natural_drift(self, step_id: Optional[str] = None) -> Dict[str, Any]:
        current = self.get_current_baseline()
        if not current:
            return {"applied": False, "error": "no_baseline"}

        before_id = str(current["id"])
        cort, dopa, oxy = current["cortisol"], current["dopamine"], current["oxytocin"]

        new_hormones = {}
        for h in ["cortisol", "dopamine", "oxytocin"]:
            h_old = current[h]
            setpoint = self.setpoints[h]
            min_key = f"min_{h}"
            min_val = self.mins[min_key]
            drift = self.ou_speed * (setpoint - h_old)
            noise_term = self.noise * random.gauss(0, 1)
            h_new = max(min_val, min(100.0, h_old + drift + noise_term))
            new_hormones[h] = h_new

        valence = compute_valence(**new_hormones)
        vector = self.encoder.encode(**new_hormones, valence=valence)
        
        after_id = self._insert_baseline(
            new_hormones["cortisol"], new_hormones["dopamine"], new_hormones["oxytocin"],
            valence, vector, "hourly_drift", step_id=step_id
        )
        
        return {"applied": True, "baseline_id": after_id}

    def _map_shutdown_to_reason(self, shutdown_type: str) -> str:
        mapping: Dict[str, str] = {
            'maintenance': 'shutdown_maintenance',
            'crash': 'shutdown_crash',
            'forced_shutdown': 'shutdown_forced',
            'user_absence': 'shutdown_absence',
            'agent_modification': 'shutdown_agent_modification',
        }
        reason_code = mapping.get(shutdown_type)
        if reason_code is None:
            raise RuntimeError(f"Unknown shutdown_type: '{shutdown_type}'. Cannot map to reason_code.")
        return reason_code

    def apply_offline_drift(self, shutdown_type: Optional[str], downtime_sec: float, step_id: Optional[str] = None) -> Dict[str, Any]:
        logger.info(f"=== OFFLINE DRIFT CALLED: type={shutdown_type}, downtime_sec={downtime_sec:.2f} (hours={downtime_sec/3600:.2f}) ===")
        if shutdown_type is None:
            logger.warning("No shutdown_type provided for offline drift")
            return {"applied": False, "error": "no_shutdown_type"}

        current = self.get_current_baseline()
        if not current:
            logger.warning("No active baseline found for drift.")
            return {"applied": False, "error": "no_baseline"}

        before_id = str(current["id"])
        cort, dopa, oxy = current["cortisol"], current["dopamine"], current["oxytocin"]

        def get_setting_strict(name: str) -> float:
            with psycopg2.connect(**self.db_config) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT value_float FROM state.settings WHERE param_name = %s", (name,))
                    row = cur.fetchone()
                    if not row or row[0] is None:
                        raise RuntimeError(f"Missing required setting in state.settings: '{name}'")
                    return float(row[0])

        hours = downtime_sec / 3600.0

        if shutdown_type == "crash":
            cort *= 1.15
            dopa *= 0.90
            logger.info(f"Crash: immediate shock applied. cortisol +15%, dopamine -10%")

        elif shutdown_type == "user_absence":
            max_hours = get_setting_strict("absence_max_effect_hours")
            depression_factor = min(hours / max_hours, 1.0)
            dopa_delta = -20.0 * depression_factor
            oxy_delta = -15.0 * depression_factor
            cort_delta = 10.0 * depression_factor
            dopa *= (1.0 + dopa_delta / 100.0)
            oxy *= (1.0 + oxy_delta / 100.0)
            cort *= (1.0 + cort_delta / 100.0)

        elif shutdown_type == "agent_modification":
            dopa *= 1.10
            oxy *= 1.08

        elif shutdown_type == "forced_shutdown":
            cort *= 1.05
        
        # maintenance и неизвестные -> нейтрально

        cort = max(self.mins["min_cortisol"], min(100.0, cort))
        dopa = max(self.mins["min_dopamine"], min(100.0, dopa))
        oxy = max(self.mins["min_oxytocin"], min(100.0, oxy))

        valence = compute_valence(cort, dopa, oxy)
        vector = self.encoder.encode(cort, dopa, oxy, valence)
        
        reason_code = self._map_shutdown_to_reason(shutdown_type)
        
        after_id = self._insert_baseline(cort, dopa, oxy, valence, vector, reason_code, step_id=step_id)
        
        return {"applied": True, "baseline_id": after_id}

    def _insert_baseline(self, cortisol, dopamine, oxytocin, valence, vector, reason_code: str, step_id: Optional[str] = None) -> str:
        with psycopg2.connect(**self.db_config) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM state.baseline_change_reasons WHERE reason_code = %s", (reason_code,))
                row = cur.fetchone()
                if not row:
                    raise RuntimeError(f"Reason code '{reason_code}' not found in state.baseline_change_reasons.")
                reason_id = row[0]

                cur.execute("UPDATE state.baseline_phs SET is_active = FALSE WHERE is_active = TRUE")
                cur.execute("""
                    INSERT INTO state.baseline_phs (
                        cortisol, dopamine, oxytocin, valence,
                        state_vector, change_reason_id, is_active, agent_version, orchestrator_step_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, TRUE, %s, %s)
                    RETURNING id
                """, (cortisol, dopamine, oxytocin, valence, vector, reason_id, agent_version, step_id))
                new_id = cur.fetchone()[0]
                conn.commit()
                return str(new_id)