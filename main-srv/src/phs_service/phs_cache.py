"""
main-srv/src/phs_service/phs_cache.py

Global cache of PHS manager instances.

Purpose:
    Prevents creating multiple manager instances with each call.
    Ensures reuse of HormonalVectorEncoder with class-level parameter caching.
    Eliminates repeated reads of state.settings and state.baseline_phs.

Architecture:
    Module-level cache using a dictionary with string keys.
    get_* functions return a cached instance or create a new one.
    clear_cache() for resetting during testing or config reload.
"""

version = "1.0.0"
description = "Global cache of PHS manager"

import logging
from typing import Dict, Any

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