"""
phs_service/vector_encoder.py
Модуль векторизации гормонального профиля через Random Fourier Features (RFF).

Реализация:
- Нормализация входов: cort/100, dopa/100, oxy/100, (val+100)/200.
- Проекция: proj = (B / sigma) @ x, где B ~ N(0,1), sigma = 1/sqrt(2*gamma).
- Преобразование: interleaved sin/cos -> вектор 128.
- L2-нормализация результата.
- Параметры (omega, gamma, seed) хранятся в state.settings.
- Матрица omega генерируется один раз по seed и фиксируется в БД.
"""

import logging
import math
import json
from typing import List, Dict, Any
import numpy as np
import psycopg2
from psycopg2.extras import RealDictCursor

# Локальные импорты
from db_manager.db_manager import load_postgres_config

logger = logging.getLogger(__name__)

# =============================================================================
# КОНСТАНТЫ МОДУЛЯ
# =============================================================================

# Размерность выходного вектора (halfvec)
VECTOR_DIM: int = 128  # Выходная размерность вектора состояния

# Размерность матрицы проекций (VECTOR_DIM // 2 x 4)
PROJ_ROWS: int = 64    # Количество строк в матрице B (64 * 2 = 128)
PROJ_COLS: int = 4     # Количество гормонов: cort, dopa, oxy, val

# Ключи настроек в state.settings
SETTING_RFF_OMEGA: str = "rff_omega"      # JSONB: матрица B (64x4)
SETTING_RFF_GAMMA: str = "rff_gamma"      # Float: параметр ядра gamma
SETTING_RFF_SEED: str = "rff_seed"        # Float: seed для генерации B
SETTING_RFF_SIGMA: str = "rff_sigma"      # Float: явный sigma (опционально, приоритет над gamma)

# Значения по умолчанию для генерации (используются только при первой инициализации)
DEFAULT_RFF_SEED: int = 42
DEFAULT_RFF_GAMMA: float = 0.1


class HormonalVectorEncoder:
    """
    Кодировщик гормонального профиля в вектор RFF.

    Атрибуты:
        omega (np.ndarray): Матрица проекций B формы (64, 4).
        sigma (float): Масштабный коэффициент для проекции.
        is_initialized (bool): Флаг успешной загрузки параметров.
    """

    def __init__(self, db_config: Dict[str, Any]):
        """
        Инициализирует энкодер, загружая параметры из БД.

        Если параметры отсутствуют, генерирует их детерминировано и сохраняет.

        Args:
            db_config: Параметры подключения к PostgreSQL.
        """
        self.db_config = db_config
        self.omega: np.ndarray = np.zeros((PROJ_ROWS, PROJ_COLS), dtype=np.float64)
        self.sigma: float = 1.0
        self.is_initialized: bool = False

        self._load_or_init_params()

    def _load_or_init_params(self) -> None:
        """
        Загружает omega и sigma из state.settings.
        Если omega отсутствует, генерирует по seed и сохраняет.
        """
        logger.debug("Loading RFF parameters from state.settings...")

        with psycopg2.connect(**self.db_config) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Читаем настройки
                cur.execute("""
                    SELECT param_name, value_float, value_json
                    FROM state.settings
                    WHERE param_name IN (%s, %s, %s, %s)
                """, (SETTING_RFF_OMEGA, SETTING_RFF_GAMMA, SETTING_RFF_SEED, SETTING_RFF_SIGMA))
                
                settings = {row['param_name']: row for row in cur.fetchall()}

        # 1. Определяем sigma
        if SETTING_RFF_SIGMA in settings and settings[SETTING_RFF_SIGMA]['value_float'] is not None:
            self.sigma = settings[SETTING_RFF_SIGMA]['value_float']
            logger.debug("Using explicit rff_sigma=%.4f", self.sigma)
        elif SETTING_RFF_GAMMA in settings and settings[SETTING_RFF_GAMMA]['value_float'] is not None:
            gamma = settings[SETTING_RFF_GAMMA]['value_float']
            # sigma = 1 / sqrt(2 * gamma)
            self.sigma = 1.0 / math.sqrt(2.0 * gamma) if gamma > 0 else 1.0
            logger.debug("Derived sigma=%.4f from gamma=%.4f", self.sigma, gamma)
        else:
            self.sigma = 1.0 / math.sqrt(2.0 * DEFAULT_RFF_GAMMA)
            logger.warning("RFF gamma/sigma not found, using default sigma=%.4f", self.sigma)

        # 2. Загружаем или генерируем omega
        omega_json = settings.get(SETTING_RFF_OMEGA, {}).get('value_json')
        
        if omega_json and isinstance(omega_json, list) and len(omega_json) == PROJ_ROWS:
            self.omega = np.array(omega_json, dtype=np.float64)
            logger.info("RFF omega matrix loaded from DB. Shape: %s", self.omega.shape)
        else:
            logger.info("RFF omega not found or invalid. Generating new matrix...")
            self._generate_and_save_omega(settings)

        self.is_initialized = True
        logger.debug("HormonalVectorEncoder initialized successfully.")

    def _generate_and_save_omega(self, existing_settings: Dict[str, Any]) -> None:
        """
        Генерирует матрицу omega по seed и сохраняет в state.settings.
        """
        seed = DEFAULT_RFF_SEED
        if SETTING_RFF_SEED in existing_settings and existing_settings[SETTING_RFF_SEED]['value_float'] is not None:
            seed = int(existing_settings[SETTING_RFF_SEED]['value_float'])
        
        logger.info("Generating RFF omega with seed=%d", seed)
        
        # Детерминированная генерация
        rng = np.random.RandomState(seed)
        self.omega = rng.randn(PROJ_ROWS, PROJ_COLS)

        # Сохраняем в БД
        omega_list = self.omega.tolist()
        
        with psycopg2.connect(**self.db_config) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO state.settings (param_name, value_json, description)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (param_name) DO UPDATE
                    SET value_json = EXCLUDED.value_json,
                        updated_at = NOW()
                """, (SETTING_RFF_OMEGA, json.dumps(omega_list), "RFF projection matrix B (64x4). Generated once from seed."))
                conn.commit()
        
        logger.info("RFF omega matrix saved to state.settings.")

    def encode(self, cortisol: float, dopamine: float, oxytocin: float, valence: float) -> List[float]:
        """
        Кодирует гормональный профиль в вектор размерности 128.

        Алгоритм:
        1. Нормализация в [0, 1].
        2. Проекция proj = (omega / sigma) @ x.
        3. Sin/Cos interleaving.
        4. L2-нормализация.

        Args:
            cortisol: Уровень кортизола [0..100].
            dopamine: Уровень дофамина [0..100].
            oxytocin: Уровень окситоцина [0..100].
            valence: Валентность [-100..100].

        Returns:
            Список из 128 float значений, готовый для halfvec(128).
        """
        if not self.is_initialized:
            raise RuntimeError("Encoder not initialized. Call _load_or_init_params first.")

        # 1. Нормализация
        x = np.array([
            cortisol / 100.0,
            dopamine / 100.0,
            oxytocin / 100.0,
            (valence + 100.0) / 200.0
        ], dtype=np.float64)

        # 2. Проекция
        # proj = (B / sigma) @ x
        proj = (self.omega / self.sigma) @ x  # shape (64,)

        # 3. Sin/Cos interleaving
        z = np.empty(VECTOR_DIM, dtype=np.float64)
        z[0::2] = np.sin(proj)
        z[1::2] = np.cos(proj)

        # 4. L2-нормализация
        norm = np.linalg.norm(z)
        if norm > 1e-8:
            z /= norm
        else:
            # Защита от нулевого вектора
            z[:] = 0.0

        return z.tolist()