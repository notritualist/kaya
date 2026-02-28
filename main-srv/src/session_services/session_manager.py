"""
main-srv/src/session_services/session_manager.py

Модуль управления сессиями диалогов для консольного интерфейса Kaya.

Отвечает за:
- Привязку пользователя Linux (console:<username>) к актору 'owner' в БД
- Создание НОВОЙ сессии при каждом запуске консоли
- Сохранение сообщений пользователя в dialogs.messages
- Завершение сессии при выходе

Схема БД: миграция V001
Таблицы: users.actors, users.actors_external_ids, dialogs.sessions, dialogs.messages
"""

__version__ = "1.0.0"
__description__ = "Менеджер сессий для консольного интерфейса Kaya"

import logging
import psycopg2
from psycopg2.extras import RealDictCursor
from typing import Optional
from datetime import datetime, timezone
from pathlib import Path
# Импортируем подсчёт токенов (путь относительно этого файла)
# Структура: main-srv/src/session_services/session_manager.py
# tokens_counter.py лежит в: main-srv/src/services/tokens_counter.py
from services.tokens_counter import count_tokens_qwen

# Логгер модуля — подхватит настройки из main.py
logger = logging.getLogger(__name__)


class SessionManager:
    """
    Менеджер сессий для интерфейсов.
    
    Принцип работы:
    - Каждый запуск консоли = новая сессия в БД (не возобновляем старые)
    - Первый пользователь консоли Linux привязывается к актору type='owner' через external_ids, последующие к типу 'user'
    - Все сообщения пишутся в dialogs.messages с полным контекстом
    
    Атрибуты:
        db_config (dict): параметры подключения к PostgreSQL
        kaya_version (str): версия агента из pyproject.toml
        console_user_id (str): идентификатор в формате "console:<username>"
        session_id (Optional[str]): UUID текущей сессии
        actor_id (Optional[str]): UUID текущего актора (owner или user)
        actor_type (str): Тип актора: 'owner' или 'user'
        _conn: кэш соединения с БД
    """
    
    def __init__(self, db_config: dict, kaya_version: str, console_user_id: str):
        """
        Инициализация менеджера сессий.
        
        Args:
            db_config: dict с параметрами подключения (host, port, dbname, user, password)
            kaya_version: строка версии из pyproject.toml
            console_user_id: идентификатор пользователя, например "console:debian"
        """
        self.db_config = db_config
        self.kaya_version = kaya_version
        self.console_user_id = console_user_id
        
        # Поля заполняются в процессе работы
        self.session_id: Optional[str] = None
        self.actor_id: Optional[str] = None      # UUID актора (owner или user)
        self.actor_type: str = 'owner'           # Тип: 'owner' или 'user'
        self.actor_external_id: Optional[str] = None  # кэш внешнего ID
        self._conn = None
                
        logger.debug(f"SessionManager создан для {console_user_id}")
            
    def _get_conn(self):
        """Возвращает активное соединение с БД, создавая при необходимости."""
        if self._conn is None or self._conn.closed:
            logger.debug("Открываю соединение с PostgreSQL")
            self._conn = psycopg2.connect(**self.db_config)
        return self._conn
    
    def _query(self, sql: str, params: tuple = None, fetch: bool = False):
        """
        Выполняет SQL-запрос с авто-коммитом.
        
        ВАЖНО: commit() вызывается ДО return, чтобы данные сразу попадали в БД.
        """
        conn = self._get_conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, params or ())
                result = cur.fetchone() if fetch else None
                conn.commit()  # ← КОММИТ ПЕРЕД ВОЗВРАТОМ (был после return — баг!)
                return result
        except psycopg2.Error as e:
            conn.rollback()
            logger.error(f"Ошибка БД: {e}\nSQL: {sql}\nParams: {params}", exc_info=True)
            raise
        
    def ensure_actor_linked(self) -> bool:
        """
        Привязывает текущего пользователя консоли к актору.
        Логика:
        - Сначала проверяем, есть ли уже привязка у этого console_user_id → если да, возвращаем False
        - Если нет → проверяем, занят ли owner ДРУГИМ console-юзером
        - Если owner свободен → привязываем к owner
        - Если owner занят → создаём нового актора type='user' и привязываем к нему
        Returns:
            bool: True, если привязка создана сейчас; False, если уже была
        """
        logger.debug(f"Проверяю привязку {self.console_user_id}")
        
        # === ШАГ 1: ПРОВЕРЯЕМ, ЕСТЬ ЛИ УЖЕ ПРИВЯЗКА У ЭТОГО ПОЛЬЗОВАТЕЛЯ ===
        existing = self._query("""
            SELECT aei.id, aei.actor_id, a.type
            FROM users.actors_external_ids aei
            JOIN users.actors a ON aei.actor_id = a.id
            WHERE aei.source = 'console'::external_source 
            AND aei.source_id = %s
        """, params=(self.console_user_id,), fetch=True)
        
        if existing:
            self.actor_id = str(existing['actor_id'])
            self.actor_type = str(existing['type'])
            self.actor_external_id = str(existing['id'])  # ← ДОБАВИЛИ: загружаем внешний ID
            logger.debug(f"{self.console_user_id} уже привязан к {self.actor_type}#{self.actor_id}, external_id={self.actor_external_id[:8]}")
            return False
        
        # === ШАГ 2: Пользователь новый — определяем, к кому привязывать ===
        # Проверяем, занят ли owner ДРУГИМ консольным пользователем
        existing_owner = self._query("""
            SELECT aei.source_id, aei.actor_id
            FROM users.actors_external_ids aei
            JOIN users.actors a ON aei.actor_id = a.id
            WHERE a.type = 'owner'::actor_type 
            AND aei.source = 'console'::external_source
            AND aei.source_id != %s
            LIMIT 1
        """, params=(self.console_user_id,), fetch=True)
        
        if existing_owner:
            # owner уже занят другим → создаём нового user
            logger.info(f"Owner занят {existing_owner['source_id']}. Создаю user для {self.console_user_id}")
            
            new_actor = self._query("""
                INSERT INTO users.actors (type, metadata, access, verified, kaya_version)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """, params=('user', '{}', True, True, self.kaya_version), fetch=True)
            
            self.actor_id = str(new_actor['id'])
            self.actor_type = 'user'
        else:
            # owner свободен → привязываем к нему
            owner_row = self._query("""
                SELECT id FROM users.actors 
                WHERE type = 'owner'::actor_type 
                ORDER BY created_at ASC 
                LIMIT 1
            """, fetch=True)
            
            if not owner_row:
                logger.critical("Актор 'owner' не найден в БД")
                raise RuntimeError("Не найден актор owner")
            
            self.actor_id = str(owner_row['id'])
            self.actor_type = 'owner'
        
        ## === ШАГ 3: Создаём привязку внешнего ID ===
        ext_row = self._query("""
            INSERT INTO users.actors_external_ids 
            (actor_id, source, source_id, authorized, kaya_version)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
        """, params=(
            self.actor_id,
            'console',
            self.console_user_id,
            True,
            self.kaya_version
        ), fetch=True)  # ← ДОБАВИЛИ: RETURNING + fetch=True
        
        # Сохраняем actor_external_id в кэш класса
        if ext_row:
            self.actor_external_id = str(ext_row['id'])
            logger.debug("actor_external_id сохранён: %s", self.actor_external_id[:8])
        
        logger.info(f"Привязка создана: {self.actor_type}#{self.actor_id} ↔ {self.console_user_id}")
        return True

    @staticmethod
    def close_dangling_sessions(db_config: dict) -> int:
        """
        Завершает «зависшие» активные сессии при перезапуске системы.
        
        Args:
            db_config: параметры подключения к PostgreSQL
            
        Returns:
            int: количество закрытых сессий
        """
        with psycopg2.connect(**db_config) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE dialogs.sessions
                    SET 
                        status = 'completed'::session_status,
                        closed_at = NOW(),
                        updated_at = NOW()
                    WHERE status = 'active'
                """)
                count = cur.rowcount
                conn.commit()
                
                if count > 0:
                    logger.warning("🔄 Завершено %d зависших сессий при старте", count)
                return count         
 
    def create_session(self, room_name: str = "open_dialogue") -> str:
        """
        Создаёт НОВУЮ сессию диалога.
        
        Важно: каждый запуск консоли = новая сессия (не resume).
        
        Args:
            room_name: имя комнаты из dialogs.rooms (по умолчанию "open_dialogue")
        
        Returns:
            str: UUID новой сессии
        """
        if self.session_id:
            raise RuntimeError("Сессия уже активна")
        if not self.actor_id:
            raise RuntimeError("Сначала вызовите ensure_actor_linked()")
        
        logger.debug(f"Создаю сессию: комната={room_name}")
        
        # Находим ID комнаты
        room_row = self._query("""
            SELECT id FROM dialogs.rooms 
            WHERE name = %s AND status = 'used'::room_status
        """, params=(room_name,), fetch=True)
        
        if not room_row:
            raise ValueError(f"Комната '{room_name}' не найдена или неактивна")
        room_id = str(room_row['id'])

        # Создаём сессию с actor_external_id из кэша (уже привязан к источнику)
        row = self._query("""
            INSERT INTO dialogs.sessions 
            (actor_id, actor_external_id, status, last_room, kaya_version)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
        """, params=(
            self.actor_id,
            self.actor_external_id,  # ← Берём из кэша (привязан к console + user_id)
            'active',
            room_id,
            self.kaya_version
        ), fetch=True)
        
        if not row:
            raise RuntimeError("Не удалось создать сессию в БД")
        
        self.session_id = str(row['id'])
        logger.info("Сессия создана: %s", self.session_id[:8])
        return self.session_id
    
    def save_message(self, content: str, room_name: str = "open_dialogue") -> str:
        """
        Сохраняет сообщение пользователя в dialogs.messages.
        
        Заполняет поля согласно миграции V001:
        - actor_id, actor_type (из self.actor_type: 'owner' или 'user')
        - session_id, room_id
        - row_text, token_count
        - kaya_version, timestamp
        
        Args:
            content: текст сообщения
            room_name: имя комнаты
        
        Returns:
            str: UUID сохранённого сообщения
        """
        if not self.session_id:
            raise RuntimeError("Сессия не создана")
        if not self.actor_id:
            raise RuntimeError("actor_id не установлен")
        
        # Считаем токены через Qwen3-токенизатор
        token_count = count_tokens_qwen(content)
        logger.debug(f"Токенов в сообщении: {token_count}")
        
        # Находим room_id
        room_row = self._query("""
            SELECT id FROM dialogs.rooms WHERE name = %s
        """, params=(room_name,), fetch=True)
        
        if not room_row:
            raise ValueError(f"Комната '{room_name}' не найдена")
        
        room_id = str(room_row['id'])

        # === === === === === === === === === === === === === === === === === === ===
        # === ВЫЧИСЛЯЕМ parent_message_id И user_think_latency (ТОЛЬКО в рамках ТЕКУЩЕЙ сессии!) ===
        # === === === === === === === === === === === === === === === === === === ===
        # Логика:
        # - Ищем последнее релевантное событие ТОЛЬКО в текущей сессии (не комнате!)
        # - Если это первое сообщение в сессии → parent = NULL, latency = NULL
        
        parent_message_id: Optional[str] = None
        user_think_latency: Optional[float] = None

        with psycopg2.connect(**self.db_config) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Находим последнее релевантное событие в ТЕКУЩЕЙ сессии:
                # 1. Ответ системы, где parent — сообщение этого пользователя в ЭТОЙ сессии, ИЛИ
                # 2. Предыдущее сообщение этого пользователя в ЭТОЙ сессии
                cur.execute("""
                    SELECT m.id, m.timestamp
                    FROM dialogs.messages m
                    WHERE m.session_id = %s
                        AND (
                            (m.actor_type = 'system' 
                            AND m.parent_message_id IN (
                                SELECT id FROM dialogs.messages 
                                WHERE session_id = %s AND actor_id = %s
                            )
                            )
                            OR (m.actor_id = %s AND m.actor_type != 'system')
                        )
                    ORDER BY m.timestamp DESC
                    LIMIT 1
                """, (self.session_id, self.session_id, self.actor_id, self.actor_id))
                
                prev_row = cur.fetchone()
                if prev_row:
                    parent_message_id = str(prev_row['id'])
                    prev_timestamp = prev_row['timestamp']
                    current_timestamp = datetime.now(timezone.utc)
                    user_think_latency = (current_timestamp - prev_timestamp).total_seconds()
                    logger.debug("parent_message_id: %s, user_think_latency: %.2f сек", 
                            parent_message_id[:8], user_think_latency)
                # else: первое сообщение в сессии → оба значения остаются None
       
        # Вставляем сообщение
        row = self._query("""
            INSERT INTO dialogs.messages 
            (
                parent_message_id,
                actor_id, 
                actor_type, 
                session_id, 
                room_id,
                row_text, 
                token_count, 
                answer_latency,
                kaya_version, 
                timestamp,
                orchestrator_step_id,
                llm_metric_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, params=(
            parent_message_id,   # ← NULL для первого сообщения в сессии
            self.actor_id,
            self.actor_type,
            self.session_id,     # ← текущая сессия
            room_id,
            content,
            token_count,
            user_think_latency,  # ← NULL для первого сообщения в сессии)
            self.kaya_version,
            datetime.now(timezone.utc),
            None,  # orchestrator_step_id = NULL для пользовательских сообщений
            None   # llm_metric_id = NULL для пользовательских сообщений
        ), fetch=True)
        
        if not row:
            raise RuntimeError("Не удалось сохранить сообщение")
        
        msg_id = str(row['id'])
        logger.debug(f"Сообщение сохранено: {msg_id}")
        return msg_id
    
    def update_activity(self):
        """Обновляет updated_at текущей сессии."""
        if not self.session_id:
            return
        self._query("""
            UPDATE dialogs.sessions SET updated_at = NOW() WHERE id = %s
        """, params=(self.session_id,))
        logger.debug(f"Активность сессии {self.session_id} обновлена")
    
    def close_session(self):
        """Завершает сессию: status='completed', closed_at=NOW()."""
        if not self.session_id:
            logger.debug("Нет активной сессии для закрытия")
            return
        
        logger.info(f"Завершаю сессию {self.session_id}")
        self._query("""
            UPDATE dialogs.sessions 
            SET status = 'completed'::session_status, closed_at = NOW()
            WHERE id = %s
        """, params=(self.session_id,))
        self.session_id = None
    
    def cleanup(self):
        """Закрывает соединение с БД."""
        if self._conn and not self._conn.closed:
            self._conn.close()
            logger.debug("Соединение с БД закрыто")
        self._conn = None
    
    # Контекстный менеджер (опционально)
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if self.session_id:
                self.close_session()
        finally:
            self.cleanup()
        return False
        
    def wait_for_agent_response(self, user_message_id: str, timeout_seconds: int = 120) -> str:
        """
        Блокирующее ожидание появления ответа агента в БД.
        
        Проверяет dialogs.messages на появление сообщения с:
        - parent_message_id = user_message_id
        - actor_type = 'system'
        
        Args:
            user_message_id (str): ID сообщения пользователя
            timeout_seconds (int): Максимальное время ожидания (сек)
            
        Returns:
            str: Чистый текст ответа агента (без <think>)
            
        Raises:
            TimeoutError: Если ответ не появился за timeout_seconds
        """
        import time
        start_time: float = time.time()
        
        while True:
            elapsed: float = time.time() - start_time
            if elapsed >= timeout_seconds:
                raise TimeoutError(
                    f"Ответ не получен за {timeout_seconds} сек на сообщение {user_message_id}"
                )
            
            try:
                with psycopg2.connect(**self.db_config) as conn:
                    with conn.cursor(cursor_factory=RealDictCursor) as cur:
                        cur.execute("""
                            SELECT row_text
                            FROM dialogs.messages
                            WHERE parent_message_id = %s
                              AND actor_type = 'system'::actor_type
                            ORDER BY timestamp DESC
                            LIMIT 1
                        """, (user_message_id,))
                        row = cur.fetchone()
                        if row:
                            # Возвращаем чистый ответ (без <think> — он уже в reasonings)
                            return row["row_text"]
            except Exception as e:
                logger.warning("⚠️ Ошибка при ожидании ответа: %s", e)
            
            # Пауза перед следующей проверкой
            remaining: float = timeout_seconds - elapsed
            time.sleep(min(0.5, remaining))
