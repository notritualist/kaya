"""
main-srv/src/phs_service/baseline_manager.py

Long-term Hormonal Background (Baseline) Manager.

Functions:
- Initialize baseline on cold start from setpoints (cortisol=50, dopamine=30, oxytocin=20).
- Apply natural drift via Ornstein-Uhlenbeck process:
    new = old + ou_speed * (setpoint - old) + noise.
- Enforce physiological boundaries [min_*, 100.0] for all hormones.
- Create NEW baseline records on every change (immutable history), deactivating the previous active record.
- Automatic state classification: computes state_id via RFF vector cosine similarity to self_knowledge prototypes.
- Handles offline drift on startup based on shutdown_type and downtime duration.
- Full traceability: all operations return baseline_id_before and baseline_id_after in output_data.

Architecture:
- Single active baseline record at any time (enforced by is_active flag).
- All mutations are INSERT+UPDATE (never UPDATE-in-place) to preserve state evolution history.
- Integrates with MomentaryManager for hourly sedimentation of momentary experience.
"""

version = "1.2.1"
description = "Hormonal Background (Baseline) Manager"

import logging
import random
import psycopg2
from typing import Dict, Any, Optional
from psycopg2.extras import RealDictCursor

# Локальные импорты
from phs_service.vector_encoder import HormonalVectorEncoder
from phs_service.valence_calculator import compute_valence
from phs_service.state_classifier import StateClassifier
from version import __version__ as agent_version

logger = logging.getLogger(__name__)


class BaselineManager:
    """
    Менеджер долговременного гормонального фона.
    
    Отвечает за инициализацию, дрейф и осаждение baseline.
    Все изменения создают новые записи, возвращают IDs до/после.
    """
   
    def __init__(self, db_config: Dict[str, Any]):
        """
        Инициализация менеджера baseline.
        
        Args:
            db_config: Параметры подключения к PostgreSQL.
        """
        self.db_config = db_config
        self.agent_version = agent_version
        self.encoder = HormonalVectorEncoder(db_config)
        self.classifier = StateClassifier(db_config) 
        self.setpoints: Dict[str, float] = {}
        self.mins: Dict[str, float] = {}
        self.alpha = 0.0
        self.noise = 0.0
        self.ou_speed = 0.0
        self._load_settings()
        logger.debug("BaselineManager initialized.")

    def _load_settings(self):
        required_params = [
            "cortisol_setpoint", "dopamine_setpoint", "oxytocin_setpoint",
            "min_cortisol", "min_dopamine", "min_oxytocin",
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
        self.noise = float(settings["baseline_drift_noise"])
        self.ou_speed = float(settings["baseline_ou_speed"])

    def _get_setting_float(self, param_name: str, default: float = 0.0) -> float:
        """
        Получает числовое значение параметра из state.settings.
        
        Args:
            param_name: Имя параметра.
            default: Значение по умолчанию.
            
        Returns:
            float: Значение параметра или default.
        """
        with psycopg2.connect(**self.db_config) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT value_float FROM state.settings WHERE param_name = %s",
                    (param_name,)
                )
                row = cur.fetchone()
                if not row or row[0] is None:
                    return default
                return float(row[0])
    
    def ensure_baseline_initialized(self) -> bool:
        """Инициализирует baseline при холодном старте."""
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
        """
        Возвращает активную запись baseline со всеми полями.
        
        Returns:
            dict | None: Словарь с полями baseline или None.
        """
        with psycopg2.connect(**self.db_config) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, cortisol, dopamine, oxytocin, valence, state_vector
                    FROM state.baseline_phs WHERE is_active = TRUE LIMIT 1
                """)
                return cur.fetchone()

    def handle_drift_task(self, task_id: str, input_data: dict):
        """
        Обрабатывает задачу дрейфа baseline.
        
        Для drift_type='hourly' выполняет:
        1. Естественный дрейф (OU-процесс).
        2. Осаждение momentary в baseline (если alpha_hourly_drift > 0).
        
        Возвращает в output_data:
        - baseline_id_before: ID baseline до дрейфа.
        - baseline_id_after: ID baseline после осаждения.
        """
        from services.service_metrics import (
            create_orchestrator_step, complete_step_success,
            complete_task_success, complete_task_error
        )

        step_id = create_orchestrator_step(task_id, 1, "phs_baseline_drift", input_data)
        
        try:
            drift_type = input_data.get("drift_type")
            
            if drift_type == "hourly":
                # 1. Естественный дрейф
                drift_result = self.apply_natural_drift(step_id=step_id)
                baseline_before = drift_result.get("baseline_id_before")
                baseline_after_drift = drift_result.get("baseline_id_after")
                
                # 2. Осаждение momentary в baseline
                # Получаем actor_id из активного momentary
                actor_id = None
                with psycopg2.connect(**self.db_config) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT actor_id FROM state.momentary WHERE is_active = TRUE LIMIT 1")
                        row = cur.fetchone()
                        if row:
                            actor_id = str(row[0])
                
                sediment_result = None
                baseline_after_sediment = baseline_after_drift
                if actor_id:
                    sediment_result = self.apply_hourly_sedimentation(actor_id=actor_id, step_id=step_id)
                    if sediment_result and sediment_result.get("applied"):
                        baseline_after_sediment = sediment_result.get("baseline_id_after")
                
                # ← ИСПРАВЛЕНО: полная трассировка
                output = {
                    "drift_type": "hourly",
                    "baseline_id_before": baseline_before,
                    "baseline_id_after": baseline_after_sediment,
                    "drift_applied": drift_result.get("applied", False),
                    "sedimentation_applied": sediment_result.get("applied", False) if sediment_result else False,
                    "momentary_id": sediment_result.get("momentary_id") if sediment_result else None,
                }
                
            elif drift_type == "offline":
                shutdown_type = input_data.get("shutdown_type")
                downtime_sec = input_data.get("downtime_sec", 0)
                result = self.apply_offline_drift(shutdown_type, downtime_sec, step_id=step_id)
                
                # ← ИСПРАВЛЕНО: полная трассировка
                output = {
                    "drift_type": "offline",
                    "baseline_id_before": result.get("baseline_id_before"),
                    "baseline_id_after": result.get("baseline_id_after"),
                    "shutdown_type": shutdown_type,
                    "downtime_hours": round(downtime_sec / 3600, 2)
                }
            else:
                raise ValueError(f"Unknown drift_type: {drift_type}")
            
            complete_step_success(step_id, output)
            complete_task_success(task_id, output)
            logger.info("PHS drift task completed: %s", output)
            
        except Exception as e:
            logger.exception("PHS drift task failed")
            complete_task_error(task_id, "phs_service", str(e))
            raise

    def apply_natural_drift(self, step_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Применяет естественный дрейф baseline (OU-процесс).
        
        Возвращает ID записи до и после изменения для трассировки.
        
        Args:
            step_id: UUID шага оркестратора.
            
        Returns:
            dict: Результат с baseline_id_before и baseline_id_after.
        """
        current = self.get_current_baseline()
        if not current:
            return {"applied": False, "error": "no_baseline"}
        
        before_id = str(current["id"])
        
        # Вычисление новых значений
        new_hormones = {}
        for h in ["cortisol", "dopamine", "oxytocin"]:
            h_old = current[h]
            setpoint = self.setpoints[h]
            min_val = self.mins[f"min_{h}"]
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
        
        return {
            "applied": True,
            "baseline_id_before": before_id,
            "baseline_id_after": after_id
        }

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

    def apply_hourly_sedimentation(self, actor_id: str, step_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Применяет ежечасное осаждение momentary в baseline.
        
        Использует ФИКСИРОВАННЫЙ коэффициент alpha_hourly_drift из настроек,
        чтобы не перекрывать естественный дрейф baseline к уставкам.
        """
        alpha = self._get_setting_float("alpha_hourly_drift", default=0.05)
        if alpha <= 0.0:
            return {"applied": False, "reason": "alpha_hourly_drift is zero"}
        
        from phs_service.momentary_manager import MomentaryManager
        momentary_mgr = MomentaryManager(self.db_config)
        result = momentary_mgr.sediment_momentary_to_baseline(
            actor_id=actor_id, alpha=alpha, reason_code="hourly_sedimentation"
        )
        
        if result:
            result["applied"] = True
            return result
        return {"applied": False, "reason": "no active momentary"}
        
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
        
        return {
        "applied": True,
        "baseline_id_before": before_id,
        "baseline_id_after": after_id
        }

    def _insert_baseline(
        self,
        cortisol: float,
        dopamine: float,
        oxytocin: float,
        valence: float,
        vector: list,
        reason_code: str,
        step_id: Optional[str] = None
    ) -> str:
        """
        Вставляет новую запись baseline с автоматической классификацией состояния.
        
        Вычисляет state_id через классификатор и сохраняет связь с прототипом.
        Деактивирует предыдущую активную запись.
        
        Args:
            cortisol, dopamine, oxytocin: Уровни гормонов [0..100].
            valence: Валентность [-100..100].
            vector: Вектор состояния (128 float).
            reason_code: Код причины изменения baseline.
            step_id: UUID шага оркестратора (опционально).
            
        Returns:
            str: UUID новой записи baseline.
        """
        # Классифицируем состояние
        state_match = self.classifier.classify_vector(vector)
        state_id = state_match.state_id
        
        logger.debug(
            f"Baseline classified: state={state_match.state_code}, "
            f"confidence={state_match.confidence:.2f}"
        )
        
        with psycopg2.connect(**self.db_config) as conn:
            with conn.cursor() as cur:
                # Получаем reason_id
                cur.execute(
                    "SELECT id FROM state.baseline_change_reasons WHERE reason_code = %s",
                    (reason_code,)
                )
                row = cur.fetchone()
                if not row:
                    raise RuntimeError(f"Reason code '{reason_code}' not found.")
                reason_id = row[0]
                
                # Деактивируем текущий baseline
                cur.execute(
                    "UPDATE state.baseline_phs SET is_active = FALSE WHERE is_active = TRUE"
                )
                
                # Вставляем новый с state_id
                cur.execute(
                    """
                    INSERT INTO state.baseline_phs (
                        cortisol, dopamine, oxytocin, valence, state_vector,
                        change_reason_id, state_id, is_active, agent_version, orchestrator_step_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, %s, %s)
                    RETURNING id
                    """,
                    (
                        cortisol, dopamine, oxytocin, valence, vector,
                        reason_id, state_id, agent_version, step_id
                    )
                )
                new_id = str(cur.fetchone()[0])
                conn.commit()
        
        return new_id