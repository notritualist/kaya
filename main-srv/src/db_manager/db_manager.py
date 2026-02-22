"""/main-srv/src/db_manager/db_manager.py"""

__version__ = "1.0.0"
__description__ = "Главный модуль баз данных"


import yaml
import logging
from pathlib import Path
from .migrations.migration_manager import MigrationManager


# Логгер для этого модуля
logger = logging.getLogger(__name__)


# === PostgreSQL ===
def load_postgres_config(config_path: str | None = None) -> dict:
    """Загружает конфигурацию БД Postgres из файла"""
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
    """Гарантирует что схема БД Postgres актуальна"""
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