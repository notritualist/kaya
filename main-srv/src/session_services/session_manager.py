"""
main-srv/src/session_services/session_manager.py
Модуль управления сессиями диалогов для консольного интерфейса Kaya.
Отвечает за:
- Привязку пользователя Linux (console:) к актору 'owner' в БД
- Создание НОВОЙ сессии при каждом запуске консоли
- Сохранение сообщений пользователя в dialogs.messages
- Завершение сессии при выходе

Схема БД: миграция V001, V002
Таблицы: users.actors, users.actors_external_ids, dialogs.sessions, dialogs.messages
"""
version = "1.1.0"
description = "Менеджер сессий для консольного интерфейса Kaya (улучшенное логирование)"

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
    - Первый пользователь консоли Linux привязывается к актору type='owner' через external_ids, 
      последующие к типу 'user'
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
            try:
                self._conn = psycopg2.connect(**self.db_config)
                logger.debug("Соединение с БД успешно открыто")
            except psycopg2.Error as e:
                logger.error(f"❌ Ошибка подключения к PostgreSQL: {e}", exc_info=True)
                raise
        return self._conn

    def _query(self, sql: str, params: tuple = None, fetch: bool = False):
        """
        Выполняет SQL-запрос с авто-коммитом.
        
        ВАЖНО: commit() вызывается ДО return, чтобы данные сразу попадали в БД.
        """
        conn = self._get_conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                logger.debug(f"SQL: {sql[:100]}... Params: {params}")
                cur.execute(sql, params or ())
                result = cur.fetchone() if fetch else None
                conn.commit()
                logger.debug("✅ Запрос выполнен успешно")
                return result
        except psycopg2.Error as e:
            conn.rollback()
            logger.error(
                f"❌ Ошибка БД: {e}\nSQL: {sql}\nParams: {params}", 
                exc_info=True
            )
            raise
        except Exception as e:
            conn.rollback()
            logger.error(
                f"❌ Неожиданная ошибка при выполнении запроса: {e}\nSQL: {sql}", 
                exc_info=True
            )
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
        logger.info(f"🔗 Проверяю привязку {self.console_user_id}")
        
        try:
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
                self.actor_external_id = str(existing['id'])
                logger.info(
                    f"✅ {self.console_user_id} уже привязан к {self.actor_type}#{self.actor_id[:8]}, "
                    f"external_id={self.actor_external_id[:8]}"
                )
                return False
            
            # === ШАГ 2: Пользователь новый — определяем, к кому привязывать ===
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
                logger.info(f"⚠️ Owner занят {existing_owner['source_id']}. Создаю user для {self.console_user_id}")
                
                new_actor = self._query("""
                    INSERT INTO users.actors (type, metadata, access, verified, kaya_version)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id
                """, params=('user', '{}', True, True, self.kaya_version), fetch=True)
                
                self.actor_id = str(new_actor['id'])
                self.actor_type = 'user'
            else:
                owner_row = self._query("""
                    SELECT id FROM users.actors 
                    WHERE type = 'owner'::actor_type 
                    ORDER BY created_at ASC 
                    LIMIT 1
                """, fetch=True)
                
                if not owner_row:
                    logger.critical("❌ Актер 'owner' не найден в БД")
                    raise RuntimeError("Не найден актор owner")
                
                self.actor_id = str(owner_row['id'])
                self.actor_type = 'owner'
            
            # === ШАГ 3: Создаём привязку внешнего ID ===
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
            ), fetch=True)
            
            if ext_row:
                self.actor_external_id = str(ext_row['id'])
                logger.debug(f"✅ actor_external_id сохранён: {self.actor_external_id[:8]}")
            
            logger.info(f"✅ Привязка создана: {self.actor_type}#{self.actor_id[:8]} ↔ {self.console_user_id}")
            return True
            
        except Exception as e:
            logger.error(f"❌ Ошибка при привязке актора: {e}", exc_info=True)
            raise

    @staticmethod
    def close_dangling_sessions(db_config: dict) -> int:
        """
        Завершает «зависшие» активные сессии при перезапуске системы.
        
        Args:
            db_config: параметры подключения к PostgreSQL
            
        Returns:
            int: количество закрытых сессий
        """
        logger.info("🔄 Проверка зависших сессий...")
        try:
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
                        logger.warning(f"⚠️ Завершено {count} зависших сессий при старте")
                    else:
                        logger.debug("✅ Зависших сессий не найдено")
                    return count
        except Exception as e:
            logger.error(f"❌ Ошибка при закрытии зависших сессий: {e}", exc_info=True)
            return 0         

    def create_session(self, room_name: str = "open_dialogue") -> str:
        """
        Создаёт НОВУЮ сессию диалога.
        
        Важно: каждый запуск консоли = новая сессия (не resume).
        Логика комнат:
        - last_room = current_room из предыдущей завершённой сессии пользователя
        - current_room = room_name (по умолчанию "open_dialogue")
        
        Args:
            room_name: имя комнаты из dialogs.rooms (по умолчанию "open_dialogue")
        
        Returns:
            str: UUID новой сессии
        """
        if self.session_id:
            logger.warning(f"⚠️ Сессия уже активна: {self.session_id[:8]}")
            raise RuntimeError("Сессия уже активна")
        if not self.actor_id:
            logger.error("❌ actor_id не установлен. Вызовите ensure_actor_linked()")
            raise RuntimeError("Сначала вызовите ensure_actor_linked()")
        
        logger.info(f"🆕 Создаю сессию: комната={room_name}")
        
        try:
            # === ЗАПРОС 1: Получаем ID комнаты (current_room) ===
            room_row = self._query("""
                SELECT id FROM dialogs.rooms 
                WHERE name = %s AND status = 'used'::room_status
            """, params=(room_name,), fetch=True)
            
            if not room_row:
                logger.error(f"❌ Комната '{room_name}' не найдена или неактивна")
                raise ValueError(f"Комната '{room_name}' не найдена или неактивна")
            
            current_room_id = str(room_row['id'])
            logger.debug(f"✅ ID комнаты получен: {current_room_id[:8]}")

            # === ЗАПРОС 2: Создаём сессию ===
            # last_room берётся подзапросом из завершённых сессий.
            # Если их нет — COALESCE подставит текущую комнату.
            row = self._query("""
                INSERT INTO dialogs.sessions 
                (
                    actor_id, 
                    actor_external_id, 
                    status, 
                    last_room, 
                    current_room, 
                    kaya_version
                )
                VALUES (
                    %s, 
                    %s, 
                    'active', 
                    COALESCE(
                        (
                            SELECT current_room 
                            FROM dialogs.sessions 
                            WHERE actor_id = %s 
                              AND status = 'completed'::session_status 
                            ORDER BY closed_at DESC 
                            LIMIT 1
                        ), 
                        %s
                    ),
                    %s, 
                    %s
                )
                RETURNING id
            """, params=(
                self.actor_id,
                self.actor_external_id,
                self.actor_id,
                current_room_id,
                current_room_id,
                self.kaya_version
            ), fetch=True)
            
            if not row:
                logger.error("❌ Не удалось создать сессию в БД (нет RETURNING id)")
                raise RuntimeError("Не удалось создать сессию в БД")
            
            self.session_id = str(row['id'])
            logger.info(f"✅ Сессия создана: {self.session_id[:8]}")
            return self.session_id
            
        except ValueError as e:
            logger.error(f"❌ Ошибка валидации: {e}")
            raise
        except Exception as e:
            logger.error(f"❌ Ошибка при создании сессии: {e}", exc_info=True)
            raise

    def save_message(self, content: str) -> str:
        """
        Сохраняет сообщение пользователя в dialogs.messages.
        
        ВАЖНО: room_id берётся из current_room текущей сессии (не параметр!).
        Это гарантирует консистентность: все сообщения сессии → одна комната.
        
        Заполняет поля согласно миграции V001/V002:
        - actor_id, actor_type (из self.actor_type: 'owner' или 'user')
        - session_id, room_id (из current_room сессии)
        - row_text, token_count
        - kaya_version, timestamp
        
        Args:
            content: текст сообщения
        
        Returns:
            str: UUID сохранённого сообщения
        """
        if not self.session_id:
            logger.error("❌ Сессия не создана. Вызовите create_session()")
            raise RuntimeError("Сессия не создана")
        if not self.actor_id:
            logger.error("❌ actor_id не установлен")
            raise RuntimeError("actor_id не установлен")
        
        logger.debug(f"💬 Сохраняю сообщение: {len(content)} символов")
        
        try:
            # Считаем токены через Qwen3-токенизатор
            token_count = count_tokens_qwen(content)
            logger.debug(f"🔢 Токенов в сообщении: {token_count}")
            
            # === ЗАПРОС 1: Берём current_room из текущей сессии ===
            session_row = self._query("""
                SELECT current_room FROM dialogs.sessions WHERE id = %s
            """, params=(self.session_id,), fetch=True)
            
            if not session_row or not session_row['current_room']:
                logger.error(f"❌ У сессии {self.session_id} не установлен current_room")
                raise ValueError(f"У сессии {self.session_id} не установлен current_room")
            
            room_id = str(session_row['current_room'])
            logger.debug(f"📍 room_id из сессии: {room_id[:8]}")

            # === ЗАПРОС 2: Вычисляем parent_message_id и user_think_latency ===
            parent_message_id: Optional[str] = None
            user_think_latency: Optional[float] = None

            with psycopg2.connect(**self.db_config) as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
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
                        logger.debug(
                            f"🔗 parent_message_id: {parent_message_id[:8]}, "
                            f"user_think_latency: {user_think_latency:.2f} сек"
                        )
            
            # === ЗАПРОС 3: Вставляем сообщение ===
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
                parent_message_id,
                self.actor_id,
                self.actor_type,
                self.session_id,
                room_id,              # ← из current_room сессии
                content,
                token_count,
                user_think_latency,
                self.kaya_version,
                datetime.now(timezone.utc),
                None,
                None
            ), fetch=True)
            
            if not row:
                logger.error("❌ Не удалось сохранить сообщение (нет RETURNING id)")
                raise RuntimeError("Не удалось сохранить сообщение")
            
            msg_id = str(row['id'])
            logger.debug(f"✅ Сообщение сохранено: {msg_id[:8]}")
            return msg_id
            
        except ValueError as e:
            logger.error(f"❌ Ошибка валидации: {e}")
            raise
        except Exception as e:
            logger.error(f"❌ Ошибка при сохранении сообщения: {e}", exc_info=True)
        raise

    def update_activity(self):
        """Обновляет updated_at текущей сессии."""
        if not self.session_id:
            logger.debug("⚠️ Нет активной сессии для обновления активности")
            return
        try:
            self._query("""
                UPDATE dialogs.sessions SET updated_at = NOW() WHERE id = %s
            """, params=(self.session_id,))
            logger.debug(f"✅ Активность сессии {self.session_id[:8]} обновлена")
        except Exception as e:
            logger.error(f"❌ Ошибка при обновлении активности: {e}", exc_info=True)
            raise

    def close_session(self):
        """Завершает сессию: status='completed', closed_at=NOW()."""
        if not self.session_id:
            logger.debug("ℹ️ Нет активной сессии для закрытия")
            return
        
        logger.info(f"🔒 Завершаю сессию {self.session_id[:8]}")
        try:
            self._query("""
                UPDATE dialogs.sessions 
                SET status = 'completed'::session_status, closed_at = NOW()
                WHERE id = %s
            """, params=(self.session_id,))
            logger.info(f"✅ Сессия {self.session_id[:8]} завершена")
            self.session_id = None
        except Exception as e:
            logger.error(f"❌ Ошибка при закрытии сессии: {e}", exc_info=True)
            raise

    def cleanup(self):
        """Закрывает соединение с БД."""
        if self._conn and not self._conn.closed:
            try:
                self._conn.close()
                logger.debug("✅ Соединение с БД закрыто")
            except Exception as e:
                logger.error(f"⚠️ Ошибка при закрытии соединения: {e}")
            finally:
                self._conn = None
        else:
            logger.debug("ℹ️ Соединение с БД уже закрыто")
    
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if self.session_id:
                self.close_session()
        except Exception as e:
            logger.error(f"❌ Ошибка при закрытии сессии в контекстном менеджере: {e}")
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
        
        logger.debug(f"⏳ Ожидание ответа на сообщение {user_message_id[:8]}...")
        
        while True:
            elapsed: float = time.time() - start_time
            if elapsed >= timeout_seconds:
                logger.error(f"❌ Таймаут ожидания ответа: {timeout_seconds} сек")
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
                            logger.debug(f"✅ Ответ получен: {len(row['row_text'])} символов")
                            return row["row_text"]
            except Exception as e:
                logger.warning(f"⚠️ Ошибка при ожидании ответа: {e}")
            
            remaining: float = timeout_seconds - elapsed
            time.sleep(min(0.5, remaining))