"""
/main-srv/src/version.py

Модуль предоставляет версию проекта Kaya из pyproject.toml.
Используется везде, где требуется версия релиза:
- миграции БД,
- записи в БД,
- логирование запуска системы,
- метрики мониторинга.
"""

import tomllib
from pathlib import Path

def get_project_version() -> str:
    """Возвращает версию проекта из pyproject.toml"""
    try:
        pyproject_path = Path(__file__).parent.parent.parent / "pyproject.toml"
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
        return data["project"]["version"]
    except Exception as e:
        # Логирование через print — допустимо на старте, до инициализации логгера
        print(f"⚠️  Не удалось прочитать версию из pyproject.toml: {e}")
        return "0.0.0-dev"

# PEP 8: модуль должен экспортировать __version__
__version__ = get_project_version()