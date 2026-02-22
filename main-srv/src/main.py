"""/main-srv/src/main.py"""

__version__ = "1.0.0"
__description__ = "Главный модуль запуска Каи"


import sys
import logging
from pathlib import Path
from version import __version__ as kaya_version # Версия проекта
from db_manager.db_manager import load_postgres_config, ensure_schema_ready


def setup_logging():
    """Настройка глобального логирования с фильтрацией"""
    # Определяем путь относительно файла проекта
    project_root = Path(__file__).parent.parent
    log_dir = project_root / "logs"
    log_dir.mkdir(exist_ok=True)
    
    # Базовая конфигурация
    logging.basicConfig(
        level=logging.DEBUG,
        format='[%(asctime)s] %(levelname)-8s | %(name)-15s | %(message)s',
        handlers=[
            logging.FileHandler(log_dir / "kaya_full.log", encoding='utf-8'),
            logging.StreamHandler(sys.stdout)
        ]
    )
    
    # Настройка уровней для консоли
    for handler in logging.root.handlers:
        if isinstance(handler, logging.StreamHandler) and handler.stream == sys.stdout:
            handler.setLevel(logging.WARNING) # Только WARNING и выше в консоль
    
    return logging.getLogger(__name__)


def main():
    """
    Точка входа проекта.
    Последовательность:
    1. Загрузка и проверка схемы БД 
    2. 
    3. 
    4. 
    """

    # Инициализация логгирования
    success = False
    logger = setup_logging()

    try:
        # 1. Пишем старт сессии в лог
        logger.info(f"Запуск Каи version {kaya_version}")

        # 2. Убеждаемся, что схема БД Postgres актуальна (миграции применены)
        postgres_config = load_postgres_config()
        if not ensure_schema_ready(postgres_config):
            logger.critical(f"Инициализация схемы базы данных Postgres не удалась")
            return 1
        success = True 
        
    except Exception as e:
        logger.critical(f"Критическая ошибка запуска {e}", exc_info=True)
        return 1
    
    finally:
        if success:
            logger.info("Сеанс работы успешно завершен")
        else:
            logger.info("Сеанс завершён с ошибкой")

    return 0

if __name__ == "__main__":
    exit(main())