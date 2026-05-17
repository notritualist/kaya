-- =============================================
-- Migration: 003_pseudohormonal_system.sql
-- Version: V003
-- Description: Pseudohormonal system for embodied AI agent.
-- Implements baseline/momentary dynamics, shutdown reasons, lifecycle,
-- and text projection via self_knowledge.
-- =============================================

-- =============================================
-- 0. Ensure pgvector extension (required for halfvec)
-- =============================================
CREATE EXTENSION IF NOT EXISTS vector;
COMMENT ON EXTENSION vector IS 'pgvector for vector similarity search and halfvec type';

-- Схема для ПГС
CREATE SCHEMA IF NOT EXISTS state;

-- =============================================
-- 1. ENUM для типов выключения (shutdown)
-- =============================================
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'shutdown_type') THEN
        CREATE TYPE state.shutdown_type AS ENUM (
            'maintenance',       -- Плановое обслуживание оборудования
            'crash',             -- Аварийное завершение
            'forced_shutdown',   -- Принудительное выключение
            'user_absence',      -- Длительное отсутствие пользователя
            'agent_modification' -- Доработка и тестирование агента
        );
    END IF;
END $$;

COMMENT ON TYPE state.shutdown_type IS 'Тип причины отключения/простоя агента';

-- =============================================
-- 2. ENUM для глобальных состояний агента
-- =============================================
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'agent_state_type') THEN
        CREATE TYPE state.agent_state_type AS ENUM (
            'off',     -- Агент выключен (сервера не работают)
            'sleep',   -- Нет диалогов более X минут (сон)
            'active'   -- В диалоге (активность менее X минут)
        );
    END IF;
END $$;

COMMENT ON TYPE state.agent_state_type IS 'Макросостояния: off – не запущен, sleep – сон, active – в диалоге';

-- =============================================
-- 3. ENUM для причин смены состояния (lifecycle)
-- =============================================
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'lifecycle_change_reason') THEN
        CREATE TYPE state.lifecycle_change_reason AS ENUM (
            'user_activity',        -- Сообщение пользователя в состоянии active
            'agent_activity',       -- Сообщение от агента в состоянии active
            'inactivity_timeout',   -- Долгое бездействие → sleep
            'shutdown_command',     -- Команда выключения → off
            'startup',              -- Запуск сервера → off → active/sleep?
            'crash_recovery',       -- Восстановление после креша
            'user_wake_up',         -- Пробуждение из sleep сообщеним пользователя
            'agent_wake_up'         -- Пробуждение из sleep инициацией сообщения агентом  
        );
    END IF;
END $$;

COMMENT ON TYPE state.lifecycle_change_reason IS 'Причина перехода между состояниями off/sleep/active';

-- =============================================
-- 4. Таблица настроек (с начальными значениями по умолчанию)
-- =============================================
CREATE TABLE IF NOT EXISTS state.settings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    param_name TEXT NOT NULL UNIQUE,
    description TEXT,
    value_float REAL,
    value_text TEXT,
    value_json JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE state.settings IS 'Настройки псевдогормональной системы: времена затухания (tau), уставки (setpoint), 
пороги для текстовой проекции, коэффициенты осаждения (alpha), порог бездействия для sleep, параметры RFF (gamma, seed, omega, bias).';
COMMENT ON COLUMN state.settings.id IS 'Уникальный идентификатор записи';
COMMENT ON COLUMN state.settings.param_name IS 'Имя параметра (например tau_cortisol_sec, rff_gamma, cortisol_setpoint). Уникально.';
COMMENT ON COLUMN state.settings.description IS 'Человекочитаемое описание параметра.';
COMMENT ON COLUMN state.settings.value_float IS 'Числовое значение для параметров с плавающей точкой (tau, setpoint, alpha, пороги, seed).';
COMMENT ON COLUMN state.settings.value_text IS 'Строковое значение (зарезервировано, обычно NULL)';
COMMENT ON COLUMN state.settings.value_json IS 'Сложные структуры: матрица omega и вектор bias для RFF (массивы).';
COMMENT ON COLUMN state.settings.created_at IS 'Дата и время создания записи';
COMMENT ON COLUMN state.settings.updated_at IS 'Время последнего обновления записи. Автоматически обновляется через триггер common.update_updated_at_column 
миграции V001.';

-- Вставка параметров по умолчанию с описаниями
INSERT INTO state.settings (param_name, value_float, value_text, value_json, description) VALUES
    -- Времена затухания (секунды)
    ('tau_cortisol_sec',   1200.0, NULL, NULL, 'Время затухания кортизола в секундах. Определяет, как быстро стресс снижается после события.'),
    ('tau_dopamine_sec',   300.0,  NULL, NULL, 'Время затухания дофамина в секундах. Влияет на скорость угасания мотивации.'),
    ('tau_oxytocin_sec',   1800.0, NULL, NULL, 'Время затухания окситоцина в секундах. Долгий спад социального доверия.'),
    -- Уставки (setpoint)
    ('cortisol_setpoint',  20.0,   NULL, NULL, 'Базовый уровень кортизола в состоянии покоя. Нормальный фон стресса.'),
    ('dopamine_setpoint',  60.0,   NULL, NULL, 'Идеальный уровень дофамина при отсутствии внешних стимулов.'),
    ('oxytocin_setpoint',  50.0,   NULL, NULL, 'Уставка окситоцина – комфортный уровень социальной близости.'),
    -- Пороги для текстовой проекции (0..100)
    ('threshold_cortisol', 70.0,   NULL, NULL, 'При кортизоле > этого порога в промпт добавляется описание тревоги.'),
    ('threshold_oxytocin', 70.0,   NULL, NULL, 'При окситоцине > этого порога добавляется описание тепла и близости.'),
    ('threshold_dopamine', 70.0,   NULL, NULL, 'При дофамине > этого порога добавляется описание понимания и удовлетворения.'),
    -- Коэффициенты осаждения в baseline (α)
    ('alpha_session_end',  0.2,    NULL, NULL, 'Вес финального momentary при осаждении в baseline после завершения сессии.'),
    ('alpha_hourly_drift', 0.01,   NULL, NULL, 'Микро-осаждение текущего momentary в baseline каждый час активности.'),
    -- Порог бездействия для перехода в sleep (минуты)
    ('inactivity_sleep_minutes', 5.0, NULL, NULL, 'Число минут без сообщений пользователя, после которых агент переходит из active в sleep.'),
    -- Параметры RFF (Random Fourier Features)
    ('rff_gamma',          0.1,    NULL, NULL, 'Параметр гамма для гауссова ядра при генерации RFF-признаков.'),
    ('rff_seed',           42.0,   NULL, NULL, 'Seed для воспроизводимости случайных матриц omega и bias.'),
    ('rff_omega',          NULL,   NULL, '[]'::jsonb, 'Матрица случайных проекций (размерность 64x4). Генерируется кодом.'),
    ('rff_bias',           NULL,   NULL, '[]'::jsonb, 'Вектор случайных сдвигов (размерность 64). Генерируется кодом.')
ON CONFLICT (param_name) DO NOTHING;

-- Триггер обновления updated_at (использует существующую общую функцию common.update_updated_at_column)
DROP TRIGGER IF EXISTS trigger_settings_updated_at ON state.settings;
CREATE TRIGGER trigger_settings_updated_at
    BEFORE UPDATE ON state.settings
    FOR EACH ROW
    EXECUTE FUNCTION common.update_updated_at_column();

COMMENT ON TRIGGER trigger_settings_updated_at ON state.settings IS 'Автоматически обновляет поле updated_at при изменении настроек.';


-- =============================================
-- 5. Причины изменения baseline (справочник)
-- =============================================
CREATE TABLE IF NOT EXISTS state.baseline_change_reasons (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    reason_code TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE state.baseline_change_reasons IS 'Причины изменения долговременного гормонального фона';
COMMENT ON COLUMN state.baseline_change_reasons.id IS 'Уникальный идентификатор';
COMMENT ON COLUMN state.baseline_change_reasons.reason_code IS 'Код причины (session_end, hourly_drift, shutdown_*)';
COMMENT ON COLUMN state.baseline_change_reasons.description IS 'Текстовое описание причины';
COMMENT ON COLUMN state.baseline_change_reasons.created_at IS 'Дата создания записи';
COMMENT ON COLUMN state.baseline_change_reasons.updated_at IS 'Дата обновления (триггер)';

DROP TRIGGER IF EXISTS trigger_baseline_change_reasons_updated_at ON state.baseline_change_reasons;
CREATE TRIGGER trigger_baseline_change_reasons_updated_at
    BEFORE UPDATE ON state.baseline_change_reasons
    FOR EACH ROW
    EXECUTE FUNCTION common.update_updated_at_column();

INSERT INTO state.baseline_change_reasons (id, reason_code, description) VALUES
    (gen_random_uuid(), 'session_end',          'Осаждение состояния по завершении сессии'),
    (gen_random_uuid(), 'hourly_drift',         'Ежечасный дрейф состояния во время активности'),
    (gen_random_uuid(), 'shutdown_maintenance', 'Изменение состояния после планового отключения'),
    (gen_random_uuid(), 'shutdown_crash',       'Изменение состояния после аварийного завершения'),
    (gen_random_uuid(), 'shutdown_forced',      'Изменение состояния после принудительного выключения'),
    (gen_random_uuid(), 'shutdown_absence',     'Изменение состояния после длительного отсутствия пользователя')
ON CONFLICT (reason_code) DO NOTHING;

-- =============================================
-- 6. Долговременный гормональный фон (baseline)
-- =============================================
CREATE TABLE IF NOT EXISTS state.baseline_pgs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    cortisol REAL CHECK (cortisol BETWEEN 0 AND 100),
    dopamine REAL CHECK (dopamine BETWEEN 0 AND 100),
    oxytocin REAL CHECK (oxytocin BETWEEN 0 AND 100),
    valence REAL CHECK (valence BETWEEN -100 AND 100),
    state_vector halfvec(128) NOT NULL,
    change_reason_id UUID NOT NULL REFERENCES state.baseline_change_reasons(id),
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    agent_version TEXT NOT NULL DEFAULT 'unknown'
);

COMMENT ON TABLE state.baseline_pgs IS 'Медленный гормональный фон (черты личности). Активна ровно одна запись.';
COMMENT ON COLUMN state.baseline_pgs.id IS 'UUID записи';
COMMENT ON COLUMN state.baseline_pgs.recorded_at IS 'Время фиксации baseline';
COMMENT ON COLUMN state.baseline_pgs.cortisol IS 'Кортизол (0..100) – стресс';
COMMENT ON COLUMN state.baseline_pgs.dopamine IS 'Дофамин (0..100) – мотивация';
COMMENT ON COLUMN state.baseline_pgs.oxytocin IS 'Окситоцин (0..100) – социальное доверие';
COMMENT ON COLUMN state.baseline_pgs.valence IS 'Валентность (-100..100) = tanh(0.05*(dopa+oxy-cort))*100';
COMMENT ON COLUMN state.baseline_pgs.state_vector IS 'Вектор RFF размерности 128';
COMMENT ON COLUMN state.baseline_pgs.change_reason_id IS 'Причина изменения (FK)';
COMMENT ON COLUMN state.baseline_pgs.is_active IS 'Активна ли запись (только одна true)';
COMMENT ON COLUMN state.baseline_pgs.created_at IS 'Дата создания записи';
COMMENT ON COLUMN state.baseline_pgs.updated_at IS 'Дата обновления (триггер)';
COMMENT ON COLUMN state.baseline_pgs.agent_version IS 'Версия агента (из pyproject.toml), зафиксировавшая данный baseline.';

CREATE UNIQUE INDEX baseline_active_unique ON state.baseline_pgs ((true)) WHERE is_active = true;
CREATE INDEX idx_baseline_recorded_at ON state.baseline_pgs (recorded_at DESC);

DROP TRIGGER IF EXISTS trigger_baseline_pgs_updated_at ON state.baseline_pgs;
CREATE TRIGGER trigger_baseline_pgs_updated_at
    BEFORE UPDATE ON state.baseline_pgs
    FOR EACH ROW
    EXECUTE FUNCTION common.update_updated_at_column();

-- =============================================
-- 7. События, вызывающие изменения momentary (справочник)
-- =============================================
CREATE TABLE IF NOT EXISTS state.delta_reasons (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type_code TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE state.delta_reasons IS 'Типы событий, вызывающих обновление momentary.';
COMMENT ON COLUMN state.delta_reasons.id IS 'UUID';
COMMENT ON COLUMN state.delta_reasons.event_type_code IS 'Код события (user_message, echo_match, ...)';
COMMENT ON COLUMN state.delta_reasons.description IS 'Описание события';
COMMENT ON COLUMN state.delta_reasons.created_at IS 'Дата создания';
COMMENT ON COLUMN state.delta_reasons.updated_at IS 'Дата обновления (триггер)';

DROP TRIGGER IF EXISTS trigger_delta_reasons_updated_at ON state.delta_reasons;
CREATE TRIGGER trigger_delta_reasons_updated_at
    BEFORE UPDATE ON state.delta_reasons
    FOR EACH ROW
    EXECUTE FUNCTION common.update_updated_at_column();

INSERT INTO state.delta_reasons (id, event_type_code, description) VALUES
    (gen_random_uuid(), 'user_message',      'Сообщение пользователя'),
    (gen_random_uuid(), 'agent_response',    'Ответ агента'),
    (gen_random_uuid(), 'echo_match',        'Совпадение смыслов между репликами пользователя и агента'),
    (gen_random_uuid(), 'prediction_match',  'Совпадение прогноза от ответа пользователя'),
    (gen_random_uuid(), 'self_reflection',   'Внутренняя рефлексия агента'),
    (gen_random_uuid(), 'decay_tick',        'Такт распада гормонов'),
    (gen_random_uuid(), 'dialog_start',      'Начало нового диалога'),
    (gen_random_uuid(), 'dialog_end',        'Завершение диалога'),
    (gen_random_uuid(), 'agent_start',       'Включение агента'),
    (gen_random_uuid(), 'agent_stop',        'Выключение агента'),
    (gen_random_uuid(), 'wake_up',           'Пробуждение после сна')
ON CONFLICT (event_type_code) DO NOTHING;

-- =============================================
-- 8. Быстрая внутридиалоговая динамика (momentary)
-- =============================================
CREATE TABLE IF NOT EXISTS state.momentary (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dialog_id UUID NOT NULL REFERENCES dialogs.dialogues(id) ON DELETE CASCADE,
    actor_id UUID NOT NULL REFERENCES users.actors(id) ON DELETE CASCADE,
    seq_num INT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    cortisol REAL CHECK (cortisol BETWEEN 0 AND 100),
    dopamine REAL CHECK (dopamine BETWEEN 0 AND 100),
    oxytocin REAL CHECK (oxytocin BETWEEN 0 AND 100),
    valence REAL CHECK (valence BETWEEN -100 AND 100),
    state_vector halfvec(128) NOT NULL,
    event_type_id UUID NOT NULL REFERENCES state.delta_reasons(id),
    orchestrator_step_id TEXT,
    event_payload JSONB,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    agent_version TEXT NOT NULL DEFAULT 'unknown'
);

COMMENT ON TABLE state.momentary IS 'Гормональная динамика внутри диалога. Активна последняя запись на каждого actor_id.';
COMMENT ON COLUMN state.momentary.id IS 'UUID записи';
COMMENT ON COLUMN state.momentary.dialog_id IS 'Ссылка на диалог (dialogs.dialogues)';
COMMENT ON COLUMN state.momentary.actor_id IS 'ID пользователя (владельца диалога)';
COMMENT ON COLUMN state.momentary.seq_num IS 'Порядковый номер события в диалоге';
COMMENT ON COLUMN state.momentary.recorded_at IS 'Время среза состояния';
COMMENT ON COLUMN state.momentary.cortisol IS 'Кортизол (0..100)';
COMMENT ON COLUMN state.momentary.dopamine IS 'Дофамин (0..100)';
COMMENT ON COLUMN state.momentary.oxytocin IS 'Окситоцин (0..100)';
COMMENT ON COLUMN state.momentary.valence IS 'Валентность (-100..100)';
COMMENT ON COLUMN state.momentary.state_vector IS 'RFF-вектор размерности 128';
COMMENT ON COLUMN state.momentary.event_type_id IS 'Тип события (FK)';
COMMENT ON COLUMN state.momentary.orchestrator_step_id IS 'ID шага оркестратора для отладки';
COMMENT ON COLUMN state.momentary.event_payload IS 'Доп. данные события (JSON)';
COMMENT ON COLUMN state.momentary.is_active IS 'Активна ли запись (только последняя на actor_id)';
COMMENT ON COLUMN state.momentary.created_at IS 'Дата создания записи';
COMMENT ON COLUMN state.momentary.updated_at IS 'Дата обновления (триггер)';
COMMENT ON COLUMN state.momentary.agent_version IS 'Версия агента при записи данного momentary-состояния.';

CREATE UNIQUE INDEX momentary_active_per_actor ON state.momentary (actor_id) WHERE is_active = true;
CREATE INDEX idx_momentary_dialog_seq ON state.momentary (dialog_id, seq_num);
CREATE INDEX idx_momentary_recorded_at ON state.momentary (recorded_at DESC);
CREATE INDEX idx_momentary_state_vector ON state.momentary USING hnsw (state_vector halfvec_cosine_ops);

DROP TRIGGER IF EXISTS trigger_momentary_updated_at ON state.momentary;
CREATE TRIGGER trigger_momentary_updated_at
    BEFORE UPDATE ON state.momentary
    FOR EACH ROW
    EXECUTE FUNCTION common.update_updated_at_column();

-- =============================================
-- 9. Причины выключений и простоев (фактические события с таймингами)
-- =============================================
CREATE TABLE IF NOT EXISTS state.shutdown_reasons (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_id UUID NOT NULL REFERENCES users.actors(id) ON DELETE CASCADE,
    shutdown_type state.shutdown_type NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE state.shutdown_reasons IS 'Факты выключений/простоев. Заполняется при выключении или при старте после креша.';
COMMENT ON COLUMN state.shutdown_reasons.id IS 'UUID';
COMMENT ON COLUMN state.shutdown_reasons.actor_id IS 'Пользователь, чьё отсутствие или действие вызвало отключение';
COMMENT ON COLUMN state.shutdown_reasons.shutdown_type IS 'Тип выключения (ENUM)';
COMMENT ON COLUMN state.shutdown_reasons.timestamp IS 'Метка времени создания записи метрики';

CREATE INDEX idx_shutdown_timestamp ON state.shutdown_reasons (timestamp);
CREATE INDEX idx_shutdown_actor ON state.shutdown_reasons (actor_id);


-- =============================================
-- 10. Глобальный жизненный цикл агента
-- =============================================
CREATE TABLE IF NOT EXISTS state.agent_lifecycle (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_id UUID NOT NULL REFERENCES users.actors(id) ON DELETE CASCADE,
    state_type state.agent_state_type NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at TIMESTAMPTZ,
    reason_change state.lifecycle_change_reason NOT NULL,
    shutdown_reason_id UUID NULL REFERENCES state.shutdown_reasons(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    agent_version TEXT NOT NULL DEFAULT 'unknown'
);

COMMENT ON TABLE state.agent_lifecycle IS 'История состояний off/sleep/active.';
COMMENT ON COLUMN state.agent_lifecycle.id IS 'UUID';
COMMENT ON COLUMN state.shutdown_reasons.actor_id IS 'Пользователь, чьё отсутствие или действие вызвало отключение';
COMMENT ON COLUMN state.agent_lifecycle.state_type IS 'Состояние: off, sleep, active';
COMMENT ON COLUMN state.agent_lifecycle.started_at IS 'Время начала состояния';
COMMENT ON COLUMN state.agent_lifecycle.ended_at IS 'Время окончания состояния (NULL – текущее)';
COMMENT ON COLUMN state.agent_lifecycle.reason_change IS 'Причина перехода (ENUM)';
COMMENT ON COLUMN state.agent_lifecycle.shutdown_reason_id IS 'Ссылка на shutdown_reasons (только для off)';
COMMENT ON COLUMN state.agent_lifecycle.created_at IS 'Дата создания записи';
COMMENT ON COLUMN state.agent_lifecycle.updated_at IS 'Дата обновления (триггер)';
COMMENT ON COLUMN state.agent_lifecycle.agent_version IS 'Версия агента, под которой было начато данное состояние жизненного цикла.';

CREATE UNIQUE INDEX lifecycle_active_per_actor ON state.agent_lifecycle (actor_id) WHERE ended_at IS NULL;
CREATE INDEX idx_lifecycle_started ON state.agent_lifecycle (started_at DESC);

DROP TRIGGER IF EXISTS trigger_agent_lifecycle_updated_at ON state.agent_lifecycle;
CREATE TRIGGER trigger_agent_lifecycle_updated_at
    BEFORE UPDATE ON state.agent_lifecycle
    FOR EACH ROW
    EXECUTE FUNCTION common.update_updated_at_column();

-- =============================================
-- 11. Справочник текстовых описаний состояния (self_knowledge)
-- =============================================
CREATE TABLE IF NOT EXISTS state.self_knowledge (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entry_type TEXT NOT NULL,
    content TEXT NOT NULL,
    confidence REAL DEFAULT 1.0 CHECK (confidence BETWEEN 0 AND 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ,
    is_active BOOLEAN NOT NULL DEFAULT true,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE state.self_knowledge IS 'Текстовые проекции гормональных состояний. Выбираются кодом на основе комплексного профиля.';
COMMENT ON COLUMN state.self_knowledge.id IS 'UUID';
COMMENT ON COLUMN state.self_knowledge.entry_type IS 'Тип описания (например anxiety, warmth)';
COMMENT ON COLUMN state.self_knowledge.content IS 'Текст для вставки в промпт';
COMMENT ON COLUMN state.self_knowledge.confidence IS 'Уверенность (0..1)';
COMMENT ON COLUMN state.self_knowledge.created_at IS 'Дата создания';
COMMENT ON COLUMN state.self_knowledge.expires_at IS 'Дата устаревания (NULL – вечно)';
COMMENT ON COLUMN state.self_knowledge.is_active IS 'Актуальна ли запись';
COMMENT ON COLUMN state.self_knowledge.updated_at IS 'Дата обновления (триггер)';

DROP TRIGGER IF EXISTS trigger_self_knowledge_updated_at ON state.self_knowledge;
CREATE TRIGGER trigger_self_knowledge_updated_at
    BEFORE UPDATE ON state.self_knowledge
    FOR EACH ROW
    EXECUTE FUNCTION common.update_updated_at_column();

-- =============================================
-- 12. Добавление колонок ПГС в существующую таблицу dialogs.sessions
-- =============================================
ALTER TABLE dialogs.sessions 
    ADD COLUMN IF NOT EXISTS baseline_id UUID REFERENCES state.baseline_pgs(id),
    ADD COLUMN IF NOT EXISTS sleep_duration INTERVAL;

COMMENT ON COLUMN dialogs.sessions.baseline_id IS 'Baseline, с которого стартовала сессия (внешний ключ к state.baseline_pgs)';
COMMENT ON COLUMN dialogs.sessions.sleep_duration IS 'Длительность сна/простоя перед началом сессии';