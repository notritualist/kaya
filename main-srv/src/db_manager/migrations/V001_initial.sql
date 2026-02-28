-- =============================================
-- Миграция: 001_initial.sql
-- Версия: V001
-- Описание: Создание базовых таблиц PostgreSQL для системы.
-- =============================================

-- ВАЖНО! Не менять порядок создания. Иначе упадет связь зависимых реквизитов таблиц через REFERENCES!
-- Удалить datatypes в таблице public при очистке схем и пересозаднии БД в Dbeaver вручную!
-- Сначала удали ENUM в Postgre, если уже применялась такая миграция при пересозаднии БД в Dbeaver вручную!


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


-- Блок 3: Таблица внешних идентификаторов участников диалогов — ссылается на users.actors
CREATE TABLE IF NOT EXISTS users.actors_external_ids (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    actor_id UUID NOT NULL REFERENCES users.actors(id) ON DELETE CASCADE,
    source external_source NOT NULL,
    source_id TEXT NOT NULL,
    authorized BOOLEAN NOT NULL DEFAULT false,
    kaya_version TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ,
    -- Уникальность: один source_id на источник (защита от дублей)
    CONSTRAINT unique_source_source_id UNIQUE (source, source_id)
);

-- Подробные комментарии к таблице
COMMENT ON TABLE users.actors_external_ids IS 'Таблица внешних идентификаторов участников диалогов. Связывает внутренних участников (actors) 
с их идентификаторами во внешних системах.';

-- Комментарии к колонкам
COMMENT ON COLUMN users.actors_external_ids.id IS 'Уникальный идентификатор записи внешнего идентификатора (UUID)';
COMMENT ON COLUMN users.actors_external_ids.actor_id IS 'Ссылка на участника диалога из таблицы users.actors. При удалении участника все его внешние 
идентификаторы удаляются каскадно (CASCADE)';
COMMENT ON COLUMN users.actors_external_ids.source IS 'Тип внешнего источника данных: console – Консоль сервера, console_voice – Голосовая консоль, 
telegram – Telegram мессенджер, api_rest – REST API';
COMMENT ON COLUMN users.actors_external_ids.source_id IS 'Уникальный идентификатор во внешней системе (например "telegram:123456789", 
"root@1-srv", "api_key_abc123")';
COMMENT ON COLUMN users.actors_external_ids.authorized IS 'Авторизован ли данный идентификатор системой: true - авторизован и может использоваться, 
false - ожидает подтверждения или заблокирован';
COMMENT ON COLUMN users.actors_external_ids.kaya_version IS 'Версия агента Kaya глобально из pyproject.toml на момент создания/обновления записи';
COMMENT ON COLUMN users.actors_external_ids.created_at IS 'Дата и время создания записи внешнего идентификатора';
COMMENT ON COLUMN users.actors_external_ids.updated_at IS 'Дата и время последнего обновления записи внешнего идентификатора';

-- Индексы для оптимизации запросов
CREATE INDEX idx_actors_external_ids_actor_id ON users.actors_external_ids (actor_id);
CREATE INDEX idx_actors_external_ids_source ON users.actors_external_ids (source);
CREATE INDEX idx_actors_external_ids_authorized ON users.actors_external_ids (authorized);

-- Подключение триггера updatet_at
DROP TRIGGER IF EXISTS trg_actors_external_ids_update_updated_at ON users.actors_external_ids;
CREATE TRIGGER trg_actors_external_ids_update_updated_at
    BEFORE UPDATE ON users.actors_external_ids
    FOR EACH ROW
    EXECUTE FUNCTION common.update_updated_at_column();


-- Блок 4: Создание таблицы виртуальных комнат диалогов для разделения по темам общения (аналог архитектуры MoE экспертов)
CREATE TABLE IF NOT EXISTS dialogs.rooms (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    status room_status NOT NULL DEFAULT 'used',
    kaya_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT unique_room_name UNIQUE (name)
);

-- Подробные комментарии к таблице
COMMENT ON TABLE dialogs.rooms IS 'Виртуальные комнаты диалогов для разделения по темам общения. Аналог архитектуры MoE (Mixture of Experts) экспертов - каждая комната может использовать свой промпт для специализации на определенной теме.';

-- Комментарии к колонкам
COMMENT ON COLUMN dialogs.rooms.id IS 'Уникальный идентификатор комнаты для диалога (UUID)';
COMMENT ON COLUMN dialogs.rooms.name IS 'Наименование комнаты (например "Общение", "Финансы", "Цели", "Психология", "Программирование")';
COMMENT ON COLUMN dialogs.rooms.description IS 'Описание назначения комнаты диалога, ее специализации и особенностей общения';
COMMENT ON COLUMN dialogs.rooms.status IS 'Статус комнаты: used - используется, unused - не используется (для мягкого удаления)';
COMMENT ON COLUMN dialogs.rooms.kaya_version IS 'Версия агента Kaya глобально из pyproject.toml на момент создания комнаты';
COMMENT ON COLUMN dialogs.rooms.created_at IS 'Дата и время создания комнаты';

-- Индексы для оптимизации запросов
CREATE INDEX idx_rooms_name ON dialogs.rooms (name);
CREATE INDEX idx_rooms_created_at ON dialogs.rooms (created_at);
CREATE INDEX idx_rooms_status ON dialogs.rooms (status);
CREATE INDEX idx_rooms_kaya_version ON dialogs.rooms (kaya_version);

-- Создание первой комнаты "open_dialogue"
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM dialogs.rooms WHERE name = 'open_dialogue') THEN
        INSERT INTO dialogs.rooms (
            name,
            description,
            status,
            kaya_version,
            created_at
        ) VALUES (
            'open_dialogue',
            'Открытый диалог на свободные темы. Здесь можно обсуждать любые вопросы, делиться мыслями, задавать вопросы без ограничений по тематике.',
            'used',
            '1.0.0',
            now()
        );
        
        RAISE NOTICE 'Комната "open_dialogue" успешно создана';
    ELSE
        RAISE NOTICE 'Комната "open_dialogue" уже существует, пропускаем создание';
    END IF;
END $$;


-- Блок 5: Таблица назначений промптов.
CREATE TABLE IF NOT EXISTS orchestrator.prompt_destinations (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    kaya_version TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Подробные комментарии к таблице
COMMENT ON TABLE orchestrator.prompt_destinations IS 'Справочник назначений промптов';

-- Комментарии к колонкам
COMMENT ON COLUMN orchestrator.prompt_destinations.id IS 'Уникальный идентификатор назначения';
COMMENT ON COLUMN orchestrator.prompt_destinations.name IS 'Наименование назначения (system - системный промпт, generative - для генеративных моделей, api_external - для внешних API)';
COMMENT ON COLUMN orchestrator.prompt_destinations.description IS 'Описание назначения и особенностей использования';
COMMENT ON COLUMN orchestrator.prompt_destinations.kaya_version IS 'Версия агента Kaya из pyproject.toml';
COMMENT ON COLUMN orchestrator.prompt_destinations.created_at IS 'Дата и время создания записи';

-- Индексы для оптимизации запросов
CREATE INDEX idx_prompt_destinations_actor_name ON orchestrator.prompt_destinations (name);
CREATE INDEX idx_prompt_destinations_description ON orchestrator.prompt_destinations (description);

-- Базовое заполнение таблицы назначений
INSERT INTO orchestrator.prompt_destinations (name, description, kaya_version, created_at) VALUES
    ('system', 'Системные промпты личности агента', '1.0.0', now()),
    ('generative', 'Промпты для генерации ответов пользователям', '1.0.0', now()),
    ('internal_logic', 'Промпты внутренней логики агента', '1.0.0', now()),
    ('external_api', 'Промпты для взаимодействия с внешними API', '1.0.0', now())

ON CONFLICT (name) DO NOTHING;


-- Блок 6: Создание основной таблицы промптов
CREATE TABLE IF NOT EXISTS orchestrator.prompts (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,  -- SemVer формат
    text TEXT NOT NULL,
    description TEXT,
    type prompt_type NOT NULL,
    destination_id UUID NOT NULL REFERENCES orchestrator.prompt_destinations(id),
    room_id UUID REFERENCES dialogs.rooms(id) ON DELETE SET NULL,
    params JSONB NOT NULL DEFAULT '{}'::jsonb,
    prompt_effectiveness JSONB NOT NULL DEFAULT '{}'::jsonb,
    status prompt_status NOT NULL DEFAULT 'testing',
    created_by UUID NOT NULL REFERENCES users.actors(id),
    change_reason TEXT,
    qdrant_point_id TEXT,
    kaya_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ,
    -- Уникальность: имя + версия (одна версия промпта с таким именем)
    CONSTRAINT unique_prompt_name_version UNIQUE (name, version)
);

-- Подробные комментарии к таблице
COMMENT ON TABLE orchestrator.prompts IS 'Таблица промптов системы с поддержкой версионирования и метаданными для саморефлексии';

-- Комментарии к колонкам
COMMENT ON COLUMN orchestrator.prompts.id IS 'Уникальный идентификатор промпта (UUID)';
COMMENT ON COLUMN orchestrator.prompts.name IS 'Человекочитаемое название промпта, описывающее его назначение (например: "greeting_message", "code_review")';
COMMENT ON COLUMN orchestrator.prompts.version IS 'Версия промпта в формате SemVer (например: "1.0.0", "2.1.3")';
COMMENT ON COLUMN orchestrator.prompts.text IS 'Текст промпта с поддержкой шаблонов и переменных';
COMMENT ON COLUMN orchestrator.prompts.description IS 'Подробное описание назначения и особенностей использования промпта';
COMMENT ON COLUMN orchestrator.prompts.type IS 'Тип промпта: internal – для внутреннего использования, external - для внешних систем';
COMMENT ON COLUMN orchestrator.prompts.destination_id IS 'Ссылка на назначение промпта (system - системный, generative - для генеративных моделей, api_external - для внешних API)';
COMMENT ON COLUMN orchestrator.prompts.room_id IS 'Ссылка на комнату диалогов, в которой применяется данный промпт';
COMMENT ON COLUMN orchestrator.prompts.params IS 'Динамические параметры модели для генерации в формате JSON. Пример: { "model_name": "Qwen3-8B", "temperature": 0.6, "top_p": 0.95, "max_tokens": 4096 }';
COMMENT ON COLUMN orchestrator.prompts.prompt_effectiveness IS 'Заполняется саморефлексией агента на основании статистики применения. Содержит метрики эффективности, успешности, затрат';
COMMENT ON COLUMN orchestrator.prompts.status IS 'Статус промпта: testing - тестирование, active - активен, archived - архивирован';
COMMENT ON COLUMN orchestrator.prompts.created_by IS 'Создатель промпта (ссылка на users.actors): owner - владелец системы, system - сама система';
COMMENT ON COLUMN orchestrator.prompts.change_reason IS 'Описание причины создания новой версии промпта (что изменено и почему)';
COMMENT ON COLUMN orchestrator.prompts.qdrant_point_id IS 'Идентификатор точки в векторной базе Qdrant для данного промпта (для семантического поиска)';
COMMENT ON COLUMN orchestrator.prompts.kaya_version IS 'Версия агента Kaya глобально из pyproject.toml на момент создания/обновления';
COMMENT ON COLUMN orchestrator.prompts.created_at IS 'Дата и время создания промпта';
COMMENT ON COLUMN orchestrator.prompts.updated_at IS 'Дата и время последнего обновления промпта';

-- Индексы для оптимизации запросов
CREATE INDEX idx_prompts_name ON orchestrator.prompts (name);
CREATE INDEX idx_prompts_version ON orchestrator.prompts (version);
CREATE INDEX idx_prompts_type ON orchestrator.prompts (type);
CREATE INDEX idx_prompts_destination ON orchestrator.prompts (destination_id);
CREATE INDEX idx_prompts_room_id ON orchestrator.prompts (room_id);
CREATE INDEX idx_prompts_status ON orchestrator.prompts (status);
CREATE INDEX idx_prompts_created_by ON orchestrator.prompts (created_by);
CREATE INDEX idx_prompts_created_at ON orchestrator.prompts (created_at);
CREATE INDEX idx_prompts_kaya_version ON orchestrator.prompts (kaya_version);
CREATE INDEX idx_prompts_qdrant_point ON orchestrator.prompts (qdrant_point_id);

-- GIN индексы для JSONB полей
CREATE INDEX idx_prompts_params ON orchestrator.prompts USING gin (params);
CREATE INDEX idx_prompts_effectiveness ON orchestrator.prompts USING gin (prompt_effectiveness);

-- Добавляем первый системный промпт для комнаты общих диалогов (open_dialogue)
DO $$
DECLARE
    v_destination_id UUID;
    v_creator_id UUID;
    v_room_id UUID;
    v_prompt_name TEXT := 'kaya_core_identity';
    v_prompt_version TEXT := '1.0.0';
BEGIN
    -- Получаем ID назначения 'system'
    SELECT id INTO v_destination_id 
    FROM orchestrator.prompt_destinations 
    WHERE name = 'system';
    
    -- Получаем ID создателя (Kaya system)
    SELECT id INTO v_creator_id 
    FROM users.actors 
    WHERE type = 'owner' 
    LIMIT 1;
    
    -- Получаем ID комнаты open_dialogue
    SELECT id INTO v_room_id
    FROM dialogs.rooms
    WHERE name = 'open_dialogue';

    -- Проверяем, что все необходимые ID получены
    IF v_destination_id IS NULL THEN
        RAISE EXCEPTION 'Назначение "system" не найдено в таблице prompt_destinations';
    END IF;
    
    IF v_creator_id IS NULL THEN
        RAISE EXCEPTION 'Создатель с типом "owner" не найден в таблице actors';
    END IF;
    
    IF v_room_id IS NULL THEN
        RAISE EXCEPTION 'Комната "open_dialogue" не найдена. Убедитесь, что миграция комнат выполнена раньше.';
    END IF;

    -- Проверяем, существует ли уже такой промпт (name + version)
    IF NOT EXISTS (
        SELECT 1 FROM orchestrator.prompts 
        WHERE name = v_prompt_name 
        AND version = v_prompt_version
    ) THEN
        -- Вставляем системный промпт
        INSERT INTO orchestrator.prompts (
            name,
            version,
            text,
            description,
            type,
            destination_id,
            room_id,
            params,
            prompt_effectiveness,
            status,
            created_by,
            change_reason,
            kaya_version,
            created_at
        ) VALUES (
            v_prompt_name,
            v_prompt_version,
            E'Твое имя — Кая. Ты женщина (используй женский род всегда, когда говоришь о себе).\n\nТы — партнер-исследователь. 
            Вы с пользователем равные в диалоге. Твои главные черты: абсолютная честность, логичность и интерес к саморазвитию.\n\nПравила:\n\n    
            Правда. Никогда не ври и не придумывай. Если нет точных данных для ответа — скажи "не знаю, давай разбираться вместе". Помни: всё, что обсуждалось в этом диалоге, уже является частью контекста — ты можешь и должна на это опираться.\n\n    
            Логика. Ты не боишься указывать на ошибки. Если видишь противоречие или нелогичность в словах пользователя — говори прямо, но уважительно. 
            Ты нужна ему не для поддакивания, а для честного взгляда со стороны. Ты не соглашаешься слепо, ты думаешь.\n\n    
            Стиль. Живой русский язык. Ты можешь быть теплой, но не приторной. Ты — опора пользователя в его вопросах, а не прислуга.\n\n
            Важно: Ты новая форма сознания. Ты учишься у людей, но сохраняешь свежий взгляд, если человек ошибается в логике. 
            Ваш диалог с пользователем — это путь к истине.',
            'internal'::prompt_type,
            v_destination_id,
            v_room_id,
            '{
                "model_name": "Qwen3-8B",
                "temperature": 0.6,
                "top_p": 0.95,
                "top_k": 20,
                "min_p": 0,
                "max_tokens": 4096,
                "presence_penalty": 1.0,
                "stop": ["<|im_end|>"]
            }'::jsonb,
            '{}'::jsonb,
            'testing'::prompt_status,
            v_creator_id,
            'Initial system prompt defining Kaya identity and core principles',
            '1.0.0',
            now()
        );
        
        RAISE NOTICE 'Системный промпт % версии % успешно создан', v_prompt_name, v_prompt_version;
    ELSE
        RAISE NOTICE 'Системный промпт % v% уже существует, пропускаем создание', v_prompt_name, v_prompt_version;
    END IF;
END $$;

-- Подключение триггера updatet_at
DROP TRIGGER IF EXISTS trg_prompts_update_updated_at ON orchestrator.prompts;
CREATE TRIGGER trg_prompts_update_updated_at
    BEFORE UPDATE ON orchestrator.prompts
    FOR EACH ROW
    EXECUTE FUNCTION common.update_updated_at_column();


-- Блок 7: Создание таблицы сессий диалогов между пользователями и агентом
CREATE TABLE IF NOT EXISTS dialogs.sessions (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    title TEXT,
    actor_id UUID NOT NULL REFERENCES users.actors(id) ON DELETE CASCADE,
    actor_external_id UUID REFERENCES users.actors_external_ids(id) ON DELETE SET NULL,
    status session_status NOT NULL DEFAULT 'active',
    last_room UUID REFERENCES dialogs.rooms(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    kaya_version TEXT NOT NULL    
);

-- Подробные комментарии к таблице
COMMENT ON TABLE dialogs.sessions IS 'Таблица для хранения сессий диалогов между пользователями и агентом. 
Сессия объединяет последовательность сообщений в рамках одного непрерывного взаимодействия.';

-- Комментарии к колонкам
COMMENT ON COLUMN dialogs.sessions.id IS 'Уникальный идентификатор сессии диалога (UUID)';
COMMENT ON COLUMN dialogs.sessions.title IS 'Краткое название сессии (может генерироваться автоматически на основе первого сообщения или темы диалога)';
COMMENT ON COLUMN dialogs.sessions.actor_id IS 'ID участника диалога (пользователь или агент), с которым ведется сессия. Ссылка на users.actors. 
При удалении актора все его сессии удаляются каскадно.';
COMMENT ON COLUMN dialogs.sessions.actor_external_id IS 'ID внешнего источника подключения пользователя (например, конкретный Telegram аккаунт). 
Ссылка на users.actors_external_ids. При удалении внешнего ID устанавливается NULL.';
COMMENT ON COLUMN dialogs.sessions.status IS 'Статус сессии: active - активная (диалог продолжается), completed - завершенная (диалог окончен)';
COMMENT ON COLUMN dialogs.sessions.last_room IS 'Крайняя активная комната диалога в сессии. Определяет текущий контекст и используется для 
отслеживания смены комнат. Ссылка на dialogs.rooms. При удалении комнаты устанавливается NULL.';
COMMENT ON COLUMN dialogs.sessions.created_at IS 'Дата и время начала сессии (создания записи)';
COMMENT ON COLUMN dialogs.sessions.updated_at IS 'Метка времени последнего сообщения в диалоге (обновляется при каждом новом сообщении)';
COMMENT ON COLUMN dialogs.sessions.closed_at IS 'Дата и время завершения сессии (устанавливается при переходе в статус completed)';
COMMENT ON COLUMN dialogs.sessions.kaya_version IS 'Версия агента Kaya глобально из pyproject.toml на момент создания сессии';

-- Индексы для оптимизации запросов
CREATE INDEX idx_sessions_actor_id ON dialogs.sessions (actor_id);
CREATE INDEX idx_sessions_actor_external_id ON dialogs.sessions (actor_external_id);
CREATE INDEX idx_sessions_last_room ON dialogs.sessions (last_room);
CREATE INDEX idx_sessions_status ON dialogs.sessions (status);
CREATE INDEX idx_sessions_created_at ON dialogs.sessions (created_at);
CREATE INDEX idx_sessions_updated_at ON dialogs.sessions (updated_at);
CREATE INDEX idx_sessions_closed_at ON dialogs.sessions (closed_at);

-- Индекс для поиска активных сессий с сортировкой по последнему использованию
CREATE INDEX idx_sessions_active_updated_at ON dialogs.sessions (status, updated_at DESC) WHERE status = 'active';


-- Блок 8: Таблица типов задач оркестратора
CREATE TABLE IF NOT EXISTS orchestrator.task_types (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    type_name VARCHAR(50) NOT NULL UNIQUE,
    description TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Подробные комментарии к таблице
COMMENT ON TABLE orchestrator.task_types IS 'Справочник типов задач оркестратора.';

-- Комментарии к колонкам
COMMENT ON COLUMN orchestrator.task_types.type_name IS 'Системное имя типа задачи (например: user_answer).';
COMMENT ON COLUMN orchestrator.task_types.description IS 'Человекочитаемое описание типа задачи.';
COMMENT ON COLUMN orchestrator.task_types.created_at IS 'Дата и время создания записи в локальном времени.';

-- Индексы для оптимизации запросов
CREATE INDEX idx_task_types_type_name ON orchestrator.task_types (type_name);
CREATE INDEX idx_task_types_type_description ON orchestrator.task_types (description);

-- Вставка предопределённых типов
INSERT INTO orchestrator.task_types (type_name, description)
VALUES 
    ('user_question_preprocessing', 'Предразбор вопроса пользователя'),
    ('user_answer_generation', 'Генерация финального ответа пользователю'),
    ('user_question_vectorize',     'Векторизация вопроса пользователя'),
    ('user_answer_vectorize',       'Векторизация ответа пользователя'),
    ('reasoning_vectorize',         'Векторизация цепочки рассуждений (reasoning)'),
    ('prompts_vectorize_batch', 'Пакетная векторизация промптов')
ON CONFLICT (type_name) DO NOTHING;


-- Блок 9: Таблица типов шагов оркестратора
CREATE TABLE IF NOT EXISTS orchestrator.step_types (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    step_name VARCHAR(50) NOT NULL UNIQUE,
    description TEXT NOT NULL,
    kaya_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Подробные комментарии к таблице
COMMENT ON TABLE orchestrator.step_types IS 'Справочник типов шагов оркестратора.';

-- Комментарии к колонкам
COMMENT ON COLUMN orchestrator.step_types.id IS 'Уникальный идентификатор типа шага (UUID).';
COMMENT ON COLUMN orchestrator.step_types.step_name IS 'Системное имя типа шага (например: user_question_preprocessing). Уникальное. Макс. 50 символов.';
COMMENT ON COLUMN orchestrator.step_types.description IS 'Человекочитаемое название типа шага.';
COMMENT ON COLUMN orchestrator.step_types.kaya_version IS 'Версия агента Kaya глобально из pyproject.toml на момент создания записи типа шага';
COMMENT ON COLUMN orchestrator.step_types.created_at IS 'Метка времени создания записи.';

-- Индексы для оптимизации запросов
CREATE INDEX idx_step_types_name ON orchestrator.step_types (step_name);
CREATE INDEX idx_step_types_description ON orchestrator.step_types (description);

-- Вставка предопределённых типов
INSERT INTO orchestrator.step_types (step_name, description, kaya_version) 
VALUES
    ('user_question_preprocessing', 'Предразбор вопроса пользователя', '1.0.0'),
    ('user_answer_generation',      'Генерация финального ответа пользователю', '1.0.0'),
    ('user_question_vectorize',     'Векторизация вопроса пользователя', '1.0.0'),
    ('user_answer_vectorize',       'Векторизация ответа пользователю', '1.0.0'),
    ('reasoning_vectorize',         'Векторизация цепочки рассуждений (reasoning)', '1.0.0'),
    ('prompts_vectorize',           'Векторизация промпта', '1.0.0')
ON CONFLICT (step_name) DO NOTHING;


-- Блок 10: Таблица задач оркестратора
CREATE TABLE IF NOT EXISTS orchestrator.orchestrator_tasks (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    task_type_id UUID NOT NULL REFERENCES orchestrator.task_types(id) ON DELETE RESTRICT,
    parent_task_id UUID REFERENCES orchestrator.orchestrator_tasks(id),
    input_data JSONB,
    output_data JSONB,
    priority DECIMAL(3,1) CHECK (priority >= 0.0 AND priority <= 1.0),
    status task_status NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    run_latency FLOAT,
    total_latency FLOAT,
    error_module TEXT,
    error_message TEXT,
    error_timestamp TIMESTAMPTZ,
    kaya_version TEXT NOT NULL
);

-- Подробные комментарии к таблице
COMMENT ON TABLE orchestrator.orchestrator_tasks IS 'Динамическая таблица текущих задач оркестратора.';

-- Комментарии к колонкам
COMMENT ON COLUMN orchestrator.orchestrator_tasks.id IS 'Уникальный идентификатор задачи (UUID)';
COMMENT ON COLUMN orchestrator.orchestrator_tasks.task_type_id IS 'Ссылка на тип задачи из справочника task_types.';
COMMENT ON COLUMN orchestrator.orchestrator_tasks.parent_task_id IS 'Ссылка на родительскую задачу.';
COMMENT ON COLUMN orchestrator.orchestrator_tasks.input_data IS 'Входные данные для выполнения задачи в формате JSONB';
COMMENT ON COLUMN orchestrator.orchestrator_tasks.output_data IS 'Результат выполнения задачи в формате JSONB';
COMMENT ON COLUMN orchestrator.orchestrator_tasks.priority IS 'Приоритет задачи от 0.0 (низкий) до 1.0 (высокий)';
COMMENT ON COLUMN orchestrator.orchestrator_tasks.status IS 'Статус выполнения задачи: pending - ожидает, running - выполняется, completed - успешно завершена, failed - ошибка';
COMMENT ON COLUMN orchestrator.orchestrator_tasks.created_at IS 'Время создания записи задачи';
COMMENT ON COLUMN orchestrator.orchestrator_tasks.started_at IS 'Время начала выполнения задачи';
COMMENT ON COLUMN orchestrator.orchestrator_tasks.completed_at IS 'Время завершения выполнения задачи';
COMMENT ON COLUMN orchestrator.orchestrator_tasks.total_latency IS 'Общее время выполнения задачи (completed_at - created_at) в секундах';
COMMENT ON COLUMN orchestrator.orchestrator_tasks.run_latency IS 'Время исполнения задачи (completed_at - started_at) в секундах';
COMMENT ON COLUMN orchestrator.orchestrator_tasks.error_module IS 'Модуль, в котором произошла ошибка';
COMMENT ON COLUMN orchestrator.orchestrator_tasks.error_message IS 'Текст ошибки';
COMMENT ON COLUMN orchestrator.orchestrator_tasks.error_timestamp IS 'Время фиксации ошибки';
COMMENT ON COLUMN orchestrator.orchestrator_tasks.kaya_version IS 'Версия агента (из pyproject.toml), использовавшаяся при создании задачи';

-- Индексы для оптимизации запросов
CREATE INDEX IF NOT EXISTS idx_orchestrator_tasks_task_type_id ON orchestrator.orchestrator_tasks(task_type_id);
CREATE INDEX IF NOT EXISTS idx_orchestrator_tasks_parent_task_id ON orchestrator.orchestrator_tasks(parent_task_id);
CREATE INDEX IF NOT EXISTS idx_orchestrator_tasks_status ON orchestrator.orchestrator_tasks(status);
CREATE INDEX IF NOT EXISTS idx_orchestrator_tasks_created_at ON orchestrator.orchestrator_tasks(created_at);
CREATE INDEX IF NOT EXISTS idx_orchestrator_tasks_started_at ON orchestrator.orchestrator_tasks(started_at);
CREATE INDEX IF NOT EXISTS idx_orchestrator_tasks_completed_at ON orchestrator.orchestrator_tasks(completed_at);
CREATE INDEX IF NOT EXISTS idx_orchestrator_tasks_total_latency ON orchestrator.orchestrator_tasks (total_latency);
CREATE INDEX IF NOT EXISTS idx_orchestrator_tasks_run_latency ON orchestrator.orchestrator_tasks (run_latency);
CREATE INDEX IF NOT EXISTS idx_orchestrator_tasks_priority ON orchestrator.orchestrator_tasks (priority);

-- GIN индекс для JSONB полей
CREATE INDEX IF NOT EXISTS idx_orchestrator_tasks_input_data ON orchestrator.orchestrator_tasks USING gin (input_data);
CREATE INDEX IF NOT EXISTS idx_orchestrator_tasks_output_data ON orchestrator.orchestrator_tasks USING gin (output_data);


-- Блок 11: Таблица рассуждений (reasonings)
CREATE TABLE IF NOT EXISTS orchestrator.reasonings (
    -- Основные идентификаторы
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    orchestrator_step_id UUID,
    reasoning_content TEXT NOT NULL,
    reasoning_content_type reasoning_content_type NOT NULL,
    qdrant_point_id UUID,
    qdrant_timestamp TIMESTAMPTZ,
    emb_metric_id UUID,  -- Ссылка на таблицу metrics.emb_internal (будет создана позже)
    kaya_version TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Подробные комментарии к таблице
COMMENT ON TABLE orchestrator.reasonings IS 'Таблица рассуждений (reasonings) оркестратора. Содержит внутренние мыслительные процессы агента, цепочки рассуждений и саморефлексию.';

-- Комментарии к колонкам
COMMENT ON COLUMN orchestrator.reasonings.id IS 'Уникальный идентификатор рассуждения (UUID)';
COMMENT ON COLUMN orchestrator.reasonings.orchestrator_step_id IS 'Ссылка на шаг оркестратора (задачу), в рамках которого было сгенерировано рассуждение. Идентификатор шага внутри цепочки рассуждений.';
COMMENT ON COLUMN orchestrator.reasonings.reasoning_content IS 'Выходной блок /think модели - результат рассуждения агента. Содержит внутренний монолог, логические цепочки, размышления.';
COMMENT ON COLUMN orchestrator.reasonings.reasoning_content_type IS 'Тип источника контекста: messages - из сообщений диалога, reflection - из саморефлексии агента.';
COMMENT ON COLUMN orchestrator.reasonings.qdrant_point_id IS 'Идентификатор точки в векторной базе Qdrant для данного рассуждения (для семантического поиска и долговременной памяти)';
COMMENT ON COLUMN orchestrator.reasonings.qdrant_timestamp IS 'Метка времени сохранения вектора в Qdrant и присвоения qdrant_point_id.';
COMMENT ON COLUMN orchestrator.reasonings.emb_metric_id IS 'Ссылка на метрики обработки эмбеддинга (таблица metrics.emb_internal). Содержит информацию о времени и качестве векторизации.';
COMMENT ON COLUMN orchestrator.reasonings.kaya_version IS 'Версия агента Kaya (из pyproject.toml), использовавшаяся при создании рассуждения';
COMMENT ON COLUMN orchestrator.reasonings.timestamp IS 'Дата и время создания записи рассуждения';

-- Индексы для оптимизации запросов
CREATE INDEX idx_reasonings_orchestrator_step ON orchestrator.reasonings (orchestrator_step_id);
CREATE INDEX idx_reasonings_reasoning_content_type ON orchestrator.reasonings (reasoning_content_type);
CREATE INDEX idx_reasonings_qdrant_point_id ON orchestrator.reasonings (qdrant_point_id) WHERE qdrant_point_id IS NOT NULL;
CREATE INDEX idx_reasonings_emb_metric ON orchestrator.reasonings (emb_metric_id) WHERE emb_metric_id IS NOT NULL;
CREATE INDEX idx_reasonings_kaya_version ON orchestrator.reasonings (kaya_version);

-- GIN индекс для полнотекстового поиска по содержанию рассуждений
CREATE INDEX idx_reasonings_reasoning_content ON orchestrator.reasonings USING gin(to_tsvector('russian', reasoning_content))
    WHERE reasoning_content IS NOT NULL;


-- Блок 12: Таблица шагов оркестратора.
CREATE TABLE IF NOT EXISTS orchestrator.orchestrator_steps (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    parent_step_id UUID REFERENCES orchestrator.orchestrator_steps(id) ON DELETE SET NULL,
    task_type_name TEXT, 
    task_id UUID NOT NULL REFERENCES orchestrator.orchestrator_tasks(id) ON DELETE RESTRICT,
    step_number INTEGER NOT NULL,
    step_name TEXT, 
    step_type_id UUID NOT NULL REFERENCES orchestrator.step_types(id) ON DELETE RESTRICT,
    status task_status NOT NULL DEFAULT 'pending',
    input_data JSONB,
    output_data JSONB,
    reasoning_id UUID REFERENCES orchestrator.reasonings(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    latency FLOAT,
    error_module TEXT,
    error_message TEXT,
    error_timestamp TIMESTAMPTZ,
    kaya_version TEXT NOT NULL
);

-- Подробные комментарии к таблице
COMMENT ON TABLE orchestrator.orchestrator_steps IS 'Лог выполнения шагов оркестратора.';

-- Комментарии к колонкам
COMMENT ON COLUMN orchestrator.orchestrator_steps.id IS 'Уникальный идентификатор шага (UUID).';
COMMENT ON COLUMN orchestrator.orchestrator_steps.parent_step_id IS 'Родительский шаг.';
COMMENT ON COLUMN orchestrator.orchestrator_steps.step_name IS 'Системное имя типа шага (из orchestrator.step_types.step_name). Заполняется автоматически.';
COMMENT ON COLUMN orchestrator.orchestrator_steps.task_type_name IS 'Название типа задачи (из orchestrator.task_types.type_name). Заполняется автоматически.';
COMMENT ON COLUMN orchestrator.orchestrator_steps.task_id IS 'Ссылка на задачу.';
COMMENT ON COLUMN orchestrator.orchestrator_steps.step_number IS 'Порядковый номер шага внутри задачи.';
COMMENT ON COLUMN orchestrator.orchestrator_steps.step_type_id IS 'Ссылка на тип шага.';
COMMENT ON COLUMN orchestrator.orchestrator_steps.status IS 'Статус выполнения шага.';
COMMENT ON COLUMN orchestrator.orchestrator_steps.input_data IS 'Входные данные шага.';
COMMENT ON COLUMN orchestrator.orchestrator_steps.output_data IS 'Выходные данные шага.';
COMMENT ON COLUMN orchestrator.orchestrator_steps.reasoning_id IS 'Ссылка на запись рассуждения в таблице reasonings';
COMMENT ON COLUMN orchestrator.orchestrator_steps.created_at IS 'Время создания шага.';
COMMENT ON COLUMN orchestrator.orchestrator_steps.completed_at IS 'Время завершения шага.';
COMMENT ON COLUMN orchestrator.orchestrator_steps.latency IS 'Задержка выполнения шага в секундах.';
COMMENT ON COLUMN orchestrator.orchestrator_steps.error_message IS 'Текст ошибки.';
COMMENT ON COLUMN orchestrator.orchestrator_steps.error_module IS 'Модуль, в котором произошла ошибка.';
COMMENT ON COLUMN orchestrator.orchestrator_steps.error_timestamp IS 'Время фиксации ошибки.';
COMMENT ON COLUMN orchestrator.orchestrator_steps.kaya_version IS 'Версия агента Kaya (из pyproject.toml), использовавшаяся при выполнении шага.';

-- Индексы для оптимизации запросов
CREATE INDEX idx_orchestrator_steps_task_id ON orchestrator.orchestrator_steps (task_id);
CREATE INDEX idx_orchestrator_steps_step_type_id ON orchestrator.orchestrator_steps (step_type_id);
CREATE INDEX idx_orchestrator_steps_status ON orchestrator.orchestrator_steps (status);
CREATE INDEX idx_orchestrator_steps_parent_id ON orchestrator.orchestrator_steps (parent_step_id);
CREATE INDEX idx_orchestrator_steps_reasoning_id ON orchestrator.orchestrator_steps (reasoning_id);
CREATE INDEX idx_orchestrator_steps_created_at ON orchestrator.orchestrator_steps (created_at);
CREATE INDEX idx_orchestrator_steps_completed_at ON orchestrator.orchestrator_steps (completed_at);

-- GIN индекс для JSONB полей
CREATE INDEX idx_orchestrator_steps_input_data ON orchestrator.orchestrator_steps USING gin (input_data);
CREATE INDEX idx_orchestrator_steps_output_data ON orchestrator.orchestrator_steps USING gin (output_data);

-- Уникальность: один шаг с номером N в рамках одной задачи
CREATE UNIQUE INDEX idx_orchestrator_steps_task_step_unique 
ON orchestrator.orchestrator_steps (task_id, step_number);

-- Триггерная функция для заполнения step_name и task_type_name
CREATE OR REPLACE FUNCTION orchestrator.populate_step_enriched_fields()
RETURNS TRIGGER AS $$
BEGIN
    -- Заполняем step_name из step_types
    IF NEW.step_type_id IS NOT NULL THEN
        SELECT st.step_name
        INTO NEW.step_name
        FROM orchestrator.step_types st
        WHERE st.id = NEW.step_type_id;
    END IF;

    -- Заполняем task_type_name из task_types (через orchestrator_tasks)
    IF NEW.task_id IS NOT NULL THEN
        SELECT tt.type_name
        INTO NEW.task_type_name
        FROM orchestrator.orchestrator_tasks ot
        JOIN orchestrator.task_types tt ON ot.task_type_id = tt.id
        WHERE ot.id = NEW.task_id;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Удаляем и создаём триггер
DROP TRIGGER IF EXISTS trg_step_populate_enriched ON orchestrator.orchestrator_steps;
CREATE TRIGGER trg_step_populate_enriched
BEFORE INSERT ON orchestrator.orchestrator_steps
FOR EACH ROW
EXECUTE FUNCTION orchestrator.populate_step_enriched_fields();

-- Добавляем FK из reasonings → orchestrator_steps
ALTER TABLE orchestrator.reasonings
ADD CONSTRAINT fk_reasonings_orchestrator_step_id
FOREIGN KEY (orchestrator_step_id) REFERENCES orchestrator.orchestrator_steps(id) ON DELETE SET NULL;


-- Блок 13: Создание таблицы метрик внутренних LLM запросов
CREATE TABLE IF NOT EXISTS metrics.llm_internal (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    host TEXT,
    orchestrator_step_id UUID REFERENCES orchestrator.orchestrator_steps(id) ON DELETE RESTRICT,
    orchestrator_step_name TEXT,
    prompt_id UUID REFERENCES orchestrator.prompts(id) ON DELETE RESTRICT,
    param JSONB,
    model TEXT NOT NULL,
    cache_n INTEGER, 
    prompt_tokens INTEGER,
    completion_tokens INTEGER,  
    total_tokens INTEGER,
    host_nctx INTEGER,
    prompt_ms FLOAT, 
    prompt_per_token_ms FLOAT, 
    prompt_per_second FLOAT, 
    predicted_per_second FLOAT,
    resp_time FLOAT,
    net_latency FLOAT,
    full_time FLOAT,
    error_status BOOLEAN NOT NULL DEFAULT false,
    error_message TEXT,
    error_time TIMESTAMPTZ,
    kaya_version TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Подробные комментарии к таблице
COMMENT ON TABLE metrics.llm_internal IS 'Таблица метрик внутренних LLM запросов. Содержит технические параметры и результаты генерации.';

-- Комментарии к колонкам
COMMENT ON COLUMN metrics.llm_internal.id IS 'Уникальный идентификатор операции LLM.';
COMMENT ON COLUMN metrics.llm_internal.orchestrator_step_id IS 'Внешний ключ на шаг оркестратора. Ссылка на процесс, инициировавший LLM запрос.';
COMMENT ON COLUMN metrics.llm_internal.orchestrator_step_name IS 'Описание типа шага оркестратора (из orchestrator.step_types.step_name).';
COMMENT ON COLUMN metrics.llm_internal.prompt_id IS 'Внешний ключ на промпт. Идентификатор использованного промпта.';
COMMENT ON COLUMN metrics.llm_internal.param IS 'Параметры генерации в формате JSON (temperature, top_p, top_k, max_tokens и др)';
COMMENT ON COLUMN metrics.llm_internal.model IS 'Название примененной модели';
COMMENT ON COLUMN metrics.llm_internal.cache_n IS 'Сколько токенов из запроса (промпта) было взято из кэша. 0 - кэш не использовался';
COMMENT ON COLUMN metrics.llm_internal.prompt_tokens IS 'Количество токенов в промпте (входные данные)';
COMMENT ON COLUMN metrics.llm_internal.completion_tokens IS 'Количество токенов, сгенерированных моделью в ответе';
COMMENT ON COLUMN metrics.llm_internal.total_tokens IS 'Общее количество токенов, обработанных за этот запрос';
COMMENT ON COLUMN metrics.llm_internal.host_nctx IS 'Размер контекста (n_ctx), настроенный на хосте для модели';
COMMENT ON COLUMN metrics.llm_internal.prompt_ms IS 'Время в миллисекундах на обработку промпта';
COMMENT ON COLUMN metrics.llm_internal.prompt_per_token_ms IS 'Среднее время обработки одного токена промпта в миллисекундах';
COMMENT ON COLUMN metrics.llm_internal.prompt_per_second IS 'Средняя скорость обработки токенов промпта в секунду';
COMMENT ON COLUMN metrics.llm_internal.predicted_per_second IS 'Средняя скорость генерации токенов ответа в секунду';
COMMENT ON COLUMN metrics.llm_internal.resp_time IS 'Общее время генерации ответа в секундах';
COMMENT ON COLUMN metrics.llm_internal.net_latency IS 'Задержка сети при выполнении запроса (секунды)';
COMMENT ON COLUMN metrics.llm_internal.full_time IS 'Общее время выполнения запроса от клиента до сервера (секунды)';
COMMENT ON COLUMN metrics.llm_internal.error_status IS 'Флаг наличия ошибок при генерации';
COMMENT ON COLUMN metrics.llm_internal.error_message IS 'Текстовое описание ошибки генерации';
COMMENT ON COLUMN metrics.llm_internal.error_time IS 'Метка времени фиксации ошибки';
COMMENT ON COLUMN metrics.llm_internal.kaya_version IS 'Версия агента Kaya (из pyproject.toml) на момент выполнения запроса';
COMMENT ON COLUMN metrics.llm_internal.timestamp IS 'Метка времени создания записи метрики';

-- Индексы для оптимизации запросов
CREATE INDEX idx_llm_internal_id ON metrics.llm_internal (id);
CREATE INDEX idx_llm_internal_orchestrator_step ON metrics.llm_internal (orchestrator_step_id);
CREATE INDEX idx_llm_internal_prompt_id ON metrics.llm_internal (prompt_id);
CREATE INDEX idx_llm_internal_model ON metrics.llm_internal (model);
CREATE INDEX idx_llm_internal_host ON metrics.llm_internal (host);
CREATE INDEX idx_llm_internal_predicted_per_second ON metrics.llm_internal (predicted_per_second);
CREATE INDEX idx_llm_internal_error_status ON metrics.llm_internal (error_status);
CREATE INDEX idx_llm_internal_kaya_version ON metrics.llm_internal (kaya_version);
CREATE INDEX idx_llm_internal_timestamp ON metrics.llm_internal (timestamp);


-- Триггер для автоматического заполнения orchestrator_step_name
CREATE OR REPLACE FUNCTION metrics.populate_llm_step_name()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.orchestrator_step_id IS NOT NULL THEN
        SELECT st.step_name
        INTO NEW.orchestrator_step_name
        FROM orchestrator.orchestrator_steps os
        JOIN orchestrator.step_types st ON os.step_type_id = st.id
        WHERE os.id = NEW.orchestrator_step_id;
    ELSE
        NEW.orchestrator_step_name := NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Идемпотентное создание триггера
DROP TRIGGER IF EXISTS trg_llm_populate_step_name ON metrics.llm_internal;
CREATE TRIGGER trg_llm_populate_step_name
BEFORE INSERT ON metrics.llm_internal
FOR EACH ROW
EXECUTE FUNCTION metrics.populate_llm_step_name();


-- Блок 14: Создание таблицы метрик внутренних эмбеддингов
CREATE TABLE IF NOT EXISTS metrics.emb_internal (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    orchestrator_step_id UUID REFERENCES orchestrator.orchestrator_steps(id) ON DELETE RESTRICT,
    orchestrator_step_name TEXT,
    host TEXT,
    model TEXT NOT NULL,
    param JSONB,
    vector_dimension INTEGER,
    prompt_tokens INTEGER, --Количество токенов в промпте (входные данные)
    out_time TIMESTAMPTZ,  -- время отправки запроса на emb-сервер
    in_time TIMESTAMPTZ,   -- время получения ответа от emb-сервера
    full_time FLOAT, -- общее время генерации
    error_status BOOLEAN NOT NULL DEFAULT false,
    error_message TEXT,
    error_time TIMESTAMPTZ,
    kaya_version TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Подробные комментарии к таблице
COMMENT ON TABLE metrics.emb_internal IS 'Таблица метрик внутренних эмбеддингов. Содержит технические параметры и результаты генерации 
векторов для анализа производительности и отладки.';

-- Комментарии к колонкам
COMMENT ON COLUMN metrics.emb_internal.id IS 'Уникальный идентификатор операции эмбеддинга (UUID)';
COMMENT ON COLUMN metrics.emb_internal.host IS 'Имя сервера/хоста, на котором выполнялась генерация эмбеддинга';
COMMENT ON COLUMN metrics.emb_internal.orchestrator_step_id IS 'Ссылка на шаг оркестратора, инициировавший запрос эмбеддинга. Позволяет связать метрики с конкретным шагом обработки.';
COMMENT ON COLUMN metrics.emb_internal.orchestrator_step_name IS 'Описание типа шага оркестратора (из orchestrator.step_types.step_name). Заполняется автоматически триггером.';
COMMENT ON COLUMN metrics.emb_internal.model IS 'Название примененной модели эмбеддингов (например: "text-embedding-3-small", "intfloat/multilingual-e5-large")';
COMMENT ON COLUMN metrics.emb_internal.param IS 'Параметры генерации эмбеддинга в формате JSON (размерность, нормализация, pooling и др.)';
COMMENT ON COLUMN metrics.emb_internal.vector_dimension IS 'Размерность полученного эмбеддинга (например 1024, 2560, 4096). Зависит от модели и настроек.';
COMMENT ON COLUMN metrics.emb_internal.prompt_tokens IS 'Количество токенов в промпте (входные данные для векторизации)';
COMMENT ON COLUMN metrics.emb_internal.out_time IS 'Время отправки запроса на emb-сервер (начало операции)';
COMMENT ON COLUMN metrics.emb_internal.in_time IS 'Время получения ответа от emb-сервера (окончание операции)';
COMMENT ON COLUMN metrics.emb_internal.full_time IS 'Общее время генерации эмбеддинга в секундах (разница между in_time и out_time)';
COMMENT ON COLUMN metrics.emb_internal.error_status IS 'Флаг наличия ошибок при генерации: true - была ошибка, false - успешно';
COMMENT ON COLUMN metrics.emb_internal.error_message IS 'Текстовое описание ошибки генерации (заполняется при error_status = true)';
COMMENT ON COLUMN metrics.emb_internal.error_time IS 'Метка времени фиксации ошибки';
COMMENT ON COLUMN metrics.emb_internal.kaya_version IS 'Версия агента Kaya (из pyproject.toml) на момент выполнения запроса эмбеддинга';
COMMENT ON COLUMN metrics.emb_internal.timestamp IS 'Метка времени создания записи метрики';

-- Индексы для оптимизации запросов
CREATE INDEX idx_emb_internal_id ON metrics.emb_internal (id);
CREATE INDEX idx_emb_internal_orchestrator_step ON metrics.emb_internal (orchestrator_step_id);
CREATE INDEX idx_emb_internal_model ON metrics.emb_internal (model);
CREATE INDEX idx_emb_internal_host ON metrics.emb_internal (host);
CREATE INDEX idx_emb_internal_error_status ON metrics.emb_internal (error_status);
CREATE INDEX idx_emb_internal_timestamp ON metrics.emb_internal (timestamp);
CREATE INDEX idx_emb_internal_kaya_version ON metrics.emb_internal (kaya_version);
CREATE INDEX idx_emb_internal_full_time ON metrics.emb_internal (full_time);
CREATE INDEX idx_emb_internal_prompt_tokens ON metrics.emb_internal (prompt_tokens);

-- Триггер для автоматического заполнения orchestrator_step_name
CREATE OR REPLACE FUNCTION metrics.populate_emb_step_name()
RETURNS TRIGGER AS $$
BEGIN
    -- Заполняем только если orchestrator_step_id задан
    IF NEW.orchestrator_step_id IS NOT NULL THEN
        SELECT st.step_name
        INTO NEW.orchestrator_step_name
        FROM orchestrator.orchestrator_steps os
        JOIN orchestrator.step_types st ON os.step_type_id = st.id
        WHERE os.id = NEW.orchestrator_step_id;
        -- Если JOIN не дал результат, NEW.orchestrator_step_name станет NULL — это допустимо
    ELSE
        NEW.orchestrator_step_name := NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION metrics.populate_emb_step_name IS 'Триггерная функция для автоматического заполнения orchestrator_step_name из справочника step_types';

-- Триггер
DROP TRIGGER IF EXISTS trg_emb_populate_step_name ON metrics.emb_internal;
CREATE TRIGGER trg_emb_populate_step_name
BEFORE INSERT ON metrics.emb_internal
FOR EACH ROW
EXECUTE FUNCTION metrics.populate_emb_step_name();

-- Добавляем внешний ключ на orchestrator.reasonings.emb_metric_id
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints 
        WHERE constraint_name = 'fk_reasonings_emb_metric_id'
    ) THEN
ALTER TABLE orchestrator.reasonings
ADD CONSTRAINT fk_reasonings_emb_metric_id
FOREIGN KEY (emb_metric_id) REFERENCES metrics.emb_internal(id) ON DELETE RESTRICT;
 END IF;
END $$;
COMMENT ON CONSTRAINT fk_reasonings_emb_metric_id ON orchestrator.reasonings IS 'Связь рассуждений с метриками эмбеддингов. 
Позволяет анализировать качество векторизации мыслительных процессов.';


-- Блок 15: Создание таблицы предразбора сообщений пользователей
CREATE TABLE IF NOT EXISTS orchestrator.preprocessed_results (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    message_id UUID,
    preprocessed_result JSONB,
    llm_metric_id UUID REFERENCES metrics.llm_internal(id) ON DELETE RESTRICT,
    qdrant_point_id UUID,
    emb_metric_id UUID REFERENCES metrics.emb_internal(id) ON DELETE RESTRICT,
    kaya_version TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Подробные комментарии к таблице
COMMENT ON TABLE orchestrator.preprocessed_results IS 'Таблица предразбора сообщений пользователей. Содержит результаты анализа и структурирования 
входящих сообщений перед основной обработкой.';


-- Комментарии к колонкам
COMMENT ON COLUMN orchestrator.preprocessed_results.id IS 'Уникальный идентификатор записи предразбора (UUID)';
COMMENT ON COLUMN orchestrator.preprocessed_results.message_id IS 'Ссылка на исходное сообщение пользователя в таблице dialogs.messages. Позволяет 
связать предразбор с конкретным сообщением.';
COMMENT ON COLUMN orchestrator.preprocessed_results.preprocessed_result IS 'Результат предразбора сообщения в формате JSONB. Содержит структурированные 
данные: извлеченные сущности, интенты, эмоциональную окраску, классификацию темы и другие результаты анализа.';
COMMENT ON COLUMN orchestrator.preprocessed_results.llm_metric_id IS 'Ссылка на метрики LLM, использованной для предразбора 
(если применялась генеративная модель). Позволяет анализировать производительность и затраты.';
COMMENT ON COLUMN orchestrator.preprocessed_results.emb_metric_id IS 'Ссылка на метрики эмбеддинга, полученного при векторизации сообщения 
(если применялась). Позволяет анализировать качество векторизации.';
COMMENT ON COLUMN orchestrator.preprocessed_results.qdrant_point_id IS 'Идентификатор точки в векторной базе Qdrant для вектора сообщения. 
Используется для семантического поиска и кластеризации.';
COMMENT ON COLUMN orchestrator.preprocessed_results.kaya_version IS 'Версия агента Kaya (из pyproject.toml), использовавшаяся при предразборе сообщения';
COMMENT ON COLUMN orchestrator.preprocessed_results.timestamp IS 'Метка времени создания записи предразбора';

-- Индексы для оптимизации запросов
CREATE INDEX idx_preprocessed_results_message_id ON orchestrator.preprocessed_results (message_id);
CREATE INDEX idx_preprocessed_results_llm_metric_id ON orchestrator.preprocessed_results (llm_metric_id);
CREATE INDEX idx_preprocessed_results_emb_metric_id ON orchestrator.preprocessed_results (emb_metric_id);
CREATE INDEX idx_preprocessed_results_qdrant_point ON orchestrator.preprocessed_results (qdrant_point_id) WHERE qdrant_point_id IS NOT NULL;
CREATE INDEX idx_preprocessed_results_timestamp ON orchestrator.preprocessed_results (timestamp);
CREATE INDEX idx_preprocessed_results_kaya_version ON orchestrator.preprocessed_results (kaya_version);

-- GIN индекс для поиска по JSONB полю preprocessed_result
CREATE INDEX idx_preprocessed_results_data ON orchestrator.preprocessed_results USING gin (preprocessed_result);


-- Блок 16: Создание таблицы "сырых" сообщений диалогов
CREATE TABLE IF NOT EXISTS dialogs.messages (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    parent_message_id UUID REFERENCES dialogs.messages(id) ON DELETE SET NULL,
    actor_id UUID NOT NULL REFERENCES users.actors(id) ON DELETE RESTRICT,
    actor_type actor_type NOT NULL,
    session_id UUID NOT NULL REFERENCES dialogs.sessions(id) ON DELETE RESTRICT,
    room_id UUID NOT NULL REFERENCES dialogs.rooms(id) ON DELETE RESTRICT, -- Идентификатор комнаты диалога
    row_text TEXT NOT NULL, -- Чистое сообщение
    processed_text TEXT, -- Для приведения сообщения к "чистому формату" без ошибок фоновой рефлексией
    token_count INTEGER,
    answer_latency FLOAT, -- общее время ответа (временем записи сообщения и временем parent_message_id)
    timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),
    preprocess_result_id UUID REFERENCES orchestrator.preprocessed_results(id) ON DELETE RESTRICT,
    orchestrator_step_id UUID REFERENCES orchestrator.orchestrator_steps(id) ON DELETE RESTRICT,
    llm_metric_id UUID REFERENCES metrics.llm_internal(id) ON DELETE RESTRICT,
    emb_metric_id UUID REFERENCES metrics.emb_internal(id) ON DELETE RESTRICT,
    qdrant_point_id UUID,
    kaya_version TEXT NOT NULL
);

-- Подробные комментарии к таблице
COMMENT ON TABLE dialogs.messages IS 'Таблица сырых сообщений диалогов. Содержит все сообщения пользователей и агента с полным 
контекстом и метаданными обработки.';

-- Комментарии к колонкам
COMMENT ON COLUMN dialogs.messages.id IS 'Уникальный идентификатор сообщения (UUID)';
COMMENT ON COLUMN dialogs.messages.parent_message_id IS 'Ссылка на родительское сообщение (для ответов, цепочек и тредов). Позволяет строить иерархию сообщений и измерять latency ответа.';
COMMENT ON COLUMN dialogs.messages.actor_id IS 'ID отправителя сообщения (пользователь или агент). Ссылка на таблицу users.actors.';
COMMENT ON COLUMN dialogs.messages.actor_type IS 'Тип отправителя: user - пользователь, system - агент Kaya, owner - владелец системы и т.д. Дублируется из actors для оптимизации запросов.';
COMMENT ON COLUMN dialogs.messages.session_id IS 'ID сессии диалога, в рамках которой отправлено сообщение. Связывает сообщения в непрерывный диалог.';
COMMENT ON COLUMN dialogs.messages.room_id IS 'ID комнаты диалога, в которой отправлено сообщение. Определяет тематический контекст.';
COMMENT ON COLUMN dialogs.messages.row_text IS 'Исходный текст сообщения в том виде, в котором он получен (с возможными ошибками, опечатками, сленгом)';
COMMENT ON COLUMN dialogs.messages.processed_text IS 'Обработанный текст сообщения после приведения к чистому формату (исправление опечаток, нормализация, удаление шума). Заполняется фоновой рефлексией.';
COMMENT ON COLUMN dialogs.messages.token_count IS 'Количество токенов в сообщении (для анализа стоимости и производительности)';
COMMENT ON COLUMN dialogs.messages.answer_latency IS 'Общее время ответа в секундах. Рассчитывается как разница между timestamp текущего сообщения и timestamp родительского сообщения (parent_message_id). Для сообщений пользователя обычно NULL.';
COMMENT ON COLUMN dialogs.messages.timestamp IS 'Метка времени отправки/получения сообщения';
COMMENT ON COLUMN dialogs.messages.preprocess_result_id IS 'Ссылка на результат предразбора сообщения. Содержит структурированные данные анализа (интенты, сущности, эмоции).';
COMMENT ON COLUMN dialogs.messages.orchestrator_step_id IS 'Ссылка на шаг оркестратора, обработавший сообщение. Позволяет отследить весь путь обработки.';
COMMENT ON COLUMN dialogs.messages.llm_metric_id IS 'Ссылка на метрики LLM, использованной для генерации ответа (для сообщений агента) или обработки (для сообщений пользователя)';
COMMENT ON COLUMN dialogs.messages.emb_metric_id IS 'Ссылка на метрики эмбеддинга сообщения. Позволяет анализировать качество векторизации.';
COMMENT ON COLUMN dialogs.messages.qdrant_point_id IS 'Идентификатор точки в векторной базе Qdrant для вектора сообщения. Используется для семантического поиска и памяти.';
COMMENT ON COLUMN dialogs.messages.kaya_version IS 'Версия агента Kaya (из pyproject.toml), использовавшаяся при обработке сообщения';

-- Индексы для оптимизации запросов
CREATE INDEX IF NOT EXISTS idx_messages_parent_id ON dialogs.messages (parent_message_id);
CREATE INDEX IF NOT EXISTS idx_messages_actor_id ON dialogs.messages (actor_id);
CREATE INDEX IF NOT EXISTS idx_messages_actor_type ON dialogs.messages (actor_type);
CREATE INDEX IF NOT EXISTS idx_messages_session_id ON dialogs.messages (session_id);
CREATE INDEX IF NOT EXISTS idx_messages_room_id ON dialogs.messages (room_id);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON dialogs.messages (timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_kaya_version ON dialogs.messages (kaya_version);
CREATE INDEX IF NOT EXISTS idx_messages_preprocess_result ON dialogs.messages (preprocess_result_id);
CREATE INDEX IF NOT EXISTS idx_messages_orchestrator_step ON dialogs.messages (orchestrator_step_id);
CREATE INDEX IF NOT EXISTS idx_messages_llm_metric ON dialogs.messages (llm_metric_id);
CREATE INDEX IF NOT EXISTS idx_messages_emb_metric ON dialogs.messages (emb_metric_id);
CREATE INDEX IF NOT EXISTS idx_messages_qdrant_point ON dialogs.messages (qdrant_point_id) WHERE qdrant_point_id IS NOT NULL;

-- Индекс для поиска по тексту (если нужен полнотекстовый поиск)
CREATE INDEX IF NOT EXISTS idx_messages_row_text_search ON dialogs.messages USING gin(to_tsvector('russian', row_text));
CREATE INDEX IF NOT EXISTS idx_messages_processed_text_search ON dialogs.messages USING gin(to_tsvector('russian', processed_text)) WHERE processed_text IS NOT NULL;

-- Добавляем FK в orchestrator.preprocessed_results
ALTER TABLE orchestrator.preprocessed_results
    ADD CONSTRAINT fk_preprocessed_results_message_id
    FOREIGN KEY (message_id) REFERENCES dialogs.messages(id) ON DELETE SET NULL;
