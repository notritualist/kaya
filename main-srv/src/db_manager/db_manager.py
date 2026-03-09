"""/main-srv/src/db_manager/db_manager.py"""

__version__ = "1.1.0"
__description__ = "Главный модуль баз данных (PostgreSQL + Qdrant)"


import yaml
import logging
from pathlib import Path
from .migrations.migration_manager import MigrationManager


# Логгер для этого модуля
logger = logging.getLogger(__name__)


# ============================================================================
# === PostgreSQL ===
# ============================================================================

def load_postgres_config(config_path: str | None = None) -> dict:
    """
    Загружает конфигурацию БД Postgres из файла
    
    Args:
        config_path: Путь к файлу конфигурации (опционально)
    
    Returns:
        dict: Словарь с конфигурацией PostgreSQL
    
    Raises:
        FileNotFoundError: Если файл конфигурации не найден
        Exception: При ошибке парсинга YAML
    """
    # Определяем путь к конфигу
    if config_path is None:
        config_file_path = Path(__file__).parent.parent.parent / "configs" / "postgres_config.yaml"
   
    else:
        config_file_path = Path(config_path)
    
    logger.debug(f"Загрузка конфигурации БД Postgres из: {config_file_path}")
    
     # Проверка существования файла
    if not config_file_path.exists():
        error_msg = f"Файл конфигурации Postgres не найден: {config_file_path}"
        logger.error(error_msg)
        raise FileNotFoundError(error_msg)
    
    # Загрузка и парсинг YAML
    try:
        with config_file_path.open('r', encoding='utf-8') as f:
            config_data = yaml.safe_load(f)
        
        logger.info(f"Конфигурация БД Postgres успешно загружена из {config_file_path}")
        
        return config_data["database"]
    
    except Exception as e:
        logger.error(f"Ошибка загрузки конфигурации БД Postgres: {e}")
        raise


# === PostgreSQL ===
def ensure_schema_ready(postgres_config: dict | None = None) -> bool:
    """
    Гарантирует что схема БД Postgres актуальна (применены все миграции)
    
    Args:
        postgres_config: Конфигурация PostgreSQL (опционально, загрузится автоматически)
    
    Returns:
        bool: True если схема актуальна, False если есть ошибки
    """
    try:
        if postgres_config is None:
            postgres_config = load_postgres_config()
        
        # Путь к миграциям относительно этого файла
        migrations_path = Path(__file__).parent / "migrations"
        migration_manager = MigrationManager(str(migrations_path))
        logger.info("Проверка актуальности схемы БД Postgres...")
        
        result = migration_manager.ensure_schema_ready(postgres_config)

        if result:
            logger.info("Проверка схемы миграций БД Postgres завершена успешно")
        else:
            logger.error("Проверка схемы миграций БД Postgres завершена с ошибками")
        
        return result
    
    except Exception as e:
        logger.error(f"Критическая ошибка при проверке схемы миграций БД: {e}", exc_info=True)
        return False
    
# ============================================================================
# === Qdrant ===
# ============================================================================

def load_qdrant_config(config_path: str | None = None) -> dict:
    """
    Загружает конфигурацию векторной БД Qdrant из файла
    
    Args:
        config_path: Путь к файлу конфигурации (опционально)
    
    Returns:
        dict: Словарь с конфигурацией Qdrant (host, port, и др.)
    
    Raises:
        FileNotFoundError: Если файл конфигурации не найден
        Exception: При ошибке парсинга YAML
    """
    # Определяем путь к конфигу
    if config_path is None:
        config_file_path = Path(__file__).parent.parent.parent / "configs" / "qdrant_config.yaml"
    else:
        config_file_path = Path(config_path)
    
    logger.debug(f"Загрузка конфигурации БД Qdrant из: {config_file_path}")
    
    # Проверка существования файла
    if not config_file_path.exists():
        error_msg = f"Файл конфигурации Qdrant не найден: {config_file_path}"
        logger.error(error_msg)
        raise FileNotFoundError(error_msg)
    
    # Загрузка и парсинг YAML
    try:
        with config_file_path.open('r', encoding='utf-8') as f:
            config_data = yaml.safe_load(f)
        
        logger.info(f"Конфигурация БД Qdrant успешно загружена из {config_file_path}")
        
        return config_data
    
    except Exception as e:
        logger.error(f"Ошибка загрузки конфигурации БД Qdrant: {e}")
        raise


# ============================================================================
# === Qdrant ===
# ============================================================================

def load_qdrant_config(config_path: str | None = None) -> dict:
    """Загружает конфигурацию векторной БД Qdrant из файла"""
    if config_path is None:
        config_file_path = Path(__file__).parent.parent.parent / "configs" / "qdrant_config.yaml"
    else:
        config_file_path = Path(config_path)
    
    logger.debug(f"Загрузка конфигурации БД Qdrant из: {config_file_path}")
    
    if not config_file_path.exists():
        error_msg = f"Файл конфигурации Qdrant не найден: {config_file_path}"
        logger.error(error_msg)
        raise FileNotFoundError(error_msg)
    
    try:
        with config_file_path.open('r', encoding='utf-8') as f:
            config_data = yaml.safe_load(f)
        logger.info(f"Конфигурация БД Qdrant успешно загружена")
        return config_data
    except Exception as e:
        logger.error(f"Ошибка загрузки конфигурации БД Qdrant: {e}")
        raise


def ensure_qdrant_collections(qdrant_config: dict | None = None) -> bool:
    """Проверяет подключение к Qdrant и создаёт коллекцию если не существует"""
    try:
        if qdrant_config is None:
            qdrant_config = load_qdrant_config()
        
        from qdrant_client import QdrantClient
        from qdrant_client.models import (
            Distance, VectorParams, HnswConfigDiff,
            OptimizersConfigDiff, ScalarQuantization,
            ScalarQuantizationConfig, ScalarType, WalConfigDiff
        )
        
        client = QdrantClient(
            host=qdrant_config.get("host", "localhost"),
            port=qdrant_config.get("port", 6333),
            timeout=30
        )
        
        # Проверка подключения
        client.get_collections()
        logger.info("Подключение к Qdrant успешно")
        
        collection_name = "kaya_db"
        
        # Если коллекция уже есть — выходим
        if client.collection_exists(collection_name):
            logger.info(f"Коллекция '{collection_name}' уже существует")
            return True
        
        # Создание коллекции (параметры из create_qdrant_collection.py)
        logger.info(f"Создание коллекции '{collection_name}'...")
        
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(
                size=2560,
                distance=Distance.COSINE,
                on_disk=False
            ),
            hnsw_config=HnswConfigDiff(
                m=16,
                ef_construct=100,
                full_scan_threshold=10000,
                max_indexing_threads=0,
                on_disk=False
            ),
            optimizers_config=OptimizersConfigDiff(
                deleted_threshold=0.2,
                vacuum_min_vector_number=1000,
                default_segment_number=2,
                memmap_threshold=50000,
                indexing_threshold=10000,
                flush_interval_sec=5,
                max_optimization_threads=2,
            ),
            wal_config=WalConfigDiff(
                wal_capacity_mb=1024,
                wal_segments_ahead=0
            ),
            quantization_config=ScalarQuantization(
                scalar=ScalarQuantizationConfig(
                    type=ScalarType.INT8,
                    quantile=0.99,
                    always_ram=True
                )
            ),
            on_disk_payload=True,
            replication_factor=1,
            shard_number=1
        )
        
        logger.info(f"Коллекция '{collection_name}' создана успешно")
        return True
        
    except Exception as e:
        logger.error(f"Ошибка при работе с Qdrant: {e}", exc_info=True)
        return False