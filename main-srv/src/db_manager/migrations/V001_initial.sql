-- =============================================
-- Migration: 001_initial.sql
-- Version: V001
-- Description: Creating basic PostgreSQL tables for system operation.
-- IMPORTANT! Do not change the creation order. Otherwise, the dependency links of table references via REFERENCES will break!
-- =============================================

-- Удалить datatypes в таблице public при очистке схем и пересозаднии БД в Dbeaver вручную!
-- Сначала удали ENUM в Postgre, если уже применялась такая миграция, при пересозаднии БД в Dbeaver вручную!


-- Создаем базовые схемы БД.
CREATE SCHEMA IF NOT EXISTS users;
CREATE SCHEMA IF NOT EXISTS dialogs;
CREATE SCHEMA IF NOT EXISTS orchestrator;
CREATE SCHEMA IF NOT EXISTS metrics;
CREATE SCHEMA IF NOT EXISTS common;



-- Блок 1: Пользовательские типы (ENUM) — ДО всех таблиц.
-- Создание ENUM для типа пользователей
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'actor_type') THEN
        CREATE TYPE actor_type AS ENUM (
            'system',     -- Сама система AGI (Kaya)
            'owner',      -- Владелец системы
            'superuser',  -- Суперпользователь с расширенными правами на диалоги
            'user',       -- Пользователь системы с ограничениями по цензуре
            'ai_agent'    -- Любой внешний AI-агент
        );
    END IF;
END $$;

COMMENT ON TYPE actor_type IS 'Типы участников диалога с AGI системой: 
system — Сама система AGI (Kaya), 
owner – Владелец системы, 
superuser – Суперпользователь с расширенными правами на диалоги, 
user – Пользователь системы с ограничениями по цензуре, 
ai_agent – Любой внешний AI – агент';

-- Создание ENUM для пола пользователей
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'gender_type') THEN
        CREATE TYPE gender_type AS ENUM ('male', 'female');
    END IF;
END $$;

COMMENT ON TYPE gender_type IS 'Пол пользователя: male - мужской, female - женский. Для Kaya по умолчанию female.';

-- Создание ENUM для типов источников сессий
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'external_source') THEN
        CREATE TYPE external_source AS ENUM (
            'console',        -- Консоль сервера
            'console_voice',  -- Голосовая консоль сервера
            'telegram',       -- Telegram мессенджер
            'api_rest'        -- REST API
        );
    END IF;
END $$;

COMMENT ON TYPE external_source IS 'Типы внешних источников данных для идентификации участников диалогов: 
console – Консоль сервера, 
console_voice – Голосовая консоль сервера, 
telegram – Telegram мессенджер, 
api_rest – REST API';

-- Создание ENUM для типа промпта (internal/external)
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'prompt_type') THEN
        CREATE TYPE prompt_type AS ENUM (
            'internal',   -- для внутреннего использования
            'external'    -- для внешних систем
        );
    END IF;
END $$;

COMMENT ON TYPE prompt_type IS 'Тип промпта: internal – для внутреннего использования, external - для внешних систем';

-- Создание ENUM для статуса промпта
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'prompt_status') THEN
        CREATE TYPE prompt_status AS ENUM (
            'testing',    -- тестирование
            'active',     -- активен
            'archived'    -- архивирован
        );
    END IF;
END $$;

COMMENT ON TYPE prompt_status IS 'Статус промпта: testing - тестирование, active - активен, archived - архивирован';

-- Создание ENUM для состояния комнат диалогов
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'room_status') THEN
        CREATE TYPE room_status AS ENUM (
            'used',       -- используется
            'unused'      -- не используется
        );
    END IF;
END $$;

COMMENT ON TYPE room_status IS 'Статус комнаты диалога: used - используется, unused - не используется';

-- Cоздание ENUM для статуса сессии
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'session_status') THEN
        CREATE TYPE session_status AS ENUM (
            'active',     -- Активная сессия, диалог продолжается
            'completed'   -- Завершенная сессия
        );
    END IF;
END $$;

COMMENT ON TYPE session_status IS 'Статус сессии диалога: active - активная, completed - завершенная';

-- Создание ENUM для статуса задач и шагов оркестратора
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'task_status') THEN
        CREATE TYPE task_status AS ENUM (
            'pending',   -- Ожидает выполнения
            'running',   -- Выполняется
            'completed', -- Успешно завершена
            'failed'     -- Завершилась с ошибкой
        );
    END IF;
END $$;

COMMENT ON TYPE task_status IS 'Статус выполнения задачи оркестратора: pending - ожидает, running - выполняется, completed - успешно завершена, failed - ошибка';

-- Создание ENUM для типа рассуждений
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'reasoning_content_type') THEN
        CREATE TYPE reasoning_content_type AS ENUM ('messages', 'reflection');
    END IF;
END $$;


-- Блок 1.1: Общая функция для обновления updated_at
CREATE OR REPLACE FUNCTION common.update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION common.update_updated_at_column IS 'Триггерная функция для автоматического обновления колонки updated_at';


-- Блок 2: Таблица участников диалогов (actors).
CREATE TABLE IF NOT EXISTS users.actors (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    type actor_type NOT NULL,
    name TEXT,
    gender gender_type,
    login TEXT UNIQUE,
    password_hash TEXT,
    email TEXT UNIQUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb, -- Метаданные и настройки (лимиты и прочее на будущее)
    access BOOLEAN NOT NULL DEFAULT true,
    verified BOOLEAN NOT NULL DEFAULT false,
    kaya_version TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ
);

-- Подробные комментарии к таблице
COMMENT ON TABLE users.actors IS 'Таблица участников диалогов (пользователи и Кая).';

-- Комментарии к колонкам
COMMENT ON COLUMN users.actors.id IS 'Уникальный идентификатор участника диалога (UUID)';
COMMENT ON COLUMN users.actors.type IS 'Тип участника диалога: system - Сама система AGI (Kaya), owner – Владелец системы, 
superuser – Суперпользователь с расширенными правами на диалоги, user – Пользователь системы с ограничениями по цензуре, ai_agent – Внешний AI – агент';
COMMENT ON COLUMN users.actors.name IS 'Человекочитаемое имя (задается вручную, либо автоматически ставится при выявлении системой)';
COMMENT ON COLUMN users.actors.gender IS 'Пол участника: male - мужской, female - женский. Для Каи по умолчанию - female.';
COMMENT ON COLUMN users.actors.login IS 'Уникальный логин пользователя для входа в систему';
COMMENT ON COLUMN users.actors.password_hash IS 'Хэш пароля пользователя (рекомендуется использовать bcrypt или argon2)';
COMMENT ON COLUMN users.actors.email IS 'Уникальный адрес электронной почты пользователя';
COMMENT ON COLUMN users.actors.metadata IS 'Структурированные дополнительные данные: настройки лимитов диалогов, предпочтения пользователя и прочее на будущее';
COMMENT ON COLUMN users.actors.access IS 'Разрешен доступ к диалогам: true - доступ разрешен, false - доступ заблокирован';
COMMENT ON COLUMN users.actors.verified IS 'Прошел ли пользователь регистрацию: true - верифицирован, false - ожидает подтверждения';
COMMENT ON COLUMN users.actors.kaya_version IS 'Версия агента глобально из pyproject.toml';
COMMENT ON COLUMN users.actors.created_at IS 'Дата и время создания записи пользователя';
COMMENT ON COLUMN users.actors.updated_at IS 'Дата и время последнего обновления записи пользователя';

-- Индексы для оптимизации запросов
CREATE INDEX idx_actors_type ON users.actors (type);
CREATE INDEX idx_actors_login ON users.actors (login);
CREATE INDEX idx_actors_email ON users.actors (email);
CREATE INDEX idx_actors_gender ON users.actors (gender);
CREATE INDEX idx_actors_access ON users.actors (access);
CREATE INDEX idx_actors_verified ON users.actors (verified);

-- GIN индекс для поиска по JSONB полю metadata
CREATE INDEX idx_actors_metadata ON users.actors USING gin (metadata);

-- Заполнение начальных системных акторов
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM users.actors WHERE type = 'system') THEN
        INSERT INTO users.actors (type, name, gender, metadata, access, verified, kaya_version, created_at) VALUES
        ('system'::actor_type, 'Кая', 'female'::gender_type,'{}'::jsonb, true, true, '1.0.0', now());
        INSERT INTO users.actors (type, metadata, access, verified, kaya_version, created_at) VALUES
        ('owner'::actor_type, '{}'::jsonb, true, true, '1.0.0', now());
    END IF;
END $$;

-- Подключение триггера updatet_at
DROP TRIGGER IF EXISTS trg_actors_update_updated_at ON users.actors;
CREATE TRIGGER trg_actors_update_updated_at
    BEFORE UPDATE ON users.actors
    FOR EACH ROW
    EXECUTE FUNCTION common.update_updated_at_column();