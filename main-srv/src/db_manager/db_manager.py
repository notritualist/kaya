"""/main-srv/src/db_manager/db_manager.py"""

__version__ = "1.0.0"
__description__ = "Main module for database (PostgreSQL) operations"


import yaml
import logging
from pathlib import Path
from .migrations.pg_migration_manager import PGMigrationManager


# Логгер для этого модуля
logger = logging.getLogger(__name__)


# ============================================================================
# === PostgreSQL ===
# ============================================================================

def load_postgres_config(config_path: str | None = None) -> dict:
    """
    Loads Postgres DB configuration from file
    Args:
        config_path: Path to configuration file (optional)
    Returns:
        dict: Dictionary with PostgreSQL configuration
    Raises:
        FileNotFoundError: If configuration file not found
        Exception: On YAML parsing error
    """
    # Определяем путь к конфигу
    if config_path is None:
        config_file_path = Path(__file__).parent.parent.parent / "configs" / "postgres_config.yaml"
   
    else:
        config_file_path = Path(config_path)
    
    logger.debug(f"Loading Postgres DB configuration from: {config_file_path}")
    
     # Проверка существования файла
    if not config_file_path.exists():
        error_msg = f"Postgres configuration file not found: {config_file_path}"
        logger.error(error_msg)
        raise FileNotFoundError(error_msg)
    
    # Загрузка и парсинг YAML
    try:
        with config_file_path.open('r', encoding='utf-8') as f:
            config_data = yaml.safe_load(f)
        
        logger.info(f"Postgres DB configuration successfully loaded from: {config_file_path}")
        
        return config_data["database"]
    
    except Exception as e:
        logger.error(f"Error loading Postgres DB configuration: {e}")
        raise


# === PostgreSQL ===
def ensure_postgres_schema_ready(postgres_config: dict | None = None) -> bool:
    """
    Ensures Postgres DB schema is up to date (all migrations applied)
    Args:
        postgres_config: PostgreSQL configuration (optional, will be loaded automatically)
    Returns:
        bool: True if schema is up to date, False if there are errors
    """
    try:
        if postgres_config is None:
            postgres_config = load_postgres_config()
        
        # Путь к миграциям относительно этого файла
        migrations_path = Path(__file__).parent / "migrations"
        migration_manager = PGMigrationManager(str(migrations_path))
        logger.info("Checking Postgres DB schema up-to-date status...")
        
        result = migration_manager.ensure_schema_ready(postgres_config)

        if result:
            logger.info("Postgres DB migration schema check completed successfully")
        else:
            logger.error("Postgres DB migration schema check completed with errors")
        
        return result
    
    except Exception as e:
        logger.error(f"Critical error during DB migration schema check: {e}", exc_info=True)
        return False