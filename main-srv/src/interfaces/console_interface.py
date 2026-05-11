"""
main-srv/src/interfaces/console_interface.py

A console interface for dialog with an AGI agent.
With multi-line input support (Shift+Enter = new line, Enter = send).
Operational logic:
On startup, determines the current OS user (Linux).
Binds it to an 'owner' actor in the database (if not already bound).
Creates a NEW dialog session (each launch = a separate session).
Starts a cycle: user input → save to database → wait for response → exit.
On exit (exit / Ctrl+D), gracefully closes the database session.
DB schema: dialogs.sessions, dialogs.row_messages, users.actors, users.actors_external_ids
Migration version: V001
"""

version = "1.0.0"
description = "Console interface for dialogue with an agent (owner mode)"

import logging
import pwd
import os
from session_services.session_manager import SessionManager
from prompt_toolkit import PromptSession
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys

# Получаем логгер для этого модуля
logger = logging.getLogger(__name__)

def _get_current_console_user() -> str:
    """
    Определяет уникальное имя текущего пользователя операционной системы.
    Возвращает строку в формате:  "console: <username> "
    Пример:  "console:debian ",  "console:root "

    Это значение будет использоваться как source_id в users.actors_external_ids
    """
    try:
        username = pwd.getpwuid(os.getuid()).pw_name
        return f"console:{username}"
    except Exception as e:
        logger.warning(f"Failed to determine OS username: {e}. Using 'console:unknown'")
        return "console:unknown"

def _print_welcome(agent_version: str, console_user_id: str, actor_type: str):
    """Выводит приветственное сообщение в консоль."""
    print(f"\n{'='*85}")
    print(f"🤖  Agent (version {agent_version})")
    print(f"👤  Mode: {actor_type} (access level) | User: {console_user_id}")
    print(f"💡  Enter = send, Alt+Enter = new line, exit/выход or Ctrl+D to quit")
    print(f"{'='*85}\n")

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
    - Ctrl+D = аварийный выход
    - Ctrl+C = игнорируется (чтобы не выходить случайно)
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

    # Ctrl+D = аварийный выход (не зависит от раскладки)
    @bindings.add('c-d')
    def _(event):
        raise KeyboardInterrupt()

    # Ctrl+C = игнорировать (чтобы не выходить случайно вместо копирования)
    @bindings.add('c-c')
    def _(event):
        pass

    return PromptSession(
        key_bindings=bindings,
        multiline=True,
        enable_history_search=True,
    )

def get_user_input(session: PromptSession) -> str:
    """
    Получает ввод от пользователя.
    Выбрасывает KeyboardInterrupt при Ctrl+D.
    """
    try:
        result = session.prompt(message='\n👤 You: ')
        return (result or "").strip()
    except (EOFError, KeyboardInterrupt):
        raise KeyboardInterrupt()

def run_console_interface(db_config: dict, agent_version: str):
    """
    Главная точка входа для консольного интерфейса.
    Args:
        db_config: словарь с параметрами подключения к PostgreSQL
        agent_version: строка версии агента из pyproject.toml
    """

    # === ШАГ 1: Инициализация ===
    console_user_id = _get_current_console_user()
    logger.info(f"Starting console interface. User: {console_user_id}, version: {agent_version}")

    # === ШАГ 2: Инициализация сервиса сессий ===
    session_service = SessionManager(db_config, agent_version, console_user_id)

    # Строго допустимые значения из session_close_reason ENUM
    exit_reason: str = "unknown"

    try:
        # === ШАГ 3: Привязка пользователя к актору owner ===
        owner_linked = session_service.ensure_actor_linked()
        if owner_linked:
            logger.info(f"User {console_user_id} linked to actor (type: {session_service.actor_type})")
            _print_status(f"User {console_user_id} activated as {session_service.actor_type}", True)
        else:
            logger.debug(f"User {console_user_id} already linked to {session_service.actor_type}")
        
        # === ШАГ 4: Создание новой сессии ===
        # Каждый запуск консоли = новая сессия (не возобновляем старые)
        session_id = session_service.create_session()
        logger.info(f"New dialog session created: {session_id}")
        _print_status(f"Session #{session_id[:8]} started", True)
        
        # === ШАГ 5: Вывод приветствия (теперь все данные известны) ===
        _print_welcome(agent_version, console_user_id, session_service.actor_type)
        
        # === ШАГ 5.1: Создаём сессию ввода ===
        prompt_session = create_prompt_session()

        # === ШАГ 6: Основной цикл диалога ===
        while True:
            try:
                # Получаем многострочный ввод
                user_input = get_user_input(prompt_session)
                
                # Обработка команд выхода
                if user_input.lower() in ("exit", "выход"):
                    logger.info("User entered exit command")
                    exit_reason = "user_command"
                    break
                
                if not user_input:
                    continue
                
                logger.debug(f"Received user message: {len(user_input)} chars")
                
                # 6.1: Сохраняем сообщение в БД
                message_id = session_service.save_message(content=user_input)
                logger.debug(f"Message saved to DB with ID: {message_id[:8]}")
                
                # 6.2: Создаём задачу для оркестратора (пока заглушка)
                                                 
                # 6.3: Обновляем время активности сессии
                session_service.update_activity()

                # 6.4: Показываем статус обработки
                status_text = "⚙️  Agent is thinking..."
                print(f"\n{status_text}", end="", flush=True)

                # 6.5: Ожидаем ответ от агента
                agent_response = session_service.wait_for_agent_response(
                    user_message_id=message_id,
                    timeout_seconds=120
                )

                # 6.6: Заменяем статус на ответ
                if agent_response:
                    print(f"\r{' ' * len(status_text)}\r🤖 Agent: {agent_response}\n", end="", flush=True)
                    logger.info("Agent response received: {len(agent_response)} chars")
                else:
                    print(f"\r{' ' * len(status_text)}\r🤖 Agent: [No response received]\n", end="", flush=True)
                    logger.warning("Timeout waiting for agent response")

            except KeyboardInterrupt:
                logger.warning("Session interrupted by user (Ctrl+D)")
                print("\n\n[!] Interrupted by user")
                exit_reason = "user_exit"
                break
                
            except Exception as e:
                logger.error(f"Error in dialog loop: {e}", exc_info=True)
                _print_status(f"Processing error: {e}", False)
                exit_reason = "loop_error"
                continue
            
    except Exception as e:
        logger.critical(f"Critical error in console interface: {e}", exc_info=True)
        _print_status(f"Critical error: {e}", False)
        exit_reason = "critical_error"
    
    # === ШАГ 7: Завершение сессии ===
    finally:
        logger.info(f"Closing dialog session with reason: {exit_reason}")
        session_service.close_session(reason=exit_reason)
        _print_status("Session completed. Data saved to DB.", True)
        session_service.cleanup()
        logger.debug("Console interface resources released")

    return 0