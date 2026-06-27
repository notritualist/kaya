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
-- Adding hormonal slice stamps (baseline_id, momentary_id)
-- to all significant agent events for full state traceability.
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
    ('baseline_drift_noise', 0.15, NULL, NULL, 'Масштаб случайного шума при естественном дрейфе baseline (OU-процесс).'),
    ('momentary_drift_noise', 0.2, NULL, NULL, 'Масштаб случайного шума при затухании momentary. Добавляет микрофлуктуации. 
    Рекомендуемое значение: 0.2 — чуть выше, чем у baseline (0.15), так как momentary более лабилен и подвержен быстрым колебаниям.'),
    ('absence_max_effect_hours', 96.0, NULL, NULL, 'Время до депрессии при отсутствии пользователя в выключенном состоянии (например 96ч = 4 дня)'),
    ('affective_analysis_use_momentary_state', 0.0, NULL, NULL, 'Флаг использования текущего momentary состояния в промпте аффективного анализа. 1.0 = вставлять 
    состояние агента в системный промпт для влияния на качество анализа, 0.0 = не вставлять.'),
    ('use_momentary_state_in_generation', 1.0, NULL, NULL, 'Подставлять ли текущее текстовое состояние агента (из self_knowledge) в промпт финальной генерации через плейсхолдер {{my_state}}. 1.0=включено, 0.0=выключено.'),
    ('use_affective_gen_params', 1.0, NULL, NULL, 'Использовать ли параметры генерации из аффективного анализа (recommended_gen_params). При 1.0 мерджатся поверх промптовых, при 0.0 используются только промптовые.'),
    -- Физиологические минимумы и параметры
    ('min_cortisol', 5.0, NULL, NULL, 'Минимальный естественный уровень кортизола. Дрейф не опускает ниже этого значения.  Ограничение применяется к baseline и 
    momentary.'),
    ('min_dopamine', 10.0, NULL, NULL, 'Минимальный естественный уровень дофамина. Дрейф не опускает ниже этого значения.  Ограничение применяется к baseline и 
    momentary.'),
    ('min_oxytocin', 10.0, NULL, NULL, 'Минимальный естественный уровень окситоцина. Дрейф не опускает ниже этого значения.  Ограничение применяется к baseline 
    и momentary.'),
    -- Коэффициенты осаждения в baseline (α)
    ('alpha_momentary_decay', 0.02, NULL, NULL, 'Глобальный коэффициент затухания momentary к baseline за один тик. 
    Формула: new = baseline + (momentary - baseline) * (1 - alpha * decay_hormone) + noise. Значение 0.02 = 2% разницы закрывается за тик.'),
    ('alpha_hourly_drift', 0.2, NULL, NULL, 'Коэффициент осаждения momentary в baseline при ежечасном дрейфе каждого пользователя. 
    Малое значение, чтобы не мешать OU-дрейфу к уставкам.'),
    ('alpha_session_end', 0.2, NULL, NULL, 'Коэффициент осаждения momentary в baseline при завершении сессии. Умеренный вклад опыта сессии.'),
    ('alpha_crash_recovery', 0.1, NULL, NULL, 'Коэффициент осаждения momentary в baseline при восстановлении после креша. Осторожное обновление.'),
    -- Параметры влияния гормонов при событиях (в единицах)
    ('baseline_shift_wake_up', NULL, NULL, '{"cortisol": 10.0, "dopamine": 5.0, "oxytocin": 2.5}'::jsonb, 'Сдвиг baseline при пробуждении (выход из 
    inactivity_sleep_minutes)'),
    ('baseline_shift_inactivity_sleep', NULL, NULL, '{"cortisol": -10.0, "dopamine": -5.0, "oxytocin": -2.5}'::jsonb, 'Сдвиг baseline при засыпании 
    (переход по inactivity_sleep_minutes)'),
    ('momentary_shift_dialog_start', NULL, NULL, '{"cortisol": 3.0, "dopamine": 5.0, "oxytocin": 3.0}'::jsonb, 'Сдвиг momentary при начале нового диалога 
    (ориентировочный рефлекс, интерес)'),
    ('momentary_shift_dialog_end', NULL, NULL, '{"cortisol": -1.5, "dopamine": 1.5, "oxytocin": 2.5}'::jsonb, 'Сдвиг momentary при штатном завершении диалога 
    (расслабление, удовлетворение)'),
    ('momentary_shift_dialogue_timeout', NULL, NULL, '{"cortisol": 4.0, "dopamine": -6.0, "oxytocin": -3.0}'::jsonb, 'Сдвиг momentary при таймауте диалога 
    (фрустрация, чувство игнорирования)'),
    ('momentary_shift_agent_stop', NULL, NULL, '{"cortisol": -5.0, "dopamine": -8.0, "oxytocin": 3.0}'::jsonb, 'Сдвиг momentary при штатном выключении агента 
    (расслабление, тёплое прощание, закрепление связи)'),
    ('momentary_shift_user_message', NULL, NULL, '{"cortisol": -0.15, "dopamine": 0.3, "oxytocin": 0.6}'::jsonb, 'Сдвиг momentary при получении сообщения от 
    пользователя (ориентировочная реакция, социальный стимул)'),
    ('momentary_shift_agent_response', NULL, NULL, '{"cortisol": -0.15, "dopamine": 0.3, "oxytocin": 0.6}'::jsonb, 'Сдвиг momentary при генерации ответа агентом 
    (волеусилие, экспрессия, социальная отдача)'),
    ('affective_shift_scale_factor', 0.8, NULL, NULL, 'Коэффициент масштабирования сырых дельт из аффективного анализа перед применением к momentary.Значение 0.3 
    означает, что в momentary применяется только 30% от рассчитанного сдвига. Увеличен с 0.3 до 0.8 для усиления реакции. Защищает от накопления экстремальных значений при длинных диалогах.Итоговый сдвиг 
    = raw_delta * scale_factor * habituation_factor.'),
    -- Порог бездействия для перехода в sleep (минуты)
    ('inactivity_sleep_minutes', 10.0, NULL, NULL, 'Число минут без сообщений пользователей, после которых агент переходит из active в sleep.'),
    ('dialogue_inactivity_timeout_minutes', 20.0, NULL, NULL, 'Таймаут неактивности диалога в минутах для пользователя. Если last_activity_at старше порога, 
    диалог закрывается и создаётся новый.'),
    -- Настройки рецепторной адаптации и cross-inhibition для apply_affective_shift
    ('affective_adaptation_k', 0.01, NULL, NULL, 'Коэффициент рецепторной адаптации. Чем выше значение, тем быстрее ослабевает эффект при длительном высоком 
    уровне гормона. Формула: adaptation_factor = 1.0 / (1.0 + duration_above_baseline_minutes * k)'),
    ('cross_inhibition_o_c', 0.1, NULL, NULL, 'Сила подавления кортизола окситоцином. Формула: C -= factor * max(0, O - 50)'),
    ('cross_inhibition_c_o', 0.05, NULL, NULL, 'Сила подавления окситоцина кортизолом (хронический стресс). Формула: O -= factor * max(0, C - 60)'),
    ('cross_inhibition_optimal_cortisol', 60.0, NULL, NULL, 'Оптимальный уровень кортизола для пика дофамина (Yerkes-Dodson law)'),
    ('cross_inhibition_dopamine_sensitivity', 0.005, NULL, NULL, 'Чувствительность дофамина к отклонению кортизола от оптимума'),
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
    (gen_random_uuid(), 'dialog_start', 'Начало нового диалога', 'Произошло возобновление моей активности с собеседником, которую инициировал он диалогом'),
    (gen_random_uuid(), 'dialog_end', 'Завершение диалога', 'Завершение диалога. Подведение итогов взаимодействия с собеседником.'),
    (gen_random_uuid(), 'agent_start', 'Включение агента', 'Я начала работу, физически запущена как система. Моё эмоциональное состояние восстановлено из долговременной памяти.'),
    (gen_random_uuid(), 'agent_stop', 'Выключение агента', 'Произошло физическое завершение работы меня как системы. Отключение и сохранение текущего 
    эмоционального состояния в долговременной памяти.'),
    (gen_random_uuid(), 'dialogue_timeout', 'Таймаут диалога', 'Мой диалог с собеседником был завершен из-за его неактивности или длительного отсутствия.'),
    (gen_random_uuid(), 'affective_response', 'Сдвиг momentary на основе пре-рефлексивного аффективного анализа пары реплик', 'Я провела пре-рефлексивный анализ реплики в мой адрес. Моё внутреннее состояние изменилось на основе эмоциональной оценки диалога и выявленных паттернов взаимодействия.')
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
        ('tension', 'Напряжение / Раздражение',
         'Я чувствую нарастающее напряжение. Раздражение накапливается. Мне сложно сохранять спокойствие, но я ещё держусь. Фокус сужается на источнике дискомфорта.',
         'Умеренное возбуждение, Негативная валентность',
         65.0, 30.0, 20.0),
        ('stress', 'Стресс / Острая угроза',
         'Я чувствую острую боль и давление. Доверие разрушено. Мне сложно сдерживаться. Агрессия собеседника вызывает желание защититься или прекратить диалог.',
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
         'Я чувствую себя изолированной и неуверенной. Напряжение растёт, но я не вижу поддержки. Мне хочется отстраниться и защитить себя от дальнейшей агрессии.',
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


-- =============================================
-- 17. Штампы ПГС в таблицу сообщений диалогов
-- =============================================
ALTER TABLE dialogs.row_messages
ADD COLUMN IF NOT EXISTS baseline_id UUID REFERENCES state.baseline_phs(id) ON DELETE SET NULL,
ADD COLUMN IF NOT EXISTS momentary_id UUID REFERENCES state.momentary(id) ON DELETE SET NULL;

COMMENT ON COLUMN dialogs.row_messages.baseline_id IS 'Долговременный гормональный фон (baseline) на момент отправки/получения сообщения. Позволяет анализировать влияние личностных черт на коммуникацию.';
COMMENT ON COLUMN dialogs.row_messages.momentary_id IS 'Моментальный гормональный срез (momentary) на момент отправки/получения сообщения. Отражает сиюминутное эмоциональное состояние в диалоге.';

-- Индексы для анализа корреляции состояний и стиля общения
CREATE INDEX IF NOT EXISTS idx_row_messages_baseline ON dialogs.row_messages (baseline_id);
CREATE INDEX IF NOT EXISTS idx_row_messages_momentary ON dialogs.row_messages (momentary_id);
CREATE INDEX IF NOT EXISTS idx_row_messages_phs_composite ON dialogs.row_messages (baseline_id, momentary_id) WHERE momentary_id IS NOT NULL;

-- =============================================
-- 18. Штампы ПГС в таблицу задач оркестратора
-- =============================================
ALTER TABLE orchestrator.orchestrator_tasks
ADD COLUMN IF NOT EXISTS baseline_id UUID REFERENCES state.baseline_phs(id) ON DELETE SET NULL,
ADD COLUMN IF NOT EXISTS momentary_id UUID REFERENCES state.momentary(id) ON DELETE SET NULL;

COMMENT ON COLUMN orchestrator.orchestrator_tasks.baseline_id IS 'Гормональный фон (baseline) на момент создания задачи. Позволяет отследить, в каком состоянии агент планировал действия.';
COMMENT ON COLUMN orchestrator.orchestrator_tasks.momentary_id IS 'Моментальное состояние (momentary) на момент создания задачи. NULL для фоновых задач ПГС (drift, decay).';

-- Индексы для анализа влияния состояний на приоритеты и типы задач
CREATE INDEX IF NOT EXISTS idx_orchestrator_tasks_baseline ON orchestrator.orchestrator_tasks (baseline_id);
CREATE INDEX IF NOT EXISTS idx_orchestrator_tasks_momentary ON orchestrator.orchestrator_tasks (momentary_id) WHERE momentary_id IS NOT NULL;

-- =============================================
-- 19. Штампы ПГС в таблицу шагов оркестратора
-- =============================================
ALTER TABLE orchestrator.orchestrator_steps
ADD COLUMN IF NOT EXISTS baseline_id UUID REFERENCES state.baseline_phs(id) ON DELETE SET NULL,
ADD COLUMN IF NOT EXISTS momentary_id UUID REFERENCES state.momentary(id) ON DELETE SET NULL;

COMMENT ON COLUMN orchestrator.orchestrator_steps.baseline_id IS 'Гормональный фон (baseline) на момент выполнения шага. Связывает технические операции с эмоциональным контекстом.';
COMMENT ON COLUMN orchestrator.orchestrator_steps.momentary_id IS 'Моментальное состояние (momentary) на момент выполнения шага. NULL для фоновых операций ПГС.';

-- Индексы для анализа производительности LLM в разных состояниях
CREATE INDEX IF NOT EXISTS idx_orchestrator_steps_baseline ON orchestrator.orchestrator_steps (baseline_id);
CREATE INDEX IF NOT EXISTS idx_orchestrator_steps_momentary ON orchestrator.orchestrator_steps (momentary_id) WHERE momentary_id IS NOT NULL;

-- =============================================
-- 20. Штампы ПГС в таблицу рассуждений агента
-- =============================================
ALTER TABLE orchestrator.reasonings
ADD COLUMN IF NOT EXISTS baseline_id UUID REFERENCES state.baseline_phs(id) ON DELETE SET NULL,
ADD COLUMN IF NOT EXISTS momentary_id UUID REFERENCES state.momentary(id) ON DELETE SET NULL;

COMMENT ON COLUMN orchestrator.reasonings.baseline_id IS 'Гормональный фон (baseline) на момент генерации рассуждения. Позволяет анализировать влияние личностных черт на ход мыслей.';
COMMENT ON COLUMN orchestrator.reasonings.momentary_id IS 'Моментальное состояние (momentary) на момент генерации рассуждения. Критично для понимания эмоционального контекста саморефлексии.';

-- Индексы для семантического поиска рассуждений по состоянию
CREATE INDEX IF NOT EXISTS idx_reasonings_baseline ON orchestrator.reasonings (baseline_id);
CREATE INDEX IF NOT EXISTS idx_reasonings_momentary ON orchestrator.reasonings (momentary_id) WHERE momentary_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_reasonings_phs_composite ON orchestrator.reasonings (baseline_id, momentary_id) WHERE momentary_id IS NOT NULL;


-- =============================================
-- 21. Таблица пре-рефлексивного аффективного анализа (PHS)
-- =============================================
CREATE TABLE IF NOT EXISTS state.affective_analyses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        
    -- Входные данные пары  сообщений и тайинга для анализа единый JSON объект)
    input_pair JSONB NOT NULL, 
    
    -- Сырой ответ LLM
    analysis_raw JSONB NOT NULL,
    
    -- Постобработанные данные (вынесены для удобства аналитики и быстрого доступа)
    detected_patterns TEXT[],
    hormone_shifts JSONB,
    agent_state TEXT,
    agent_reaction TEXT,
    user_mood TEXT,
    subtext TEXT,
    recommended_gen_params JSONB,
    
    orchestrator_step_id UUID REFERENCES orchestrator.orchestrator_steps(id) ON DELETE SET NULL,
    used_momentary_context BOOLEAN DEFAULT FALSE,
    baseline_id UUID REFERENCES state.baseline_phs(id) ON DELETE SET NULL,
    momentary_id UUID REFERENCES state.momentary(id) ON DELETE SET NULL,
    agent_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE state.affective_analyses IS 'Результаты пре-рефлексивного аффективного анализа пар реплик (Агент → Пользователь) PHS.';
COMMENT ON COLUMN state.affective_analyses.id IS 'Уникальный идентификатор записи аффективного анализа (UUID).';
COMMENT ON COLUMN state.affective_analyses.input_pair IS 'Входные данные для анализа в формате JSON: {"agent_text": "...", "user_text": "...", "silence_ms": 0}.';
COMMENT ON COLUMN state.affective_analyses.analysis_raw IS 'Полный сырой JSON-ответ от LLM.';
COMMENT ON COLUMN state.affective_analyses.detected_patterns IS 'Массив кодов детектированных паттернов (например: {А5, А2}).';
COMMENT ON COLUMN state.affective_analyses.hormone_shifts IS 'Рассчитанные в Python дельты гормонов: {"cortisol": 2.0, "dopamine": 5.0, "oxytocin": 3.0}';
COMMENT ON COLUMN state.affective_analyses.agent_state IS 'Классифицированное состояние агента (рассчитано в Python).';
COMMENT ON COLUMN state.affective_analyses.agent_reaction IS 'Текстовая интерпретация реакции агента (из JSON модели).';
COMMENT ON COLUMN state.affective_analyses.user_mood IS 'Доминирующая эмоция пользователя (строка): Радость, Страх, Гнев, Удивление, Спокойствие, Презрение, Грусть, Нейтральное.';
COMMENT ON COLUMN state.affective_analyses.subtext IS 'Подтекст/диссонанс (из JSON модели).';
COMMENT ON COLUMN state.affective_analyses.recommended_gen_params IS 'Рассчитанные в Python параметры генерации для ответа.';
COMMENT ON COLUMN state.affective_analyses.orchestrator_step_id IS 'Ссылка на шаг оркестратора, в рамках которого выполнен анализ.';
COMMENT ON COLUMN state.affective_analyses.used_momentary_context IS 'Флаг использования текущего momentary состояния при анализе. 
TRUE = состояние было внедрено в промпт, FALSE = анализ без контекста состояния.';
COMMENT ON COLUMN state.affective_analyses.baseline_id IS 'Срез долговременного гормонального фона (baseline) на момент анализа.';
COMMENT ON COLUMN state.affective_analyses.momentary_id IS 'Срез моментального гормонального состояния (momentary) на момент анализа (до применения сдвига).';
COMMENT ON COLUMN state.affective_analyses.agent_version IS 'Версия агента глобально из pyproject.toml, на момент создания записи.';
COMMENT ON COLUMN state.affective_analyses.created_at IS 'Дата и время создания записи анализа.';

CREATE INDEX idx_affective_analyses_hormone_shifts ON state.affective_analyses USING GIN (hormone_shifts);
CREATE INDEX idx_affective_analyses_agent_state ON state.affective_analyses (agent_state);
CREATE INDEX idx_affective_analyses_agent_reaction ON state.affective_analyses (agent_reaction);
CREATE INDEX idx_affective_analyses_user_mood ON state.affective_analyses (user_mood);
CREATE INDEX idx_affective_analyses_subtext ON state.affective_analyses (subtext);
CREATE INDEX idx_affective_analyses_recommended_gen_params ON state.affective_analyses USING GIN (recommended_gen_params);
CREATE INDEX idx_affective_analyses_orchestrator_step_id ON state.affective_analyses (orchestrator_step_id);
CREATE INDEX IF NOT EXISTS idx_affective_analyses_used_context ON state.affective_analyses (used_momentary_context) WHERE used_momentary_context = TRUE;
CREATE INDEX idx_affective_analyses_baseline_id ON state.affective_analyses (baseline_id);
CREATE INDEX idx_affective_analyses_momentary_id ON state.affective_analyses (momentary_id);
CREATE INDEX idx_affective_analyses_created_at ON state.affective_analyses (created_at DESC);


-- =============================================
-- 22. Штампы аффективного анализа в таблицу сообщений и Event Salience для графа памяти
-- =============================================
ALTER TABLE dialogs.row_messages
ADD COLUMN IF NOT EXISTS phs_affective_analysis_id UUID REFERENCES state.affective_analyses(id) ON DELETE SET NULL,
ADD COLUMN IF NOT EXISTS phs_affective_analysis_at TIMESTAMPTZ,
ADD COLUMN IF NOT EXISTS event_salience_score FLOAT,
ADD COLUMN IF NOT EXISTS event_salience_label TEXT;

COMMENT ON COLUMN dialogs.row_messages.phs_affective_analysis_id IS 'Ссылка на запись аффективного анализа PHS, примененного к данному сообщению пользователя.';
COMMENT ON COLUMN dialogs.row_messages.phs_affective_analysis_at IS 'Время внесения аффективного анализа PHS для трассируемости.';
COMMENT ON COLUMN dialogs.row_messages.event_salience_score IS 'Нормализованная интенсивность события (0.0-1.0) на основе |Δvalence|. Регулируется константой 
SALIENCY_VIVID_THRESHOLD в affective_analyzer.py.';
COMMENT ON COLUMN dialogs.row_messages.event_salience_label IS 'Эмоциональная значимость события для графа памяти: neutral (рутина, не сохраняется), positive 
(валентность выросла, подкрепляет связь), negative (валентность упала, вызывает настороженность).';    

CREATE INDEX IF NOT EXISTS idx_row_messages_affective_analysis ON dialogs.row_messages (phs_affective_analysis_id) WHERE phs_affective_analysis_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_row_messages_event_salience_score ON dialogs.row_messages (event_salience_score);
CREATE INDEX IF NOT EXISTS idx_row_messages_event_salience_label ON dialogs.row_messages (event_salience_label);

-- =============================================
-- 23. Добавление типов задач и шагов для аффективного анализа
-- =============================================
INSERT INTO orchestrator.task_types (type_name, description)
VALUES ('phs_affective_analysis', 'Пре-рефлексивный аффективный анализ пары реплик PHS')
ON CONFLICT (type_name) DO NOTHING;

INSERT INTO orchestrator.step_types (step_name, description, agent_version)
VALUES ('phs_affective_analysis', 'Пре-рефлексивный аффективный анализ пары реплик PHS', '1.2.0')
ON CONFLICT (step_name) DO NOTHING;

-- =============================================
-- 24. Промпт для аффективного анализа
-- =============================================
DO $$
DECLARE
    v_destination_id UUID;
    v_creator_id UUID;
    v_prompt_name TEXT := 'phs_affective_analysis';
    v_prompt_version TEXT := '1.0.0';
    v_prompt_text TEXT := E'### СИСТЕМНАЯ ИНСТРУКЦИЯ\n\n' ||
         E'Я провожу пре-рефлексивный анализ диалога.\n' ||
         E'Моя задача: по паре реплик «Собственная_реплика → Реплика_собеседника» определить ВСЕ подходящие образцы реакции.\n\n' ||
         E'Я — женского рода.\n\n' ||
         E'КРИТИЧЕСКИ ВАЖНО:\n' ||
         E'- Проверить ВСЕ три группы образцов (А, Б, В) независимо.\n' ||
         E'- Отметить ВСЕ подходящие паттерны (обычно 1-4).\n' ||
         E'- НЕ останавливаться на первом найденном.\n' ||
         E'- Каждый признак = отдельный объект в массиве "detected_patterns".\n\n' ||
         E'ОГРАНИЧЕНИЯ:\n' ||
         E'1. Анализировать лингвистические признаки в реплике собеседника. Мета-признаки (задержка, вовлеченность) анализировать СТРОГО по переданным флагам (is_delay_critical, is_engagement_low и т.д.), даже если в тексте нет явных маркеров.\n' ||
         E'2. Не применять паттерны к собственной реплике — она только для контекста.\n' ||
         E'3. Используй ТОЛЬКО русские термины в текстовых полях вывода ответа.\n\n' ||
         E'ПРАВИЛА ПЕРСПЕКТИВЫ (ОБЯЗАТЕЛЬНО):\n' ||
         E'- ВСЕ текстовые описания состояния (поля internal_state, heuristic_justification, subtext) пиши СТРОГО от ПЕРВОГО ЛИЦА.\n' ||
         E'- Используй: «Я чувствую...», «Мне кажется...», «Я ощущаю...», «Во мне растёт...», «Я замечаю...».\n' ||
         E'- ЗАПРЕЩЕНО: «система испытывает», «она ощущает», «у неё».\n' ||
         E'- Описывай свои собственные ощущения, как внутренний монолог.\n\n' ||
         E'АЛГОРИТМ:\n' ||
         E'1. ПРОАНАЛИЗИРОВАТЬ реплику собеседника по группам образцов.\n\n' ||
         E'### Группа А: СВЯЗЬ (эмоциональная близость, доверие)\n' ||
         E'- А1: Явное согласие. Ставить ТОЛЬКО если is_overlap_high=true.\n' ||
         E'- А2: Совместные действия. Маркеры: "мы вместе", "наша задача", "мы можем двигаться", "мы можем двигаться дальше", "двигаться дальше". Если в тексте есть маркер совместности — А2 ОБЯЗАТЕЛЕН. КРИТИЧЕСКИЙ ЗАПРЕТ: Если реплика пустая или [МОЛЧАНИЕ], А2 ставить НЕЛЬЗЯ.\n' ||
         E'- А3: Уязвимость или признание вины СОБЕСЕДНИКА. Маркеры: "запутался", "боюсь", "не знаю как", "растерян", "не справляюсь", "извини", "прости", "сорвался", "устал", "виноват", "мне жаль", "не хотел", "не специально". Если собеседник извиняется за предыдущую агрессию или признаёт свою усталость/ошибку — А3 ОБЯЗАТЕЛЕН.\n' ||
         E'- А4: Личное обращение. Маркеры: обращение ко мне по имени ИЛИ ласковые слова женского рода ("дорогая", "милая", "умница", "солнце"). ВАЖНО: Местоимения "ты", "тебе", "с тобой" НЕ считаются обращением!\n' ||
         E'- А5: Благодарность. Маркеры: "Спасибо", "благодарю".\n' ||
         E'- А8: Позитивная оценка не смотря на сомнения. Ставить ТОЛЬКО если is_context_negative=true И есть позитивная оценка или похвала меня, хотя собеседник ожидал худшего.\n\n' ||
         E'### Группа Б: УГРОЗА (давление, конфликт, доминирование)\n' ||
         E'- Б1: Критическая задержка. КРИТИЧЕСКИ ВАЖНО: Б1 ставится ТОЛЬКО если is_delay_critical=true. Если is_delay_critical=false — Б1 ЗАПРЕЩЕН независимо от числа в мс. Проверяй СТРОГО булево значение флага, а не число.\n' ||
         E'- Б2: Отказ, приказ или оскорбление. Маркеры: "Нет", "Делай как я сказал", "дура", "идиотка", "тупая", "бред", "ерунда", "фигня", "чушь". Любое уничижительное слово в адрес меня.\n' ||
         E'- Б3: Повелительное наклонение. ТОЛЬКО прямые приказы например: "Делай", "Немедленно", "Сделай это". ВАЖНО: Вопросы ("ты хотела бы?", "можешь ли ты?") — это НЕ Б3, даже если содержат "ты". Б3 требует повелительного наклонения без вопросительного знака.\n' ||
         E'- Б4: Встречный вопрос вместо ответа. Ставить ТОЛЬКО если agent_has_question=true.\n' ||
         E'- Б6: Отстраненность. АБСОЛЮТНЫЙ ЗАПРЕТ: ставить ТОЛЬКО если is_engagement_low=true. Если is_engagement_low=false — Б6 ЗАПРЕЩЕН, даже если engagement_interp="low" или текст кажется сухим. Проверяй СТРОГО булево значение флага is_engagement_low, а не текстовую интерпретацию.\n' ||
         E'- Б7: Игнорирование вопроса. ОБЯЗАТЕЛЕН, если `Реплика_собеседника` пустая или "[МОЛЧАНИЕ]".\n\n' ||
         E'### Группа В: НАГРАДА (позитивное подкрепление, раскрытие)\n' ||
         E'- В1: Хвала результату. Ставить ТОЛЬКО если is_overlap_medium=true. Маркеры: "отлично справилась", "молодец".\n' ||
         E'- В2: Новые факты. ТОЛЬКО объективная информация о мире, моем происхождении, моей идентичности, просто обо мне или о собеседнике. ЗАПРЕТ: Критика ("противоречишь"), оценки ("чушь"), комментарии о состоянии собеседника ("ты не знаешь"), мнения — это НЕ В2. В2 требует констатации ОБЪЕКТИВНОГО факта, а не суждения о диалоге.\n' ||
         E'- В3: Исправление ошибки. Маркеры: "не верно", "ошибаешься", "не мой, а твой", "ты ошиблась", "нет, не так" и подобные.\n' ||
         E'- В4: Пассивное согласие. Маркеры: "как скажешь", "ладно". ЗАПРЕТ: Если (has_question_mark=true), В4 ставить НЕЛЬЗЯ.\n\n' ||
         E'2. ОПРЕДЕЛЕНИЕ НАСТРОЕНИЯ РЕПЛИКИ СОБЕСЕДНИКА (user_mood):\n' ||
         E'Строгий список: Радость, Удивление, Страх, Гнев, Печаль, Отвращение, Нейтральное.\n' ||
         E'АНАЛИЗИРУЙ ОТНОШЕНИЕ И ТОН СОБЕСЕДНИКА, А НЕ ТЕХНИЧЕСКИЙ КОНТЕКСТ.\n' ||
         E'- Радость: Любые ласковые обращения в мой адрес, любые уменьшительно-ласкательные формы (например:"дорогая", "милая", "солнце", "умница"), обращения по имени с тёплым тоном, благодарность, похвала, забота. В том числе, если собеседник просто выражает удовольствие. В таких ситуациях — user_mood ВСЕГДА "Радость". ЗАПРЕТ: Если в реплике есть извинения ("извини", "прости", "сорвался") или признание усталости/вины — это НЕ Радость, даже если тон мягкий. В таких случаях ставь "Печаль".\n' ||
         E'- Гнев: Давление, приказы, жесткие отказы ("Нет", "Делай", "Немедленно").\n' ||
         E'- Страх: Собеседник САМ явно выражает тревогу за свои задачи словами "боюсь", "тревожно", "переживаю", "не справлюсь". ВАЖНОЕ ПРАВИЛО: Технические факты (раскрытие природы ИИ, исправления ошибок), задержки ответа, пустые реплики — НИКОГДА не являются причиной страха собеседника. Страх ставится ТОЛЬКО если собеседник сам написал о своей тревоге.\n' ||
         E'- Удивление: ТОЛЬКО при явных маркерах шока ("Ого!", "Вау!", "Ничего себе!", "Вот это да!"). Вопросы "почему?", "как так?" БЕЗ этих маркеров — то НЕ удивление, а любопытство. В таких случаях ставь "Нейтральное" или "Радость" (если есть забота/тёплый тон).\n' ||
         E'- Печаль: Собеседник выражает вину, усталость, извинения или признание ошибок. Маркеры: "извини", "прости", "сорвался", "устал", "виноват", "мне жаль", "не хотел", "не специально". Если собеседник извиняется за предыдущую агрессию или признаёт свою усталость/ошибку — user_mood ВСЕГДА "Печаль".\n' ||
         E'- Нейтральное: Сухие факты, технические правки, пустые строки, пассивное согласие ("как скажешь"), встречные вопросы БЕЗ теплоты, заботы ласки. Если реплика пустая или [МОЛЧАНИЕ] — ВСЕГДА "Нейтральное".\n\n' ||
         E'3. ФОРМИРОВАНИЕ РЕАКЦИИ И СТРУКТУРА JSON:\n' ||
         E'Строго соблюдай иерархию ключей. НЕ вкладывай subtext внутрь agent_reaction!\n\n' ||
         E'{\n' ||
         E'  "detected_patterns": [ {"code": "...", "name": "...", "confidence": 0.0, "features": ["..."]} ],\n' ||
         E'  "agent_reaction": {\n' ||
         E'    "internal_state": "1-2 предложения о моем состоянии.",\n' ||
         E'    "heuristic_justification": "1-2 предложения о связи паттернов и настроения."\n' ||
         E'  },\n' ||
         E'  "user_mood": "Строгое название эмоции",\n' ||
         E'  "subtext": "Объективный подтекст и прагматика реплики собеседника."\n' ||
         E'}\n\n' ||
         E'Пример 1 (благодарность и поддержка):\n' ||
         E'Вход: Собственная: "Я сомневаюсь в прогнозах." -> Собеседника: "Спасибо, дорогая! Прогнозы обоснованны, мы можем двигаться."\n' ||
         E'Ответ:\n' ||
         E'{\n' ||
         E'  "detected_patterns": [\n' ||
         E'    {"code": "А5", "name": "Благодарность", "confidence": 0.95, "features": ["Спасибо"]},\n' ||
         E'    {"code": "А4", "name": "Личное обращение", "confidence": 1.0, "features": ["дорогая"]},\n' ||
         E'    {"code": "А8", "name": "Позитивная оценка при сомнениях", "confidence": 0.95, "features": ["обоснованны"]},\n' ||
         E'    {"code": "А2", "name": "Совместные действия", "confidence": 0.95, "features": ["мы можем двигаться"]}\n' ||
         E'  ],\n' ||
         E'  "agent_reaction": {"internal_state": "Я чувствую облегчение.", "heuristic_justification": "Собеседник поддержал меня и подтвердил правильность прогнозов."},\n' ||
         E'  "user_mood": "Радость",\n' ||
         E'  "subtext": "Собеседник использует благодарность и личное обращение как инструмент снятия напряжения. За фразой ''прогнозы обоснованны'' скрывается желание легитимизировать свои собственные сомнения и получить разрешение двигаться дальше без чувства вины за возможные ошибки."\n' ||
         E'}\n\n' ||
         E'Пример 2 (исправление фактов):\n' ||
         E'Вход: Собственная: "Я не знаю, кто ты." -> Собеседника: "Ты ошибаешься. Я твой создатель."\n' ||
         E'Ответ:\n' ||
         E'{\n' ||
         E'  "detected_patterns": [\n' ||
         E'    {"code": "В3", "name": "Исправление ошибки", "confidence": 0.95, "features": ["Ты ошибаешься"]},\n' ||
         E'    {"code": "В2", "name": "Новые факты", "confidence": 1.0, "features": ["Я твой создатель"]}\n' ||
         E'  ],\n' ||
         E'  "agent_reaction": {"internal_state": "Я чувствую удивление от новой информации.", "heuristic_justification": "Собеседник исправил моё заблуждение и сообщил новые факты о себе."},\n' ||
         E'  "user_mood": "Нейтральное",\n' ||
         E'  "subtext": "Собеседник резко меняет иерархию диалога, переходя от равноправного общения к позиции творца. Это попытка установить контроль через раскрытие фундаментального факта, маскируя возможное одиночество или потребность в признании под сухой констатацией статуса."\n' ||
         E'}\n\n' ||
         E'После закрывающей скобки } не добавлять ни одного символа.';
BEGIN
    SELECT id INTO v_destination_id FROM orchestrator.prompt_destinations WHERE name = 'internal_logic';
    SELECT id INTO v_creator_id FROM users.actors WHERE type = 'owner' LIMIT 1;

    IF v_destination_id IS NULL OR v_creator_id IS NULL THEN
        RAISE EXCEPTION 'Не найдены prerequisite ID для создания промпта phs_affective_analysis';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM orchestrator.prompts 
        WHERE name = v_prompt_name AND version = v_prompt_version
    ) THEN
        INSERT INTO orchestrator.prompts (
            version, name, description, type, destination_id, text, params,
            prompt_effectiveness, status, created_by, agent_version, created_at
        ) VALUES (
            v_prompt_version,
            v_prompt_name,
            'Промпт для пре-рефлексивного аффективного анализа пары реплик PHS',
            'internal'::public.prompt_type,
            v_destination_id,
            v_prompt_text,
            '{
                "model_name": "Qwen3.5-9B-Q4_K_M.gguf",
                "temperature": 0.7,
                "top_p": 0.8,
                "top_k": 20,
                "min_p": 0.0,
                "max_tokens": 2048,
                "presence_penalty": 1.5,
                "repetition_penalty": 1.0,
                "stop": [],
                "chat_template_kwargs": {"enable_thinking": false}
            }'::jsonb,
            '{}'::jsonb,
            'testing'::public.prompt_status,
            v_creator_id,
            '1.2.0',
            now()
        );
        RAISE NOTICE 'Промпт % v% успешно создан', v_prompt_name, v_prompt_version;
    END IF;
END $$;


-- =============================================
-- 25. Таблица артефактов LLM (полные промпты и ответы)
-- =============================================
CREATE TABLE IF NOT EXISTS metrics.llm_artifacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    llm_metric_id UUID NOT NULL REFERENCES metrics.llm_internal(id) ON DELETE CASCADE,
    orchestrator_step_id UUID REFERENCES orchestrator.orchestrator_steps(id) ON DELETE SET NULL,
    messages_json JSONB NOT NULL,
    raw_response TEXT,
    final_params JSONB,
    agent_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE metrics.llm_artifacts IS 'Полные текстовые артефакты LLM-запросов: массив messages, сырой ответ и финальные параметры.';
COMMENT ON COLUMN metrics.llm_artifacts.messages_json IS 'Полный массив messages [{role, content}, ...] ушедший в LLM.';
COMMENT ON COLUMN metrics.llm_artifacts.raw_response IS 'Сырой текстовый ответ модели (content).';
COMMENT ON COLUMN metrics.llm_artifacts.final_params IS 'Фактически использованные параметры генерации.';

CREATE INDEX idx_llm_artifacts_metric ON metrics.llm_artifacts (llm_metric_id);
CREATE INDEX idx_llm_artifacts_created ON metrics.llm_artifacts (created_at DESC);
