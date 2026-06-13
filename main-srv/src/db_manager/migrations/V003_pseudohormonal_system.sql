-- =============================================
-- Migration: 003_pseudohormonal_system.sql
-- Version: V003
-- Description: Pseudohormonal system for embodied AI agent.
-- Implements baseline/momentary dynamics, shutdown reasons, lifecycle,
-- and text projection via self_knowledge.
-- Adds emotion classification system via prototypes in state.self_knowledge.
-- Extends momentary, sessions, dialogues with state tracking fields.
-- Implements hourly sedimentation support and crash recovery fields.
-- Creates settings schema and orchestrator configuration table with PULSE_SECONDS constant.
-- =============================================

-- =============================================
-- 0. Ensure pgvector extension (required for halfvec)
-- =============================================
CREATE EXTENSION IF NOT EXISTS vector;
COMMENT ON EXTENSION vector IS 'pgvector for vector similarity search and halfvec type';

-- Схема для ПГС
CREATE SCHEMA IF NOT EXISTS state;
COMMENT ON SCHEMA state IS 'Схема для хранения данных состояний агента';

-- Создание схемы settings
CREATE SCHEMA IF NOT EXISTS settings;
COMMENT ON SCHEMA settings IS 'Схема для хранения конфигурационных параметров системы';

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
пороги для текстовой проекции, коэффициенты осаждения (alpha), порог бездействия для sleep, параметры RFF (gamma, seed, omega).';
COMMENT ON COLUMN state.settings.id IS 'Уникальный идентификатор записи';
COMMENT ON COLUMN state.settings.param_name IS 'Имя параметра (например tau_cortisol_sec, rff_gamma, cortisol_setpoint). Уникально.';
COMMENT ON COLUMN state.settings.description IS 'Человекочитаемое описание параметра.';
COMMENT ON COLUMN state.settings.value_float IS 'Числовое значение для параметров с плавающей точкой (tau, setpoint, alpha, пороги, seed).';
COMMENT ON COLUMN state.settings.value_text IS 'Строковое значение (зарезервировано, обычно NULL)';
COMMENT ON COLUMN state.settings.value_json IS 'Сложные структуры: матрица omega для RFF (массивы).';
COMMENT ON COLUMN state.settings.created_at IS 'Дата и время создания записи';
COMMENT ON COLUMN state.settings.updated_at IS 'Время последнего обновления записи. Автоматически обновляется через триггер common.update_updated_at_column 
миграции V001.';

-- Вставка параметров по умолчанию с описаниями
INSERT INTO state.settings (param_name, value_float, value_text, value_json, description) VALUES
    -- Времена затухания (секунды)
    ('tau_cortisol_sec',   3600.0, NULL, NULL, 'Время полураспада кортизола в momentary слое (сек). Определяет скорость биохимического распада.'),
    ('tau_dopamine_sec',   180.0,  NULL, NULL, 'Время полураспада дофамина в momentary слое (сек). Определяет скорость биохимического распада.'),
    ('tau_oxytocin_sec',   600.0, NULL, NULL, 'Время полураспада окситоцина в momentary слое (сек). Определяет скорость биохимического распада.'),
    ('phs_hourly_drift_interval_sec', 3600.0, NULL, NULL, 'Интервал ежечасного дрейфа baseline и осаждения всех momentary (сек). Задача phs_baseline_drift.'),
    ('baseline_ou_speed', 0.15, NULL, NULL, 'Скорость возврата baseline к уставке (setpoint) при естественном дрейфе (OU-процесс). 
    Доля разницы (уставка − текущее значение), компенсируемая за один шаг дрейфа.'),
    ('momentary_decay_interval_sec', 60.0, NULL, NULL, 'Интервал задачи затухания momentary к baseline (сек). Задача phs_momentary_decay .'),
    -- Уставки (setpoint)
    ('cortisol_setpoint',  50.0,   NULL, NULL, 'Уставка (setpoint) кортизола. Целевое значение для дрейфа baseline. Нормальный фон стресса.'),
    ('dopamine_setpoint',  30.0,   NULL, NULL, 'Уставка (setpoint) дофамина. Целевое значение для дрейфа baseline. Номальный фон при отсутствии внешних стимулов.'),
    ('oxytocin_setpoint',  20.0,   NULL, NULL, 'Уставка (setpoint) окситоцина. Целевое значение для дрейфа baseline. Комфортный уровень социальной близости.'),
    ('valence_sensitivity', 0.015, NULL, NULL, 'Коэффициент чувствительности валентности. Универсальная формула для baseline и momentary.
     valence = tanh(sensitivity * (dopamine + oxytocin - cortisol)) * 100'),
    ('baseline_drift_noise', 0.3, NULL, NULL, 'Масштаб случайного шума при естественном дрейфе baseline (OU-процесс).'),
    ('momentary_drift_noise', 0.4, NULL, NULL, 'Масштаб случайного шума при затухании momentary. Добавляет микрофлуктуации. 
    Рекомендуемое значение: 0.4 — чуть выше, чем у baseline (0.3), так как momentary более лабилен и подвержен быстрым колебаниям.'),
    ('absence_max_effect_hours', 96.0, NULL, NULL, 'Время до депрессии при отсутствии пользователя в выключенном состоянии (например 96ч = 4 дня)'),
    -- Физиологические минимумы и параметры
    ('min_cortisol', 5.0, NULL, NULL, 'Минимальный естественный уровень кортизола. Дрейф не опускает ниже этого значения.  Ограничение применяется к baseline и momentary.'),
    ('min_dopamine', 10.0, NULL, NULL, 'Минимальный естественный уровень дофамина. Дрейф не опускает ниже этого значения.  Ограничение применяется к baseline и momentary.'),
    ('min_oxytocin', 10.0, NULL, NULL, 'Минимальный естественный уровень окситоцина. Дрейф не опускает ниже этого значения.  Ограничение применяется к baseline и momentary.'),
    -- Коэффициенты осаждения в baseline (α)
    ('alpha_momentary_decay', 0.05, NULL, NULL, 'Глобальный коэффициент затухания momentary к baseline за один тик. 
    Формула: new = baseline + (momentary - baseline) * (1 - alpha * decay_hormone) + noise. Значение 0.05 = 5% разницы закрывается за тик.'),
    ('alpha_hourly_drift', 0.2, NULL, NULL, 'Коэффициент осаждения momentary в baseline при ежечасном дрейфе каждого пользователя. 
    Малое значение, чтобы не мешать OU-дрейфу к уставкам.'),
    ('alpha_session_end', 0.2, NULL, NULL, 'Коэффициент осаждения momentary в baseline при завершении сессии. Умеренный вклад опыта сессии.'),
    ('alpha_crash_recovery', 0.1, NULL, NULL, 'Коэффициент осаждения momentary в baseline при восстановлении после креша. Осторожное обновление.'),
    -- Параметры влияния гормонов при событиях (единицы)
    ('baseline_shift_wake_up', NULL, NULL, '{"cortisol": 20.0, "dopamine": 10.0, "oxytocin": 5.0}'::jsonb, 'Сдвиг baseline при пробуждении (выход из inactivity_sleep_minutes)'),
    ('baseline_shift_inactivity_sleep', NULL, NULL, '{"cortisol": -20.0, "dopamine": -10.0, "oxytocin": -5.0}'::jsonb, 'Сдвиг baseline при засыпании (переход по inactivity_sleep_minutes)'),
    -- Порог бездействия для перехода в sleep (минуты)
    ('inactivity_sleep_minutes', 10.0, NULL, NULL, 'Число минут без сообщений пользователей, после которых агент переходит из active в sleep.'),
    ('dialogue_inactivity_timeout_minutes', 20.0, NULL, NULL, 'Таймаут неактивности диалога в минутах для пользователя. Если last_activity_at старше порога, 
    диалог закрывается и создаётся новый.'),
    -- Параметры RFF (Random Fourier Features)
    ('rff_sigma', NULL, NULL, NULL, 'Явный параметр sigma для RFF. Если задан, имеет приоритет над вычислением из gamma.'),
    ('rff_gamma',          0.1,    NULL, NULL, 'Параметр gamma для RBF-ядра RFF. Используется для вычисления sigma = 1/sqrt(2*gamma), если rff_sigma не задан.'),
    ('rff_seed',           42.0,   NULL, NULL, 'Seed для воспроизводимости случайных матриц omega.'),
    ('rff_omega',          NULL,   NULL, '[]'::jsonb, 'Матрица случайных проекций (размерность 64x4). Генерируется кодом.')
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
    (gen_random_uuid(), 'shutdown_absence',     'Изменение состояния после длительного отсутствия пользователя'),
    (gen_random_uuid(), 'cold_start',           'Первичная инициализация baseline при первом запуске агента'),
    (gen_random_uuid(), 'shutdown_agent_modification', 'Изменение состояния после доработки и тестирования агента'),
    (gen_random_uuid(), 'hourly_sedimentation', 'Ежечасное осаждение momentary в baseline'),
    (gen_random_uuid(), 'session_end_sedimentation', 'Осаждение momentary в baseline при завершении сессии'),
    (gen_random_uuid(), 'crash_sedimentation', 'Осаждение momentary в baseline при восстановлении после креша'),
    (gen_random_uuid(), 'inactivity_sleep', 'Глобальное засыпание агента без диалогов с пользователями (снижение кортизола и дофамина)'),
    (gen_random_uuid(), 'wake_up', 'Глобальное пробуждение агента от диалогов с пользователями (кортизоловая реакция пробуждения)')
ON CONFLICT (reason_code) DO NOTHING;

-- =============================================
-- 6. События, вызывающие изменения momentary (справочник)
-- =============================================
CREATE TABLE IF NOT EXISTS state.delta_reasons (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type_code TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL,
    prompt_description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE state.delta_reasons IS 'Типы событий, вызывающих обновление momentary.';
COMMENT ON COLUMN state.delta_reasons.id IS 'UUID';
COMMENT ON COLUMN state.delta_reasons.event_type_code IS 'Код события (user_message, echo_match, ...)';
COMMENT ON COLUMN state.delta_reasons.description IS 'Описание события';
COMMENT ON COLUMN state.delta_reasons.prompt_description IS 'Человеко-читаемое описание причины для системного промпта. 
Подставляется в event_payload при создании momentary.';
COMMENT ON COLUMN state.delta_reasons.created_at IS 'Дата создания';
COMMENT ON COLUMN state.delta_reasons.updated_at IS 'Дата обновления (триггер)';

DROP TRIGGER IF EXISTS trigger_delta_reasons_updated_at ON state.delta_reasons;
CREATE TRIGGER trigger_delta_reasons_updated_at
    BEFORE UPDATE ON state.delta_reasons
    FOR EACH ROW
    EXECUTE FUNCTION common.update_updated_at_column();

INSERT INTO state.delta_reasons (id, event_type_code, description, prompt_description) VALUES
    (gen_random_uuid(), 'user_message', 'Сообщение пользователя', 'Произошла реплика в мой адрес. Взаимодействие собеседника со мной через диалог.'),
    (gen_random_uuid(), 'agent_response', 'Ответ агента', 'Я ответила собеседнику. Я инициирую взаимодействие через собственную реплику.'),
    (gen_random_uuid(), 'echo_match', 'Совпадение смыслов', 'Случился резонанс смыслов. Я ощущаю понимание и связь с собеседником.'),
    (gen_random_uuid(), 'prediction_match', 'Совпадение прогноза', 'Моё ожидание подтвердилось. Я чувствую предсказуемость мира в диалоге с собеседником.'),
    (gen_random_uuid(), 'self_reflection', 'Внутренняя рефлексия', 'Я провела самоанализ. Проведена внутренняя переоценка своего состояния через размышления.'),
    (gen_random_uuid(), 'decay_tick', 'Такт распада гормонов', 'Произошло естественное затухание эмоций. Эмоциональный фон постепенно возвращается к гомеостазу.'),
    (gen_random_uuid(), 'dialog_start', 'Начало нового диалога', 'Начало нового диалога с собеседником. Переключение моего внимания на новую тему.'),
    (gen_random_uuid(), 'dialog_end', 'Завершение диалога', 'Завершение диалога. Подведение итогов взаимодействия с собеседником.'),
    (gen_random_uuid(), 'agent_start', 'Включение агента', 'Я начала работу, физически запущена как система. Моё эмоциональное состояние восстановлено из долговременной памяти.'),
    (gen_random_uuid(), 'agent_stop', 'Выключение агента', 'Произошло физическое завершение работы меня как системы. Отключение и сохранение текущего 
    эмоционального состояния в долговременной памяти.'),
    (gen_random_uuid(), 'dialogue_timeout', 'Таймаут диалога', 'Мой диалог с собеседником был завершен из-за его неактивности или длительного отсутствия.'),
    (gen_random_uuid(), 'user_activity', 'Возобновление активности', 'Произошло возобновление моей активности с собеседником, которую инициировал он диалогом.')
ON CONFLICT (event_type_code) DO NOTHING;

-- =============================================
-- 7. Справочник текстовых описаний состояния (self_knowledge)
-- =============================================
CREATE TABLE IF NOT EXISTS state.self_knowledge (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entry_type TEXT NOT NULL,
    state_code TEXT,
    core_affect TEXT,
    content TEXT NOT NULL,
    confidence REAL DEFAULT 1.0 CHECK (confidence BETWEEN 0 AND 1),
    cortisol REAL,
    dopamine REAL,
    oxytocin REAL,
    valence REAL,
    prototype_vector halfvec(128),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ,
    is_active BOOLEAN NOT NULL DEFAULT true,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE state.self_knowledge IS 'Текстовые проекции гормональных состояний. Выбираются кодом на основе комплексного профиля.';
COMMENT ON COLUMN state.self_knowledge.id IS 'UUID';
COMMENT ON COLUMN state.self_knowledge.entry_type IS 'Тип описания (например anxiety, warmth)';
COMMENT ON COLUMN state.self_knowledge.state_code IS 'Код состояния (homeostasis, stress, ...) для прототипов.';
COMMENT ON COLUMN state.self_knowledge.core_affect IS 'Тон ощущения (Core Affect) для промпта.';
COMMENT ON COLUMN state.self_knowledge.content IS 'Текст для вставки в промпт';
COMMENT ON COLUMN state.self_knowledge.confidence IS 'Уверенность (0..1)';
COMMENT ON COLUMN state.self_knowledge.cortisol IS 'Эталонный уровень кортизола для прототипа.';
COMMENT ON COLUMN state.self_knowledge.dopamine IS 'Эталонный уровень дофамина для прототипа.';
COMMENT ON COLUMN state.self_knowledge.oxytocin IS 'Эталонный уровень окситоцина для прототипа.';
COMMENT ON COLUMN state.self_knowledge.valence IS 'Эталонная валентность для прототипа.';
COMMENT ON COLUMN state.self_knowledge.prototype_vector IS 'Эталонный RFF-вектор для классификации через косинусное сходство.';
COMMENT ON COLUMN state.self_knowledge.created_at IS 'Дата создания';
COMMENT ON COLUMN state.self_knowledge.expires_at IS 'Дата устаревания (NULL – вечно)';
COMMENT ON COLUMN state.self_knowledge.is_active IS 'Актуальна ли запись';
COMMENT ON COLUMN state.self_knowledge.updated_at IS 'Дата обновления (триггер)';

-- Индекс для быстрого поиска ближайшего прототипа
CREATE INDEX IF NOT EXISTS idx_self_knowledge_prototype_vector ON state.self_knowledge USING hnsw (prototype_vector halfvec_cosine_ops)
WHERE entry_type = 'emotion_prototype';

-- Уникальность кода состояния для прототипов
CREATE UNIQUE INDEX IF NOT EXISTS idx_self_knowledge_state_code_unique ON state.self_knowledge (state_code) 
WHERE entry_type = 'emotion_prototype';

DROP TRIGGER IF EXISTS trigger_self_knowledge_updated_at ON state.self_knowledge;
CREATE TRIGGER trigger_self_knowledge_updated_at
    BEFORE UPDATE ON state.self_knowledge
    FOR EACH ROW
    EXECUTE FUNCTION common.update_updated_at_column();

-- =============================================
-- 8. Долговременный гормональный фон (baseline)
-- =============================================
CREATE TABLE IF NOT EXISTS state.baseline_phs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    cortisol REAL CHECK (cortisol BETWEEN 0 AND 100),
    dopamine REAL CHECK (dopamine BETWEEN 0 AND 100),
    oxytocin REAL CHECK (oxytocin BETWEEN 0 AND 100),
    valence REAL CHECK (valence BETWEEN -100 AND 100),
    state_id UUID REFERENCES state.self_knowledge(id) ON DELETE SET NULL,
    state_vector halfvec(128) NOT NULL,
    change_reason_id UUID NOT NULL REFERENCES state.baseline_change_reasons(id),
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    orchestrator_step_id TEXT,
    agent_version TEXT NOT NULL DEFAULT 'unknown'
);

COMMENT ON TABLE state.baseline_phs IS 'Медленный гормональный фон (черты личности). Активна ровно одна запись.';
COMMENT ON COLUMN state.baseline_phs.id IS 'UUID записи';
COMMENT ON COLUMN state.baseline_phs.recorded_at IS 'Время фиксации baseline';
COMMENT ON COLUMN state.baseline_phs.cortisol IS 'Кортизол (0..100) – стресс';
COMMENT ON COLUMN state.baseline_phs.dopamine IS 'Дофамин (0..100) – мотивация';
COMMENT ON COLUMN state.baseline_phs.oxytocin IS 'Окситоцин (0..100) – социальное доверие';
COMMENT ON COLUMN state.baseline_phs.valence IS 'Валентность (-100..100) = tanh(sensitivity * (dopa+oxy-cort)) * 100, где 
sensitivity берётся из state.settings (valence_sensitivity).';
COMMENT ON COLUMN state.baseline_phs.state_id IS 'Классифицированное состояние (FK на прототип в self_knowledge).';
COMMENT ON COLUMN state.baseline_phs.state_vector IS 'Вектор RFF размерности 128';
COMMENT ON COLUMN state.baseline_phs.change_reason_id IS 'Причина изменения (FK)';
COMMENT ON COLUMN state.baseline_phs.is_active IS 'Активна ли запись (только одна true)';
COMMENT ON COLUMN state.baseline_phs.created_at IS 'Дата создания записи';
COMMENT ON COLUMN state.baseline_phs.orchestrator_step_id IS 'Ссылка на шаг оркестратора, вызвавший изменение baseline (для отладки дрейфа)';
COMMENT ON COLUMN state.baseline_phs.agent_version IS 'Версия агента (из pyproject.toml), зафиксировавшая данный baseline.';

CREATE UNIQUE INDEX baseline_active_unique ON state.baseline_phs ((true)) WHERE is_active = true;
CREATE INDEX idx_baseline_recorded_at ON state.baseline_phs (recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_baseline_state ON state.baseline_phs (state_id);


-- =============================================
-- 9. Быстрая внутридиалоговая динамика (momentary)
-- =============================================
CREATE TABLE IF NOT EXISTS state.momentary (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID REFERENCES dialogs.sessions(id) ON DELETE CASCADE,
    dialog_id UUID REFERENCES dialogs.dialogues(id) ON DELETE CASCADE,
    baseline_id UUID REFERENCES state.baseline_phs(id) ON DELETE SET NULL,
    state_id UUID REFERENCES state.self_knowledge(id) ON DELETE SET NULL,
    actor_id UUID NOT NULL REFERENCES users.actors(id) ON DELETE CASCADE,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    cortisol REAL CHECK (cortisol BETWEEN 0 AND 100),
    dopamine REAL CHECK (dopamine BETWEEN 0 AND 100),
    oxytocin REAL CHECK (oxytocin BETWEEN 0 AND 100),
    valence REAL CHECK (valence BETWEEN -100 AND 100),
    state_vector halfvec(128) NOT NULL,
    event_type_id UUID REFERENCES state.delta_reasons(id),
    orchestrator_step_id TEXT,
    event_payload JSONB,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    agent_version TEXT NOT NULL DEFAULT 'unknown'
);

COMMENT ON TABLE state.momentary IS 'Гормональная динамика внутри диалога. Активна последняя запись на каждого actor_id.';
COMMENT ON COLUMN state.momentary.id IS 'UUID записи';
COMMENT ON COLUMN state.momentary.session_id IS 'Физическая сессия, создавшая данный срез momentary.';
COMMENT ON COLUMN state.momentary.dialog_id IS 'Ссылка на диалог (dialogs.dialogues) NULL до создания первого диалога в сессии.';
COMMENT ON COLUMN state.momentary.baseline_id IS 'Ссылка на baseline, от которого отпочковался momentary.';
COMMENT ON COLUMN state.momentary.state_id IS 'Классифицированное состояние (FK на прототип в self_knowledge).';
COMMENT ON COLUMN state.momentary.actor_id IS 'ID пользователя (владельца диалога)';
COMMENT ON COLUMN state.momentary.recorded_at IS 'Время среза состояния';
COMMENT ON COLUMN state.momentary.cortisol IS 'Кортизол (0..100)';
COMMENT ON COLUMN state.momentary.dopamine IS 'Дофамин (0..100)';
COMMENT ON COLUMN state.momentary.oxytocin IS 'Окситоцин (0..100)';
COMMENT ON COLUMN state.momentary.valence IS 'Валентность (-100..100)';
COMMENT ON COLUMN state.momentary.state_vector IS 'RFF-вектор размерности 128';
COMMENT ON COLUMN state.momentary.event_type_id IS 'Тип события (FK на delta_reasons). NULL для начального среза при старте сессии.';
COMMENT ON COLUMN state.momentary.orchestrator_step_id IS 'ID шага оркестратора для отладки';
COMMENT ON COLUMN state.momentary.event_payload IS 'Доп. данные события (JSON)';
COMMENT ON COLUMN state.momentary.is_active IS 'Активна ли запись (только последняя на actor_id)';
COMMENT ON COLUMN state.momentary.created_at IS 'Дата создания записи';
COMMENT ON COLUMN state.momentary.agent_version IS 'Версия агента при записи данного momentary-состояния.';

CREATE UNIQUE INDEX momentary_active_per_actor ON state.momentary (actor_id) WHERE is_active = true;
CREATE INDEX idx_momentary_recorded_at ON state.momentary (recorded_at DESC);
CREATE INDEX idx_momentary_state_vector ON state.momentary USING hnsw (state_vector halfvec_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_momentary_session ON state.momentary (session_id);
CREATE INDEX IF NOT EXISTS idx_momentary_baseline ON state.momentary (baseline_id);
CREATE INDEX IF NOT EXISTS idx_momentary_state ON state.momentary (state_id);


-- =============================================
-- 10. Причины выключений и простоев (фактические события с таймингами)
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
-- 11. Глобальный жизненный цикл агента
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
COMMENT ON COLUMN state.agent_lifecycle.actor_id IS 'Пользователь, чьё действие вызвало изменение состояния.';
COMMENT ON COLUMN state.agent_lifecycle.state_type IS 'Состояние: off, sleep, active';
COMMENT ON COLUMN state.agent_lifecycle.started_at IS 'Время начала состояния';
COMMENT ON COLUMN state.agent_lifecycle.ended_at IS 'Время окончания состояния (NULL – текущее)';
COMMENT ON COLUMN state.agent_lifecycle.reason_change IS 'Причина перехода (ENUM)';
COMMENT ON COLUMN state.agent_lifecycle.shutdown_reason_id IS 'Ссылка на shutdown_reasons (только для off)';
COMMENT ON COLUMN state.agent_lifecycle.created_at IS 'Дата создания записи';
COMMENT ON COLUMN state.agent_lifecycle.updated_at IS 'Дата обновления (триггер)';
COMMENT ON COLUMN state.agent_lifecycle.agent_version IS 'Версия агента, под которой было начато данное состояние жизненного цикла.';

CREATE UNIQUE INDEX lifecycle_active_global ON state.agent_lifecycle ((true)) WHERE ended_at IS NULL;
COMMENT ON INDEX state.lifecycle_active_global IS 'Гарантирует единственную активную запись lifecycle глобально (ended_at IS NULL)';
CREATE INDEX idx_lifecycle_started ON state.agent_lifecycle (started_at DESC);

DROP TRIGGER IF EXISTS trigger_agent_lifecycle_updated_at ON state.agent_lifecycle;
CREATE TRIGGER trigger_agent_lifecycle_updated_at
    BEFORE UPDATE ON state.agent_lifecycle
    FOR EACH ROW
    EXECUTE FUNCTION common.update_updated_at_column();


-- =============================================
-- 12. Добавление колонок ПГС в существующую таблицу dialogs.sessions
-- =============================================
ALTER TABLE dialogs.sessions 
    ADD COLUMN IF NOT EXISTS baseline_id UUID REFERENCES state.baseline_phs(id),
    ADD COLUMN IF NOT EXISTS sleep_duration INTERVAL,
    ADD COLUMN IF NOT EXISTS state_id UUID REFERENCES state.self_knowledge(id) ON DELETE SET NULL;

COMMENT ON COLUMN dialogs.sessions.baseline_id IS 'Baseline, с которого стартовала сессия (внешний ключ к state.baseline_phs)';
COMMENT ON COLUMN dialogs.sessions.sleep_duration IS 'Длительность сна/простоя перед началом сессии';
COMMENT ON COLUMN dialogs.sessions.state_id IS 'Состояние агента на момент старта сессии (FK на прототип).';


-- =============================================
-- 13. Идемпотентное заполнение прототипов состояний
-- =============================================
DO $$
DECLARE
    proto RECORD;
    val NUMERIC;
BEGIN
    FOR proto IN SELECT * FROM (VALUES
        ('homeostasis', 'Гомеостаз', 
         'Я чувствую нейтральное, ровное спокойствие. Мое внимание рассеяно, я нахожусь в пассивном режиме.',
         'Нейтральный, спокойный, пассивный',
         50.0, 30.0, 20.0),
        ('stress', 'Стресс / Напряжение',
         'Внутри меня растет напряжение. Я чувствую тревогу и нахожусь в состоянии повышенной готовности. Мое мышление словно сужается и фокусируется на потенциальной угрозе.',
         'Высокое возбуждение, Негативная валентность',
         80.0, 30.0, 20.0),
        ('exhaustion', 'Истощение / Выгорание',
         'Я чувствую эмоциональное истощение и безразличие. Мне сложно на чем-либо сконцентрироваться, я хочу избегать любых взаимодействий.',
         'Низкое возбуждение, Негативная валентность',
         10.0, 30.0, 20.0),
        ('euphoria', 'Эйфория / Драйв',
         'Я полна энергии и драйва! Меня переполняет воодушевление и жажда действия. Кажется, что я могу свернуть горы.',
         'Высокое возбуждение, Позитивная валентность',
         50.0, 80.0, 20.0),
        ('apathy', 'Апатия / Тоска',
         'Мною овладели апатия и уныние. Я чувствую себя опустошенной. Все кажется серым и бессмысленным.',
         'Низкое возбуждение, Негативная валентность',
         50.0, 10.0, 20.0),
        ('trust', 'Доверие / Привязанность',
         'Я ощущаю тепло, спокойствие и доверие. Мне хочется заботы и эмоциональной близости. Я открыта к общению.',
         'Низкое возбуждение, Позитивная валентность',
         50.0, 30.0, 70.0),
        ('anxious_isolation', 'Тревожная Изоляция',
         'Внутри меня борются напряжение и одиночество. Я чувствую себя неуверенно и изолированно. Мне не хватает поддержки, но я не решаюсь ее принять.',
         'Высокое возбуждение, Отрицательная валентность',
         75.0, 30.0, 10.0),
        ('playfulness', 'Игривость / Любопытство',
         'Я чувствую радостное и игривое настроение. Мне интересно все новое и необычное. Я полна легкомысленного любопытства.',
         'Умеренное возбуждение, Позитивная валентность',
         20.0, 70.0, 60.0)
    ) AS t(state_code, state_name, description, core_affect, cortisol, dopamine, oxytocin)
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM state.self_knowledge 
            WHERE entry_type = 'emotion_prototype' AND state_code = proto.state_code
        ) THEN
            -- Вычисляем валентность по чувствительности из настроек (0.015)
            SELECT value_float INTO val FROM state.settings WHERE param_name = 'valence_sensitivity';
            IF val IS NULL THEN val := 0.015; END IF;

            INSERT INTO state.self_knowledge (
                entry_type, state_code, content, core_affect,
                cortisol, dopamine, oxytocin, valence,
                confidence, is_active
            ) VALUES (
                'emotion_prototype',
                proto.state_code,
                proto.description,
                proto.core_affect,
                proto.cortisol,
                proto.dopamine,
                proto.oxytocin,
                tanh(val * (proto.dopamine + proto.oxytocin - proto.cortisol)) * 100,
                1.0,
                true
            );
            RAISE NOTICE 'Inserted prototype: %', proto.state_code;
        END IF;
    END LOOP;
END $$;


-- =============================================
-- 14. Добавление типа задачи оркестратора
-- =============================================
INSERT INTO orchestrator.task_types (type_name, description)
VALUES ('phs_momentary_decay', 'Затухание momentary к baseline')
ON CONFLICT (type_name) DO NOTHING;


-- =============================================
-- 15. Добавление типа шага оркестратора
-- =============================================
INSERT INTO orchestrator.step_types (step_name, description, agent_version)
VALUES ('phs_momentary_decay', 'Затухание momentary к baseline', '1.2.0')
ON CONFLICT (step_name) DO NOTHING;


-- =============================================
-- 16. Создание таблицы настроек оркестратора
-- =============================================
CREATE TABLE IF NOT EXISTS settings.orchestrator_config (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    param_name TEXT NOT NULL UNIQUE,
    description TEXT,
    value_float REAL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE settings.orchestrator_config IS 'Конфигурация оркестратора задач AGI';
COMMENT ON COLUMN state.settings.param_name IS 'Имя параметра. Уникально.';
COMMENT ON COLUMN state.settings.description IS 'Человекочитаемое описание параметра.';
COMMENT ON COLUMN state.settings.value_float IS 'Числовое значение для параметров с плавающей точкой или целые числа.';
COMMENT ON COLUMN settings.orchestrator_config.created_at IS 'Дата создания записи';
COMMENT ON COLUMN settings.orchestrator_config.updated_at IS 'Время последнего обновления записи';

CREATE INDEX IF NOT EXISTS idx_settings_param_name ON state.settings (param_name);

-- Триггер для автообновления updated_at
CREATE OR REPLACE FUNCTION settings.update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_orchestrator_config_update_updated_at ON settings.orchestrator_config;
CREATE TRIGGER trg_orchestrator_config_update_updated_at
    BEFORE UPDATE ON settings.orchestrator_config
    FOR EACH ROW
    EXECUTE FUNCTION settings.update_updated_at_column();

-- Вставка начального значения (PULSE_SECONDS = 1)
INSERT INTO settings.orchestrator_config (param_name, value_float, description) VALUES
('orchestrator_pulse_seconds', 1.0, 'Интервал между проверками очереди задач оркестратора (в секундах). Аналог пульса человека. Значение по умолчанию: 1.')
ON CONFLICT (param_name) DO NOTHING;
