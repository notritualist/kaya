"""
main-srv/src/phs_service/phs_cache.py

PHS Managers Cache and Utility Functions.

Features:
- Singleton cache for BaselineManager, MomentaryManager, LifecycleManager instances.
- Avoids repeated DB config loading and manager initialization.
- Thread-safe lazy initialization.
- Provides utility function get_current_phs_snapshot() for stamping messages,
  tasks, steps, and reasonings with current hormonal state IDs.

Architecture:
- Global dict _cache stores manager instances.
- get_*_manager() functions return cached or create new instances.
- get_current_phs_snapshot() is stateless, executes single DB query.
"""

version = "1.1.0"
description = "PHS Managers Cache and Utility Functions module"

import logging
import psycopg2
from typing import Dict, Any, Optional


logger = logging.getLogger(__name__)

# Локальные импорты
from phs_service.vector_encoder import HormonalVectorEncoder
from phs_service.state_classifier import StateClassifier
from phs_service.baseline_manager import BaselineManager
from phs_service.momentary_manager import MomentaryManager

# Module-level кэш
_cache: Dict[str, Any] = {}


def get_encoder(db_config: Dict[str, Any]) -> HormonalVectorEncoder:
    """
    Возвращает кэшированный экземпляр HormonalVectorEncoder.
    
    При первом вызове создаёт энкодер, который загружает параметры из БД.
    Последующие вызовы возвращают тот же экземпляр.
    HormonalVectorEncoder использует class-level кэш для omega/sigma.
    """
    key = "encoder"
    if key not in _cache:
        logger.debug("Creating cached HormonalVectorEncoder")
        _cache[key] = HormonalVectorEncoder(db_config)
    return _cache[key]


def get_classifier(db_config: Dict[str, Any]) -> StateClassifier:
    """
    Возвращает кэшированный экземпляр StateClassifier.
    """
    key = "classifier"
    if key not in _cache:
        logger.debug("Creating cached StateClassifier")
        _cache[key] = StateClassifier(db_config)
    return _cache[key]


def get_baseline_manager(db_config: Dict[str, Any]) -> BaselineManager:
    """
    Возвращает кэшированный экземпляр BaselineManager.
    """
    key = "baseline_manager"
    if key not in _cache:
        logger.debug("Creating cached BaselineManager")
        _cache[key] = BaselineManager(db_config)
    return _cache[key]


def get_momentary_manager(db_config: Dict[str, Any]) -> MomentaryManager:
    """
    Возвращает кэшированный экземпляр MomentaryManager.
    """
    key = "momentary_manager"
    if key not in _cache:
        logger.debug("Creating cached MomentaryManager")
        _cache[key] = MomentaryManager(db_config)
    return _cache[key]


def clear_cache() -> None:
    """
    Очищает кэш менеджеров.
    
    Используется при тестировании или принудительной перезагрузке конфигурации.
    Также вызывает HormonalVectorEncoder.clear_cache() для сброса параметров RFF.
    """
    global _cache
    _cache.clear()
    HormonalVectorEncoder.clear_cache()
    logger.debug("PHS cache cleared")


def get_current_phs_snapshot(db_config: dict, actor_id: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """
    Возвращает кортеж (baseline_id, momentary_id) для текущего состояния агента.
    
    Используется для штамповки сообщений, задач, шагов и рассуждений.
    
    Логика:
    1. baseline_id — всегда берется активный baseline (is_active=TRUE).
    2. momentary_id — если передан actor_id, берется активный momentary для него.
       Если actor_id=None (фоновые задачи ПГС) — momentary_id будет None.
    
    Args:
        db_config: Параметры подключения к PostgreSQL.
        actor_id: UUID актора (пользователя) для поиска momentary. Может быть None.
        
    Returns:
        tuple[str | None, str | None]: (baseline_id, momentary_id).
            Оба могут быть None, если система еще не инициализирована.
    """
    baseline_id = None
    momentary_id = None
    
    try:
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor() as cur:
                # 1. Получаем активный baseline
                cur.execute("SELECT id FROM state.baseline_phs WHERE is_active = TRUE LIMIT 1")
                row = cur.fetchone()
                if row:
                    baseline_id = str(row[0])
                
                # 2. Получаем активный momentary для актора (если задан)
                if actor_id:
                    cur.execute(
                        "SELECT id FROM state.momentary WHERE actor_id = %s AND is_active = TRUE LIMIT 1",
                        (actor_id,)
                    )
                    row = cur.fetchone()
                    if row:
                        momentary_id = str(row[0])
    except Exception as e:
        # Ошибка получения PHS-среза не должна ломать основную операцию
        logger.warning(f"Failed to get PHS snapshot: {e}")
        
    return baseline_id, momentary_id