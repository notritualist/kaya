"""
main-srv/src/phs_service/momentary_manager.py

Manager for momentary hormonal state slices.

Features:
- Creates momentary slices from baseline at physical session start (one per session).
- Tracks origin: session_id, baseline_id, actor_id, state_id, dialog_id (auto-filled if active dialogue exists).
- Sediments momentary experience back to baseline in three scenarios:
    • session_end: on graceful shutdown (alpha = alpha_session_end from settings)
    • hourly: during phs_baseline_drift task (alpha = alpha_hourly_drift from settings)
    • crash: on startup after unclean shutdown (alpha = alpha_crash_recovery from settings)
- Handles decay via orchestrator task phs_momentary_decay:
    Formula: new = baseline + (momentary - baseline) * (1 - alpha_momentary_decay * exp(-dt/tau_hormone)) + noise.
    Each hormone decays at its own biological rate (tau_cortisol=3600s, tau_dopamine=180s, tau_oxytocin=600s).
- Manages dangling momentary cleanup on startup/crash recovery (deactivates orphaned records).
- All changes create NEW rows (immutable history), old rows deactivated via is_active=FALSE.
- Full traceability: output_data contains IDs before/after for all operations (momentary_id_before/after, baseline_id).

Architecture:
- One active momentary slice per actor at any time (enforced by is_active flag).
- Decay and sedimentation are separate processes:
    • Decay: momentary → baseline (continuous, every 60s)
    • Sedimentation: momentary → baseline (discrete events: session end/hourly/crash)
- Integrates with BaselineManager for sedimentation and state classification.
"""

version = "1.1.2"
description = "Momentary hormonal state manager"

import logging
import psycopg2
import random
from psycopg2.extras import RealDictCursor
from typing import Dict, Any, Optional

from phs_service.state_classifier import StateClassifier
from phs_service.baseline_manager import BaselineManager
from phs_service.valence_calculator import compute_valence
from phs_service.vector_encoder import HormonalVectorEncoder
from services.service_metrics import (
    create_orchestrator_step, complete_step_success,
    complete_task_success, complete_task_error
)
from version import __version__ as agent_version

logger = logging.getLogger(__name__)

# =============================================================================
# КОНСТАНТЫ ЛОГИКИ MOMENTARY
# =============================================================================

#: Код причины изменения baseline при осаждении из momentary.
REASON_SESSION_END: str = "session_end_sedimentation"
REASON_HOURLY_SEDIMENTATION: str = "hourly_sedimentation"
REASON_CRASH_SEDIMENTATION: str = "crash_sedimentation"


class MomentaryManager:
    """
    Менеджер моментальных срезов гормонального состояния.
    
    Отвечает за создание, обновление, затухание и осаждение momentary.
    Все изменения создают НОВЫЕ записи, старые деактивируются.
    Возвращает полную трассировку изменений (ID до/после).
    """

    def __init__(self, db_config: Dict[str, Any]):
        """
        Инициализация менеджера momentary.
        
        Args:
            db_config: Параметры подключения к PostgreSQL.
        """
        self.db_config = db_config
        self.agent_version = agent_version
        self.classifier = StateClassifier(db_config)
        self.baseline_mgr = BaselineManager(db_config)
        self.encoder = HormonalVectorEncoder(db_config)
        logger.debug("MomentaryManager initialized.")

    def _get_active_dialogue_id(self, actor_id: str) -> Optional[str]:
        """
        Возвращает UUID активного диалога для актора или None.
        """
        with psycopg2.connect(**self.db_config) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id FROM dialogs.dialogues
                    WHERE actor_id = %s AND status = 'active'
                    LIMIT 1
                """, (actor_id,))
                row = cur.fetchone()
                return str(row[0]) if row else None
    
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
    
    def create_momentary_from_baseline(
        self,
        session_id: str,
        actor_id: str
    ) -> str:
        """
        Создаёт momentary срез на основе актуального baseline.
        
        Вызывается при старте физической сессии.
        Копирует гормоны и вектор из baseline, классифицирует состояние,
        заполняет связи session_id, baseline_id, actor_id, state_id.
        Обновляет dialogs.sessions с baseline_id и state_id.
        
        Args:
            session_id: UUID физической сессии.
            actor_id: UUID пользователя (владельца сессии).
            
        Returns:
            str: UUID созданной записи momentary.
            
        Raises:
            RuntimeError: Если baseline не инициализирован.
        """
        baseline = self.baseline_mgr.get_current_baseline()
        if not baseline:
            raise RuntimeError(
                "Cannot create momentary: no active baseline found. "
                "Ensure baseline is initialized."
            )
        
        cort = baseline["cortisol"]
        dopa = baseline["dopamine"]
        oxy = baseline["oxytocin"]
        valence = baseline["valence"]
        vector = baseline["state_vector"]
        baseline_id = str(baseline["id"])
        
        state_match = self.classifier.classify_vector(vector)
        state_id = state_match.state_id
        
        logger.debug(
            f"Creating momentary for session={session_id[:8]}, actor={actor_id[:8]}. "
            f"Baseline={baseline_id[:8]}, state={state_match.state_code}, "
            f"confidence={state_match.confidence:.2f}"
        )
        
        with psycopg2.connect(**self.db_config) as conn:
            with conn.cursor() as cur:
                # Деактивируем предыдущий momentary для этого актора
                cur.execute(
                    """
                    UPDATE state.momentary
                    SET is_active = FALSE
                    WHERE actor_id = %s AND is_active = TRUE
                    """,
                    (actor_id,)
                )
                
                # Получаем event_type_id для agent_start
                cur.execute(
                    "SELECT id FROM state.delta_reasons WHERE event_type_code = %s",
                    ("agent_start",)
                )
                row = cur.fetchone()
                event_type_id = row[0] if row else None
                dialog_id = self._get_active_dialogue_id(actor_id)

                # Вставляем новый momentary
                cur.execute("""
                    INSERT INTO state.momentary (
                        session_id, baseline_id, actor_id, dialog_id,
                        cortisol, dopamine, oxytocin, valence, state_vector,
                        state_id, event_type_id, is_active, agent_version
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, %s)
                    RETURNING id
                """, (
                    session_id, baseline_id, actor_id, dialog_id,  # ← dialog_id добавлен
                    cort, dopa, oxy, valence, vector,
                    state_id, event_type_id, self.agent_version
                ))
                momentary_id = str(cur.fetchone()[0])
                
                # Обновляем сессию с baseline_id и state_id
                cur.execute(
                    """
                    UPDATE dialogs.sessions
                    SET baseline_id = %s, state_id = %s
                    WHERE id = %s
                    """,
                    (baseline_id, state_id, session_id)
                )
                
                conn.commit()
        
        logger.info(
            f"Created momentary={momentary_id[:8]} from baseline={baseline_id[:8]} "
            f"for session={session_id[:8]}"
        )
        return momentary_id


    def sediment_momentary_to_baseline(
        self,
        actor_id: str,
        alpha: float,
        reason_code: str
    ) -> Optional[Dict[str, Any]]:
        """
        Осаждает momentary в baseline с заданным коэффициентом.
        
        Args:
            actor_id: UUID пользователя.
            alpha: Коэффициент осаждения [0..1] (из настроек или вычисленный).
            reason_code: Код причины изменения baseline.
            
        Returns:
            dict | None: Словарь с трассировкой или None.
        """
        if alpha <= 0.0:
            return None
        
        # Получаем активный momentary
        with psycopg2.connect(**self.db_config) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, cortisol, dopamine, oxytocin
                    FROM state.momentary
                    WHERE actor_id = %s AND is_active = TRUE
                    LIMIT 1
                    """,
                    (actor_id,)
                )
                momentary = cur.fetchone()
        
        if not momentary:
            logger.debug(f"No active momentary found for actor={actor_id[:8]}")
            return None
        
        baseline = self.baseline_mgr.get_current_baseline()
        if not baseline:
            logger.warning("No active baseline for sedimentation.")
            return None
        
        before_id = str(baseline["id"])
        momentary_id = str(momentary["id"])
        
        # Вычисляем новые значения
        new_hormones = {}
        for h in ["cortisol", "dopamine", "oxytocin"]:
            m_val = momentary[h]
            b_val = baseline[h]
            new_val = b_val + alpha * (m_val - b_val)
            new_hormones[h] = max(0.0, min(100.0, new_val))
        
        new_valence = compute_valence(**new_hormones)
        new_vector = self.encoder.encode(**new_hormones, valence=new_valence)
        state_match = self.classifier.classify_vector(new_vector)
        
        logger.info(
            f"Sedimenting momentary→baseline for actor={actor_id[:8]}, alpha={alpha:.2f}. "
            f"Reason={reason_code}, new_state={state_match.state_code}"
        )
        
        # Вставляем новый baseline
        after_id = self._insert_baseline_with_state(
            cortisol=new_hormones["cortisol"],
            dopamine=new_hormones["dopamine"],
            oxytocin=new_hormones["oxytocin"],
            valence=new_valence,
            vector=new_vector,
            reason_code=reason_code,
            state_id=state_match.state_id
        )
        
        # Возвращаем полную трассировку
        return {
            "baseline_id_before": before_id,
            "baseline_id_after": after_id,
            "momentary_id": momentary_id,
            "actor_id": actor_id,
            "alpha": alpha
        }

    def sediment_all_active_momentaries(
        self,
        reason_code: str
    ) -> int:
        """
        Осаждает все активные momentary в baseline.
        
        Используется при crash recovery.
        
        Args:
            reason_code: Код причины изменения baseline.
            
        Returns:
            int: Количество обработанных momentary записей.
        """
        alpha = self._get_setting_float("alpha_crash_recovery", default=0.1)
        with psycopg2.connect(**self.db_config) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT DISTINCT actor_id
                    FROM state.momentary
                    WHERE is_active = TRUE
                    """
                )
                actors = [row["actor_id"] for row in cur.fetchall()]
        
        if not actors:
            logger.debug("No active momentaries to sediment.")
            return 0
        
        count = 0
        for actor_id in actors:
            result = self.sediment_momentary_to_baseline(actor_id, alpha, reason_code)
            if result:
                count += 1
        
        logger.info(f"Sedimented {count} momentaries with reason={reason_code}")
        return count
        
     
    def close_dangling_momentary(self) -> int:
        """
        Сбрасывает флаг is_active у всех зависших momentary.
        
        Вызывается при старте агента после обработки креша.
        Не удаляет данные, только деактивирует для предотвращения конфликтов.
        
        Returns:
            int: Количество сброшенных записей.
        """
        with psycopg2.connect(**self.db_config) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE state.momentary
                    SET is_active = FALSE
                    WHERE is_active = TRUE
                    """
                )
                count = cur.rowcount
                conn.commit()
        
        if count > 0:
            logger.warning(f"Closed {count} dangling momentary records on startup.")
        return count

    
    def _insert_baseline_with_state(
        self,
        cortisol: float,
        dopamine: float,
        oxytocin: float,
        valence: float,
        vector: list,
        reason_code: str,
        state_id: str
    ) -> str:
        """
        Вставляет новую запись baseline с явным state_id.
        
        Внутренний метод для использования из momentary_manager.
        Деактивирует предыдущий baseline и вставляет новый.
        
        Args:
            cortisol, dopamine, oxytocin: Уровни гормонов.
            valence: Валентность.
            vector: Вектор состояния.
            reason_code: Код причины изменения.
            state_id: UUID прототипа состояния.
            
        Returns:
            str: UUID новой записи baseline.
        """
        with psycopg2.connect(**self.db_config) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM state.baseline_change_reasons WHERE reason_code = %s",
                    (reason_code,)
                )
                row = cur.fetchone()
                if not row:
                    raise RuntimeError(f"Reason code '{reason_code}' not found.")
                reason_id = row[0]
                
                cur.execute(
                    "UPDATE state.baseline_phs SET is_active = FALSE WHERE is_active = TRUE"
                )
                
                cur.execute(
                    """
                    INSERT INTO state.baseline_phs (
                        cortisol, dopamine, oxytocin, valence, state_vector,
                        change_reason_id, state_id, is_active, agent_version
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, %s)
                    RETURNING id
                    """,
                    (
                        cortisol, dopamine, oxytocin, valence, vector,
                        reason_id, state_id, self.agent_version
                    )
                )
                new_id = str(cur.fetchone()[0])
                conn.commit()
        
        return new_id

    def handle_decay_task(self, task_id: str, input_data: dict) -> None:
        """
        Обрабатывает задачу затухания momentary.
        
        Логика:
        1. Создаёт шаг оркестратора.
        2. Вызывает apply_decay_tick для всех активных акторов.
        3. Завершает задачу с полным отчётом в output_data.
        """
        step_id = create_orchestrator_step(task_id, 1, "phs_momentary_decay", input_data)
        
        try:
            result = self.apply_decay_tick(step_id=step_id)
            complete_step_success(step_id, result)
            complete_task_success(task_id, result)
            logger.debug("PHS momentary decay completed: %d updates", len(result.get("updates", [])))
        except Exception as e:
            logger.exception("Momentary decay task failed")
            complete_task_error(task_id, "phs_service", str(e))
            raise

    def apply_decay_tick(self, step_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Применяет затухание momentary к baseline для всех активных акторов.
    
        Двойная динамика:
        1. alpha_momentary_decay (0.05) — глобальный коэффициент затухания.
        Определяет, насколько сильно momentary стремится к baseline за тик.
        2. tau_*_sec — индивидуальные времена биохимического распада гормонов:
        - Кортизол (tau=3600с): медленно, стресс долго держится
        - Дофамин (tau=180с): быстро, мотивация угасает за минуты
        - Окситоцин (tau=600с): средне, социальное доверие
        
        Формула (мультипликативная):
            decay_hormone = exp(-dt / tau_hormone)   # биохимическая гетерогенность
            new_momentary = baseline + (momentary - baseline) * (1 - alpha * decay_hormone) + noise
        
        Каждый гормон затухает со своей биологически правдоподобной скоростью,
        а alpha масштабирует общую силу затухания.
        
        Returns:
            dict: {applied, updates[], alpha, noise, tau_*, dt}
        """
        import math
        
        # === ЗАГРУЖАЕМ ВСЕ ПАРАМЕТРЫ ИЗ state.settings ===
        with psycopg2.connect(**self.db_config) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT param_name, value_float FROM state.settings
                    WHERE param_name IN (
                        'alpha_momentary_decay',
                        'momentary_drift_noise',
                        'tau_cortisol_sec',
                        'tau_dopamine_sec',
                        'tau_oxytocin_sec',
                        'momentary_decay_interval_sec'
                    )
                """)
                settings = {row['param_name']: row['value_float'] for row in cur.fetchall()}
        
        # Глобальный регулятор силы затухания (5% разницы за тик)
        alpha = settings.get('alpha_momentary_decay', 0.05)
        # Микрофлуктуации
        noise_scale = settings.get('momentary_drift_noise', 0.5)
        # Индивидуальные времена распада (секунды)
        tau_cort = settings.get('tau_cortisol_sec', 3600.0)
        tau_dopa = settings.get('tau_dopamine_sec', 180.0)
        tau_oxy = settings.get('tau_oxytocin_sec', 600.0)
        # Базовый временной шаг (интервал между тиками)
        dt = settings.get('momentary_decay_interval_sec', 60.0)
        
        # === ИНДИВИДУАЛЬНЫЕ КОЭФФИЦИЕНТЫ РАСПАДА ===
        decay_cort = math.exp(-dt / tau_cort) if tau_cort > 0 else 0.0  # ~0.983 (медленно)
        decay_dopa = math.exp(-dt / tau_dopa) if tau_dopa > 0 else 0.0  # ~0.716 (быстро)
        decay_oxy = math.exp(-dt / tau_oxy) if tau_oxy > 0 else 0.0     # ~0.905 (средне)
        
        # === ПОЛУЧАЕМ АКТИВНЫЕ MOMENTARY С BASELINE ===
        with psycopg2.connect(**self.db_config) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT m.id, m.actor_id, m.session_id, m.baseline_id,
                        m.cortisol, m.dopamine, m.oxytocin,
                        b.cortisol AS b_cort, b.dopamine AS b_dopa, b.oxytocin AS b_oxy
                    FROM state.momentary m
                    JOIN state.baseline_phs b ON b.is_active = TRUE
                    WHERE m.is_active = TRUE
                """)
                momentaries = cur.fetchall()
        
        if not momentaries:
            return {"applied": False, "reason": "no active momentaries", "updates": []}
        
        updates = []
        
        # === ПОЛУЧАЕМ event_type_id ДЛЯ decay_tick ===
        event_type_id = None
        with psycopg2.connect(**self.db_config) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM state.delta_reasons WHERE event_type_code = 'decay_tick'")
                row = cur.fetchone()
                event_type_id = row[0] if row else None
        
        for m in momentaries:
            # Независимый шум для каждого гормона
            noise_cort = noise_scale * random.gauss(0, 1)
            noise_dopa = noise_scale * random.gauss(0, 1)
            noise_oxy = noise_scale * random.gauss(0, 1)
            
            # === ДВОЙНАЯ ДИНАМИКА: alpha * decay_hormone ===
            # Momentary стремится к baseline со скоростью, зависящей от биохимии гормона
            factor_cort = 1.0 - alpha * decay_cort
            factor_dopa = 1.0 - alpha * decay_dopa
            factor_oxy  = 1.0 - alpha * decay_oxy

            new_cort = m['b_cort'] + (m['cortisol'] - m['b_cort']) * factor_cort + noise_cort
            new_dopa = m['b_dopa'] + (m['dopamine'] - m['b_dopa']) * factor_dopa + noise_dopa
            new_oxy  = m['b_oxy']  + (m['oxytocin']  - m['b_oxy'])  * factor_oxy  + noise_oxy
            
            # Clamp к физиологическим границам [0..100]
            new_cort = max(0.0, min(100.0, new_cort))
            new_dopa = max(0.0, min(100.0, new_dopa))
            new_oxy = max(0.0, min(100.0, new_oxy))
            
            # Пересчёт valence и RFF-вектора
            new_valence = compute_valence(new_cort, new_dopa, new_oxy)
            new_vector = self.encoder.encode(new_cort, new_dopa, new_oxy, new_valence)
            
            # Классификация состояния
            state_match = self.classifier.classify_vector(new_vector)
            
            # === СОЗДАЁМ НОВУЮ ЗАПИСЬ И ДЕАКТИВИРУЕМ СТАРУЮ ===
            with psycopg2.connect(**self.db_config) as conn:
                with conn.cursor() as cur:
                    # Деактивируем предыдущую запись (иммутабельная история)
                    cur.execute(
                        "UPDATE state.momentary SET is_active = FALSE WHERE id = %s",
                        (m['id'],)
                    )
                    
                    # Автозаполнение dialog_id (если есть активный диалог)
                    dialog_id = self._get_active_dialogue_id(m['actor_id'])
                    
                    # Вставляем новую запись
                    cur.execute("""
                        INSERT INTO state.momentary (
                            session_id, baseline_id, actor_id, dialog_id,
                            cortisol, dopamine, oxytocin, valence, state_vector,
                            state_id, event_type_id, is_active, agent_version,
                            orchestrator_step_id, recorded_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, %s, %s, NOW())
                        RETURNING id
                    """, (
                        m['session_id'], m['baseline_id'], m['actor_id'], dialog_id,
                        new_cort, new_dopa, new_oxy, new_valence, new_vector,
                        state_match.state_id, event_type_id, self.agent_version,
                        step_id
                    ))
                    new_id = str(cur.fetchone()[0])
                    conn.commit()
            
            # Трассировка: before/after IDs
            updates.append({
                "momentary_id_before": str(m['id']),
                "momentary_id_after": new_id,
                "actor_id": str(m['actor_id']),
                "baseline_id": str(m['baseline_id']) if m['baseline_id'] else None
            })
        
        return {
            "applied": True,
            "updates": updates,
            "alpha": alpha,
            "noise": noise_scale,
            "tau_cortisol": tau_cort,
            "tau_dopamine": tau_dopa,
            "tau_oxytocin": tau_oxy,
            "dt": dt
        }