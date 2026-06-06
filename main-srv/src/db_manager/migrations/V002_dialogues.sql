-- =============================================
-- Migration: 002_dialogues.sql
-- Version: V002
-- Description: Introduces the consequences of the Physical Session Surface Dialogue.
-- Find a logical context break without breaking the connection.
-- =============================================

-- 1. ENUM для статусов диалогов
DO $$ BEGIN
IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'dialog_status') THEN
    CREATE TYPE public.dialog_status AS ENUM ('active', 'completed');
END IF;
END $$;

COMMENT ON TYPE dialog_status IS 'Статусы диалогов: active - активен, completed - завершен';


-- 2. ENUM для причин завершения диалогов (изолирован от сессий)
DO $$ BEGIN
IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'dialog_close_reason') THEN
    CREATE TYPE public.dialog_close_reason AS ENUM (
        'user_new_dialogue',  -- Явный запрос пользователя (Ctrl+N)
        'inactivity_timeout', -- Автоматическое закрытие по таймауту
        'session_end',        -- Завершение вместе с родительской сессией
        'system_restart'      -- Зависший диалог при рестарте сервера
    );
END IF;
END $$;

COMMENT ON TYPE dialog_close_reason IS 'Причины завершения диалогов: 
user_new_dialogue - явный запрос пользователя (Ctrl+N)
inactivity_timeout - автоматическое закрытие по таймауту
session_end - завершение вместе с родительской сессией
system_restart - зависший диалог при рестарте сервера';

-- 3. Создание таблицы диалогов
CREATE TABLE IF NOT EXISTS dialogs.dialogues (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES dialogs.sessions(id) ON DELETE CASCADE,
    actor_id UUID NOT NULL REFERENCES users.actors(id) ON DELETE CASCADE,
    status dialog_status NOT NULL DEFAULT 'active',
    reason dialog_close_reason,
    start_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    end_at TIMESTAMPTZ,
    last_activity_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    agent_version TEXT NOT NULL
);

COMMENT ON TABLE dialogs.dialogues IS 'Таблица логических диалогов. Функционирует поверх физической сессии.';
COMMENT ON COLUMN dialogs.dialogues.actor_id IS 'ID пользователя (владельца). Используется для быстрого поиска без JOIN сессий.';
COMMENT ON COLUMN dialogs.dialogues.status IS 'Статус: active - диалог идёт, completed - завершён.';
COMMENT ON COLUMN dialogs.dialogues.last_activity_at IS 'Метка времени последнего сообщения. Используется для расчёта таймаута неактивности.';

-- Индексы для оптимизации
CREATE INDEX idx_dialogues_actor_status ON dialogs.dialogues (actor_id, status) WHERE status = 'active';
CREATE INDEX idx_dialogues_session_status ON dialogs.dialogues (session_id, status);
CREATE INDEX idx_dialogues_agent_version ON dialogs.dialogues (agent_version);

-- Триггер auto-update для last_activity_at (не нужен, обновляется явно в коде, 
-- но можно использовать общий триггер если потребуется)
-- В данной архитектуре last_activity_at обновляется явно при проверке таймаута для точности.

-- 4. Добавление поля dialogue_id в таблицу сообщений
-- Поле обязательное, так как каждое сообщение принадлежит ровно одному диалогу
ALTER TABLE dialogs.row_messages 
    ADD COLUMN IF NOT EXISTS dialogue_id UUID REFERENCES dialogs.dialogues(id) ON DELETE RESTRICT;

-- Индекс для быстрого сбора контекста в границах одного диалога
CREATE INDEX IF NOT EXISTS idx_row_messages_dialogue_timestamp 
ON dialogs.row_messages (dialogue_id, timestamp ASC);