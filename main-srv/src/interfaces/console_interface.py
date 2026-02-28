"""
main-srv/src/interfaces/console_interface.py

Консольный интерфейс для диалога с AGI-агентом Кая.
С поддержкой многострочного ввода (Shift+Enter = новая строка, Enter = отправить)

Логика работы:
1. При старте определяет текущего пользователя ОС (Linux).
2. Привязывает его к актору типа 'owner' в БД (если ещё не привязан).
3. Создаёт НОВУЮ сессию диалога (каждый запуск = отдельная сессия).
4. Запускает цикл: ввод пользователя → сохранение в БД → ожидание ответа → вывод.
5. При выходе (exit / Ctrl+C) корректно завершает сессию в БД.

Схема БД: dialogs.sessions, dialogs.messages, users.actors, users.actors_external_ids
Версия миграции: V001
"""

__version__ = "1.0.0"
__description__ = "Консольный интерфейс диалога с Kaya (owner-режим)"

import logging
import pwd
import os
from session_services.session_manager import SessionManager
from session_services.session_manager import SessionManager
from prompt_toolkit import PromptSession
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys

# Получаем логгер для этого модуля
logger = logging.getLogger(__name__)


def _get_current_console_user() -> str:
    """
    Определяет уникальное имя текущего пользователя операционной системы.
    
    Возвращает строку в формате: "console:<username>"
    Пример: "console:debian", "console:root"
    
    Это значение будет использоваться как source_id в users.actors_external_ids
    """
    try:
        username = pwd.getpwuid(os.getuid()).pw_name
        return f"console:{username}"
    except Exception as e:
        logger.warning(f"Не удалось определить имя пользователя ОС: {e}. Использую 'console:unknown'")
        return "console:unknown"


def _print_welcome(kaya_version: str, console_user_id: str, actor_type: str):
    """Выводит приветственное сообщение в консоль."""
    print(f"\n{'='*66}")
    print(f"🤖  Кая (версия {kaya_version})")
    print(f"👤  Режим: {actor_type} (уровень доступа) | Пользователь: {console_user_id}")
    print(f"💡  Enter = отправить, Alt+Enter = новая строка, exit/выход или Ctrl+C для выхода")
    print(f"{'='*66}\n")


def _print_status(message: str, is_success: bool):
    """Выводит цветное сообщение статуса в консоль."""
    COLOR_GREEN = "\033[92m"
    COLOR_RED = "\033[91m"
    COLOR_RESET = "\033[0m"
    
    symbol = "✓" if is_success else "✗"
    color = COLOR_GREEN if is_success else COLOR_RED
    
    print(f"{color}[{symbol}] {message}{COLOR_RESET}")


def create_prompt_session() -> PromptSession:
    """
    Создаёт сессию prompt_toolkit с поддержкой:
    - Enter = отправить сообщение
    - Alt+Enter = новая строка
    - Ctrl+C = аварийный выход
    """
    bindings = KeyBindings()
    
    # Alt+Enter = новая строка (приоритет выше, чем Enter)
    @bindings.add(Keys.Escape, Keys.Enter)
    def _(event):
        event.current_buffer.insert_text('\n')
    
    # Enter = отправить
    @bindings.add(Keys.Enter)
    def _(event):
        event.current_buffer.validate_and_handle()
    
    # Ctrl+C = аварийный выход
    @bindings.add('c-c')
    def _(event):
        raise KeyboardInterrupt()
    
    return PromptSession(
        key_bindings=bindings,
        multiline=True,
        enable_history_search=True,
    )


def get_user_input(session: PromptSession) -> str:
    """
    Получает ввод от пользователя.
    Выбрасывает KeyboardInterrupt при Ctrl+C.
    """
    try:
        result = session.prompt(message='\n👤 Вы: ')
        return (result or "").strip()
    except (EOFError, KeyboardInterrupt):
        raise KeyboardInterrupt()


def run_console_interface(db_config: dict, kaya_version: str):
    """
    Главная точка входа для консольного интерфейса.
    
    Args:
        db_config: словарь с параметрами подключения к PostgreSQL
        kaya_version: строка версии агента из pyproject.toml
    """
    
    # === ШАГ 1: Инициализация ===
    console_user_id = _get_current_console_user()
    logger.info(f"Запуск консольного интерфейса. Пользователь: {console_user_id}, версия: {kaya_version}")
    
    # === ШАГ 2: Инициализация сервиса сессий ===
    session_service = SessionManager(db_config, kaya_version, console_user_id)
    
    try:
        # === ШАГ 3: Привязка пользователя к актору owner ===
        owner_linked = session_service.ensure_actor_linked()
        if owner_linked:
            logger.info(f"Пользователь {console_user_id} привязан к актору (тип: {session_service.actor_type})")
            _print_status(f"Пользователь {console_user_id} активирован как {session_service.actor_type}", True)
        else:
            logger.debug(f"Пользователь {console_user_id} уже привязан к {session_service.actor_type}")
        
        # === ШАГ 4: Создание новой сессии ===
        # Каждый запуск консоли = новая сессия (не возобновляем старые)
        session_id = session_service.create_session(room_name="open_dialogue")
        logger.info(f"Создана новая сессия диалога: {session_id}")
        _print_status(f"Сессия #{session_id[:8]} начата", True)
        
        # === ШАГ 5: Вывод приветствия (теперь все данные известны) ===
        _print_welcome(kaya_version, console_user_id, session_service.actor_type)
        
        # === ШАГ 5.1: Создаём сессию ввода ===
        prompt_session = create_prompt_session()

        # === ШАГ 6: Основной цикл диалога ===
        while True:
            try:
                # Получаем многострочный ввод
                user_input = get_user_input(prompt_session)
                
                # Обработка команд выхода
                if user_input.lower() in ("exit", "выход"):
                    logger.info("Пользователь ввёл команду выхода")
                    break
                
                if not user_input:
                    continue
                
                logger.debug(f"Получено сообщение от пользователя: {len(user_input)} симв.")
                
                # 6.1: Сохраняем сообщение в БД
                message_id = session_service.save_message(
                    content=user_input,
                    room_name="open_dialogue"
                )
                logger.debug(f"Сообщение сохранено в БД с ID: {message_id[:8]}")
                
                # 6.2: Создаём задачу для оркестратора
                from orchestrator.orchestrator_entry import on_user_message
                on_user_message(message_id)
                                
                # 6.3: Обновляем время активности сессии
                session_service.update_activity()

                # 6.4: Показываем статус обработки
                status_text = "⚙️  Кая думает..."
                print(f"\n{status_text}", end="", flush=True)

                # 6.5: Ожидаем ответ от агента
                kaya_response = session_service.wait_for_agent_response(
                    user_message_id=message_id,
                    timeout_seconds=120
                )

                # 6.6: Заменяем статус на ответ
                if kaya_response:
                    print(f"\r{' ' * len(status_text)}\r🤖 Кая: {kaya_response}\n", end="", flush=True)
                    logger.info("Ответ агента получен: %d симв.", len(kaya_response))
                else:
                    print(f"\r{' ' * len(status_text)}\r🤖 Кая: [Ответ не получен]\n", end="", flush=True)
                    logger.warning("Таймаут ожидания ответа от агента")

            except KeyboardInterrupt:
                logger.warning("Сессия прервана пользователем (Ctrl+C)")
                print("\n\n[!] Прервано пользователем")
                break
                
            except Exception as e:
                logger.error(f"Ошибка в цикле диалога: {e}", exc_info=True)
                _print_status(f"Ошибка обработки: {e}", False)
                continue
        
        # === ШАГ 7: Завершение сессии ===
        logger.info("Завершение сессии диалога...")
        session_service.close_session()
        _print_status("Сессия завершена. Данные сохранены в БД.", True)
        
    except Exception as e:
        logger.critical(f"Критическая ошибка консольного интерфейса: {e}", exc_info=True)
        _print_status(f"Критическая ошибка: {e}", False)
        return 1
    
    finally:
        session_service.cleanup()
        logger.debug("Ресурсы консольного интерфейса освобождены")
    
    return 0