"""
main-srv/src/phs_service/valence_calculator.py

Valence Calculator

Module for calculating valence based on current hormone levels.
Formula:
    valence = tanh(sensitivity * (dopamine + oxytocin - cortisol)) * 100

Sensitivity coefficient is read from state.settings (param_name='valence_sensitivity').
If parameter is missing — fallback value 0.015 is used.

Architectural requirements:
- No dependencies on BaselineManager or VectorEncoder.
- Each call reads the actual sensitivity value from the database.
- No caching — changes in settings take effect immediately.
"""

version = "1.1.1"
description = "Valence Calculator"

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