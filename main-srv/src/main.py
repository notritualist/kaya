"""/main-srv/src/main.py"""

__version__ = "1.0.0"
__description__ = "Главный модуль запуска Каи"


import sys
import logging
from pathlib import Path
from version import __version__ as kaya_version # Версия проекта
from db_manager.db_manager import load_postgres_config, ensure_schema_ready
from interfaces.console_interface import run_console_interface
from orchestrator.orchestrator import start_orchestrator
from session_services.session_manager import SessionManager

def setup_logging():
    """Настройка глобального логирования с фильтрацией"""
    project_root = Path(__file__).parent.parent
    log_dir = project_root / "logs"
    log_dir.mkdir(exist_ok=True)
    
    # Создаем логгер
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)  # Корневой логгер ловит всё
    
    # Форматтер
    formatter = logging.Formatter('[%(asctime)s] %(levelname)-8s | %(name)-15s | %(message)s')
    
    # 1. Файловый handler - пишет ВСЁ (DEBUG и выше)
    file_handler = logging.FileHandler(log_dir / "kaya_full.log", encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    
    # 2. Консольный handler - только WARNING и выше
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(formatter)
    
    # Удаляем старые handlers
    logger.handlers.clear()
    
    # Добавляем новые
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logging.getLogger(__name__)


def main():
    """
    Точка входа проекта.
    Последовательность:
    1. Логгирование старта агента
    2. Загрузка и проверка схемы БД 
    3. Очистка зависших после рестарта сессий диалогов пользователей
    3. Запустить цикл оркестратора
    4. Запуск консольного интерфейса с управлением сессией
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
        
        # 3. Очистка зависших до рестарта сессий
        SessionManager.close_dangling_sessions(postgres_config)
        
        # 3. Запустить цикл оркестратора
        start_orchestrator()

        # 4. Запустить консольный интерфейс с передачей конфига БД и версии агента
        run_console_interface(postgres_config, kaya_version)

    except Exception as e:
        logger.critical(f"Критическая ошибка запуска {e}", exc_info=True)
        return 1
    
    finally:
        if success:
            logger.info("Сеанс работы успешно завершен")
        else:
            logger.critical("Сеанс завершён с ошибкой")
            return 1
        
    return 0

if __name__ == "__main__":
    exit(main())