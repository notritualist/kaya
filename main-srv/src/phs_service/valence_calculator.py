"""
phs_service/valence_calculator.py
Модуль для расчёта валентности на основе текущих уровней гормонов.
Формула:
    valence = tanh(sensitivity * (dopamine + oxytocin - cortisol)) * 100

Коэффициент sensitivity берётся из state.settings (param_name='valence_sensitivity').
Если параметр отсутствует — используется fallback 0.015.

Архитектурные требования:
- Никаких зависимостей от BaselineManager / VectorEncoder.
- Каждый вызов читает актуальное значение sensitivity из БД.
- Без кэширования — чтобы изменения в settings сразу применялись.
"""

import logging
import math
import psycopg2
from db_manager.db_manager import load_postgres_config

logger = logging.getLogger(__name__)

def compute_valence(cortisol: float, dopamine: float, oxytocin: float) -> float:
    """
    Вычисляет валентность по формуле с динамическим коэффициентом из БД.
    
    Args:
        cortisol: уровень кортизола [0..100]
        dopamine: уровень дофамина [0..100]
        oxytocin: уровень окситоцина [0..100]
    
    Returns:
        Валентность в диапазоне (-100, 100)
    """
    # Загружаем чувствительность из БД (без кэширования!)
    sensitivity = _load_valence_sensitivity()
    
    raw = sensitivity * (dopamine + oxytocin - cortisol)
    return math.tanh(raw) * 100.0

def _load_valence_sensitivity() -> float:
    """
    Читает 'valence_sensitivity' из state.settings.
    Fallback: 0.015.
    """
    db_config = load_postgres_config()
    try:
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT value_float 
                    FROM state.settings 
                    WHERE param_name = 'valence_sensitivity'
                """)
                row = cur.fetchone()
                if row and row[0] is not None:
                    return float(row[0])
    except Exception as e:
        logger.warning("Failed to load 'valence_sensitivity' from DB, using default 0.015: %s", e)
    return 0.015