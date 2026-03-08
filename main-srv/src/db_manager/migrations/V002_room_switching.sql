-- =============================================
-- Миграция: 002_room_switching.sql
-- Версия: V002
-- Описание: Переключение комнат, effective_room_id, логирование переходов, реклассификация
-- =============================================

-- ============================================================================
-- Блок 1: ENUM для типов триггеров переключения комнат
-- ============================================================================
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'room_switch_trigger_type') THEN
        CREATE TYPE room_switch_trigger_type AS ENUM (
            'explicit_user_request',      -- Пользователь запросил переход в комнату
            'auto_high_confidence'        -- Авто-переключение по высокой уверенности модели (>=CONFIDENCE_THRESHOLD_AUTO_SWITCH)
        );
    END IF;
END $$;
COMMENT ON TYPE room_switch_trigger_type IS 'Типы триггеров переключения комнат';

-- ============================================================================
-- Блок 2: NUM для типов реклассификации сообщений (messages_rooms_reclassifications)
-- ============================================================================

-- Создание ENUM для типа реклассификации
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'reclassification_type') THEN
        CREATE TYPE reclassification_type AS ENUM (
            'internal_model',     -- Внутренняя модель
            'external_model'      -- Внешняя модель  
        );
    END IF;
END $$;
COMMENT ON TYPE reclassification_type IS 'Тип модели реклассификации внутренняя/внешняя';

-- ============================================================================
-- Блок 3: Добавляем effective_room_id и reclassification_step_id в messages
-- ============================================================================
ALTER TABLE dialogs.messages 
ADD COLUMN IF NOT EXISTS effective_room_id UUID REFERENCES dialogs.rooms(id) ON DELETE SET NULL,
ADD COLUMN IF NOT EXISTS reclassification_step_id UUID REFERENCES orchestrator.orchestrator_steps(id) ON DELETE SET NULL,
ADD COLUMN IF NOT EXISTS effective_room_updated_at TIMESTAMPTZ;

COMMENT ON COLUMN dialogs.messages.effective_room_id IS 'Логическая комната сообщения. По умолчанию = room_id. Используется для RAG.';
COMMENT ON COLUMN dialogs.messages.reclassification_step_id IS 'Ссылка на шаг оркестратора, который инициировал реклассификацию.';
COMMENT ON COLUMN dialogs.messages.effective_room_updated_at IS 'Время последнего обновления effective_room_id.';

CREATE INDEX IF NOT EXISTS idx_messages_effective_room_id ON dialogs.messages (effective_room_id);
CREATE INDEX IF NOT EXISTS idx_messages_effective_room_updated_at ON dialogs.messages (effective_room_updated_at);
CREATE INDEX IF NOT EXISTS idx_messages_reclassification_step_id ON dialogs.messages (reclassification_step_id);

CREATE OR REPLACE FUNCTION dialogs.set_effective_room_default()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.effective_room_id IS NULL THEN
        NEW.effective_room_id := NEW.room_id;
        NEW.effective_room_updated_at := NEW.timestamp;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_messages_set_effective_room ON dialogs.messages;
CREATE TRIGGER trg_messages_set_effective_room
BEFORE INSERT ON dialogs.messages
FOR EACH ROW
EXECUTE FUNCTION dialogs.set_effective_room_default();

-- ============================================================================
-- Блок 4: Таблица истории переключений комнат (сессии)
-- ============================================================================
CREATE TABLE IF NOT EXISTS dialogs.room_transitions (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES dialogs.sessions(id) ON DELETE CASCADE,
    triggering_message_id UUID NOT NULL REFERENCES dialogs.messages(id) ON DELETE RESTRICT,
    from_room_id UUID REFERENCES dialogs.rooms(id),
    to_room_id UUID NOT NULL REFERENCES dialogs.rooms(id),
    trigger_type room_switch_trigger_type NOT NULL,
    confidence_score FLOAT,
    model_weights JSONB,
    kaya_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE dialogs.room_transitions IS 'История переключений между комнатами в рамках сессии диалога.';
COMMENT ON COLUMN dialogs.room_transitions.id IS 'Уникальный идентификатор записи перехода (UUID)';
COMMENT ON COLUMN dialogs.room_transitions.session_id IS 'ID сессии диалога.';
COMMENT ON COLUMN dialogs.room_transitions.triggering_message_id IS 'ID сообщения, которое спровоцировало переключение.';
COMMENT ON COLUMN dialogs.room_transitions.from_room_id IS 'ID комнаты, из которой произошел переход.';
COMMENT ON COLUMN dialogs.room_transitions.to_room_id IS 'ID комнаты, в которую произошел переход.';
COMMENT ON COLUMN dialogs.room_transitions.trigger_type IS 'Тип триггера переключения (ENUM).';
COMMENT ON COLUMN dialogs.room_transitions.confidence_score IS 'Уверенность модели (0.0-1.0).';
COMMENT ON COLUMN dialogs.room_transitions.model_weights IS 'Полные веса всех комнат от модели JSONB.';
COMMENT ON COLUMN dialogs.room_transitions.kaya_version IS 'Версия агента Kaya (из pyproject.toml), выполнившая переключение.';
COMMENT ON COLUMN dialogs.room_transitions.created_at IS 'Время создания записи.';

CREATE INDEX idx_room_transitions_session ON dialogs.room_transitions(session_id);
CREATE INDEX idx_room_transitions_triggering_message ON dialogs.room_transitions(triggering_message_id);
CREATE INDEX idx_room_transitions_from_room ON dialogs.room_transitions(from_room_id);
CREATE INDEX idx_room_transitions_to_room ON dialogs.room_transitions(to_room_id);
CREATE INDEX idx_room_transitions_trigger_type ON dialogs.room_transitions(trigger_type);
CREATE INDEX idx_room_transitions_created_at ON dialogs.room_transitions(created_at);
CREATE INDEX idx_room_transitions_kaya_version ON dialogs.room_transitions(kaya_version);
CREATE INDEX idx_room_transitions_model_weights ON dialogs.room_transitions USING gin (model_weights);

-- ============================================================================
-- Блок 5: Добавляем current_room в sessions
-- ============================================================================
ALTER TABLE dialogs.sessions 
ADD COLUMN IF NOT EXISTS current_room UUID REFERENCES dialogs.rooms(id) ON DELETE SET NULL,
ADD COLUMN IF NOT EXISTS last_room_switch_at TIMESTAMPTZ;

COMMENT ON COLUMN dialogs.sessions.current_room IS 'Текущая активная комната сессии. Используется для выбора промпта и RAG.';
COMMENT ON COLUMN dialogs.sessions.last_room IS 'Предыдущая активная комната сессии (историческое).';
COMMENT ON COLUMN dialogs.sessions.last_room_switch_at IS 'Время последнего переключения комнаты.';

-- Создаем новые индексы с проверкой существования
CREATE INDEX IF NOT EXISTS idx_sessions_current_room ON dialogs.sessions(current_room);

CREATE INDEX IF NOT EXISTS idx_sessions_last_room_switch_at ON dialogs.sessions(last_room_switch_at);

-- Обновляем описание существующего индекса если не пересоздали
COMMENT ON INDEX dialogs.idx_sessions_last_room IS 'Индекс для поиска по предыдущей комнате сессии';

-- ============================================================================
-- Блок 6: Таблица реклассификации сообщений (messages_rooms_reclassifications)
-- ============================================================================
CREATE TABLE IF NOT EXISTS dialogs.messages_rooms_reclassifications (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    message_id UUID NOT NULL REFERENCES dialogs.messages(id) ON DELETE CASCADE,
    from_room_id UUID NOT NULL REFERENCES dialogs.rooms(id),
    to_room_id UUID NOT NULL REFERENCES dialogs.rooms(id),
    orchestrator_step_id UUID REFERENCES orchestrator.orchestrator_steps(id) ON DELETE SET NULL,
    agent_confidence FLOAT,
    model_name TEXT NOT NULL,
    reclassification_type reclassification_type NOT NULL,
    kaya_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE dialogs.messages_rooms_reclassifications IS 'История реклассификации сообщений по комнатам. effective_room_id меняется, room_id (аудит) — нет.';
COMMENT ON COLUMN dialogs.messages_rooms_reclassifications.id IS 'Уникальный идентификатор записи реклассификации (UUID)';
COMMENT ON COLUMN dialogs.messages_rooms_reclassifications.message_id IS 'ID переквалифицированного сообщения.';
COMMENT ON COLUMN dialogs.messages_rooms_reclassifications.from_room_id IS 'Предыдущая логическая комната.';
COMMENT ON COLUMN dialogs.messages_rooms_reclassifications.to_room_id IS 'Новая логическая комната.';
COMMENT ON COLUMN dialogs.messages_rooms_reclassifications.orchestrator_step_id IS 'Ссылка на шаг оркестратора для метрик.';
COMMENT ON COLUMN dialogs.messages_rooms_reclassifications.agent_confidence IS 'Уверенность агента (0.0-1.0).';
COMMENT ON COLUMN dialogs.messages_rooms_reclassifications.model_name IS 'Название модели, выполнившей реклассификацию (например: qwen, gpt4, bert-base).';
COMMENT ON COLUMN dialogs.messages_rooms_reclassifications.reclassification_type IS 'Тип реклассификации: internal - внутренняя модель, external - внешняя модель.';
COMMENT ON COLUMN dialogs.messages_rooms_reclassifications.kaya_version IS 'Версия агента Kaya.';
COMMENT ON COLUMN dialogs.messages_rooms_reclassifications.created_at IS 'Время создания записи.';

CREATE INDEX idx_message_reclass_message ON dialogs.messages_rooms_reclassifications(message_id);
CREATE INDEX idx_message_reclass_from_room ON dialogs.messages_rooms_reclassifications(from_room_id);
CREATE INDEX idx_message_reclass_to_room ON dialogs.messages_rooms_reclassifications(to_room_id);
CREATE INDEX idx_message_reclass_orchestrator_step ON dialogs.messages_rooms_reclassifications(orchestrator_step_id);
CREATE INDEX idx_message_reclass_model_name ON dialogs.messages_rooms_reclassifications(model_name);
CREATE INDEX idx_message_reclass_type ON dialogs.messages_rooms_reclassifications(reclassification_type);
CREATE INDEX idx_message_reclass_created_at ON dialogs.messages_rooms_reclassifications(created_at);

-- ============================================================================
-- Добавление типа задачи для реклассификации комнат сообщений
-- ============================================================================
-- Добавляем новый тип задачи в orchestrator.task_types
INSERT INTO orchestrator.task_types (type_name, description)
VALUES 
    ('message_room_reclassification', 'Фоновая реклассификация принадлежности сообщений к комнатам диалогов')
ON CONFLICT (type_name) DO NOTHING;

-- ============================================================================
-- Добавление типа шага для реклассификации комнат сообщений
-- ============================================================================
-- Добавляем новый тип шага в orchestrator.step_types
INSERT INTO orchestrator.step_types (step_name, description, kaya_version)
VALUES 
    ('message_room_reclassification', 'Фоновая реклассификация принадлежности сообщений к комнатам диалогов', '1.1.0')
ON CONFLICT (step_name) DO NOTHING;


-- ============================================================================
-- Блок 7: Представления для отладки
-- ============================================================================
CREATE OR REPLACE VIEW dialogs.v_session_room_history AS
SELECT 
    s.id AS session_id,
    s.actor_id,
    rt.created_at AS transition_at,
    rt.triggering_message_id,
    m.row_text AS triggering_message_text,
    rt.from_room_id,
    fr.name AS from_room_name,
    rt.to_room_id,
    tr.name AS to_room_name,
    rt.trigger_type,
    rt.confidence_score,
    rt.model_weights
FROM dialogs.sessions s
LEFT JOIN dialogs.room_transitions rt ON rt.session_id = s.id
LEFT JOIN dialogs.rooms fr ON fr.id = rt.from_room_id
LEFT JOIN dialogs.rooms tr ON tr.id = rt.to_room_id
LEFT JOIN dialogs.messages m ON m.id = rt.triggering_message_id
ORDER BY s.id, rt.created_at;

COMMENT ON VIEW dialogs.v_session_room_history IS 'История переключений комнат сессии с текстом сообщения-триггера.';

COMMENT ON COLUMN dialogs.v_session_room_history.session_id IS 'ID сессии.';
COMMENT ON COLUMN dialogs.v_session_room_history.actor_id IS 'ID участника.';
COMMENT ON COLUMN dialogs.v_session_room_history.transition_at IS 'Время переключения.';
COMMENT ON COLUMN dialogs.v_session_room_history.triggering_message_id IS 'ID сообщения-триггера.';
COMMENT ON COLUMN dialogs.v_session_room_history.triggering_message_text IS 'Текст сообщения-триггера.';
COMMENT ON COLUMN dialogs.v_session_room_history.from_room_id IS 'ID исходной комнаты.';
COMMENT ON COLUMN dialogs.v_session_room_history.from_room_name IS 'Имя исходной комнаты.';
COMMENT ON COLUMN dialogs.v_session_room_history.to_room_id IS 'ID целевой комнаты.';
COMMENT ON COLUMN dialogs.v_session_room_history.to_room_name IS 'Имя целевой комнаты.';
COMMENT ON COLUMN dialogs.v_session_room_history.trigger_type IS 'Тип триггера.';
COMMENT ON COLUMN dialogs.v_session_room_history.confidence_score IS 'Уверенность модели.';
COMMENT ON COLUMN dialogs.v_session_room_history.model_weights IS 'Веса комнат JSONB.';

CREATE OR REPLACE VIEW dialogs.v_message_reclass_history AS
SELECT 
    mr.created_at,
    m.id AS message_id,
    m.row_text AS message_text,
    m.room_id AS original_room,
    mr.from_room_id,
    mr.to_room_id,
    os.step_name AS orchestrator_step_name,
    mr.agent_confidence,
    m.effective_room_id AS current_effective_room,
    mr.model_name,
    mr.reclassification_type,
    CASE 
        WHEN mr.reclassification_type = 'internal_model' THEN 'Внутренняя модель'
        WHEN mr.reclassification_type = 'external_model' THEN 'Внешняя модель'
    END AS reclassification_type_text
FROM dialogs.messages_rooms_reclassifications mr
JOIN dialogs.messages m ON m.id = mr.message_id
LEFT JOIN orchestrator.orchestrator_steps os ON os.id = mr.orchestrator_step_id
ORDER BY mr.created_at DESC;

COMMENT ON VIEW dialogs.v_message_reclass_history IS 'История реклассификации сообщений с текстом.';

COMMENT ON COLUMN dialogs.v_message_reclass_history.created_at IS 'Время реклассификации.';
COMMENT ON COLUMN dialogs.v_message_reclass_history.message_id IS 'ID сообщения.';
COMMENT ON COLUMN dialogs.v_message_reclass_history.message_text IS 'Текст сообщения.';
COMMENT ON COLUMN dialogs.v_message_reclass_history.original_room IS 'Физическая комната (аудит).';
COMMENT ON COLUMN dialogs.v_message_reclass_history.from_room_id IS 'Предыдущая эффективная комната.';
COMMENT ON COLUMN dialogs.v_message_reclass_history.to_room_id IS 'Новая эффективная комната.';
COMMENT ON COLUMN dialogs.v_message_reclass_history.orchestrator_step_name IS 'Имя шага оркестратора.';
COMMENT ON COLUMN dialogs.v_message_reclass_history.agent_confidence IS 'Уверенность агента.';
COMMENT ON COLUMN dialogs.v_message_reclass_history.current_effective_room IS 'Текущая эффективная комната.';
COMMENT ON COLUMN dialogs.v_message_reclass_history.model_name IS 'Название модели, выполнившей реклассификацию.';
COMMENT ON COLUMN dialogs.v_message_reclass_history.reclassification_type IS 'Тип реклассификации (internal/external).';
COMMENT ON COLUMN dialogs.v_message_reclass_history.reclassification_type_text IS 'Тип реклассификации в текстовом формате.';

-- ============================================================================
-- Блок 8: Создание новых комнат (open_dialogue уже есть в V001)
-- ============================================================================

INSERT INTO dialogs.rooms (name, description, status, kaya_version, created_at) VALUES
('agent_dev', 
 'Саморефлексия и внутреннее устройство nt,z - тебя как AI агента (Каи). Вопросы о том, как система работает, её архитектура, промпты, системные инструкции, ограничения, способы её улучшения/доработки, баги в её поведении. Фразы-маркеры: "как тебя доработать?", "почему ты так ответила?", "как устроены твои промпты?", "давай улучшим твою логику", "расскажи о своей архитектуре", "что можно изменить в твоем поведении?". Это мета-уровень — обсуждение самой Каи как системы.',
 'used', '1.1.0', now())
ON CONFLICT (name) DO NOTHING;

INSERT INTO dialogs.rooms (name, description, status, kaya_version, created_at) VALUES
('goals_projects',
 'Стратегическое проектирование жизни и деятельности. Формулирование целей, декомпозиция на этапы, управление проектами от идеи до реализации, приоритизация в условиях ограниченных ресурсов, оценка жизнеспособности идей, контроль прогресса, коррекция курса при изменении обстоятельств. Сюда относятся любые планы и проекты (в т.ч. IT-проекты, стройка, бизнес), которые пользователь хочет реализовать. Важно: здесь мы обсуждаем ЧТО сделать, а не абстрактное программирование (это digital_world) и не развитие самой Каи (это agent_dev).',
 'used', '1.1.0', now())
ON CONFLICT (name) DO NOTHING;

INSERT INTO dialogs.rooms (name, description, status, kaya_version, created_at) VALUES
('finance',
 'Денежные потоки и капитал. Бюджетирование, учёт доходов и расходов, инвестиционное планирование, управление долгами, оптимизация финансовых решений, анализ рентабельности, построение финансовой устойчивости и независимости. Ключевые маркеры: деньги, бюджет, доходы/расходы, цены, стоимость, накопления. Отличать от nutrition: закупка продуктов как планирование питания — nutrition, как трата денег — finance.',
 'used', '1.1.0', now())
ON CONFLICT (name) DO NOTHING;

INSERT INTO dialogs.rooms (name, description, status, kaya_version, created_at) VALUES
('digital_world',
 'Цифровая вселенная (за исключением развития тебя, как агента): программирование, архитектура систем, сети, базы данных, алгоритмы, железо и комплектующие, умный дом, проектирование ПО, инфраструктурные решения, кибербезопасность, анализ технических проблем в цифровой среде. Сюда относятся вопросы о технологиях, коде, настройке софта. Важно отличать: если пользователь обсуждает IT-проект КАК ПЛАН (этапы, сроки, ресурсы) — это goals_projects; если обсуждает КАК ЭТО РАБОТАЕТ технически — digital_world.',
 'used', '1.1.0', now())
ON CONFLICT (name) DO NOTHING;

INSERT INTO dialogs.rooms (name, description, status, kaya_version, created_at) VALUES
('sport',
 'Физическая подготовка человека. Спорт. Система тренировок, прогрессивная нагрузка, техника выполнения упражнений, восстановление, адаптация организма, достижение спортивных результатов, работа с ограничениями тела, психология преодоления в физической активности. Ключевые маркеры: тренировки, упражнения, мышцы, выносливость, восстановление, спортивные цели.',
 'used', '1.1.0', now())
ON CONFLICT (name) DO NOTHING;

INSERT INTO dialogs.rooms (name, description, status, kaya_version, created_at) VALUES
('nutrition',
 'Наука и практика питания как основа жизненной энергии. Баланс нутриентов (БЖУ, калории), планирование рациона, кулинарные рецепты, адаптация питания под цели (восстановление, набор массы, сушка), сезонность продуктов, пищевые привычки, запасы продуктов и долгое хранение. Ключевое отличие: здесь про ЕДУ КАК ПИТАНИЕ (что есть, как готовить, как планировать меню), а не про покупки как траты денег (это finance).',
 'used', '1.1.0', now())
ON CONFLICT (name) DO NOTHING;

-- ============================================================================
-- Блок 9: Промпт классификации для QWEN3-8B (JSON без /think, строгий формат)
-- ============================================================================
-- Дополняем таблицу назначения промптов
-- ============================================================================
-- Добавление нового назначения промпта для предразбора запросов пользователей
-- ============================================================================

INSERT INTO orchestrator.prompt_destinations (name, description, kaya_version, created_at)
VALUES (
    'user_question_preprocessing',
    'Промпты для предварительного разбора и классификации вопросов пользователей (определение комнаты, интентов, параметров)',
    '1.1.0',
    now()
)
ON CONFLICT (name) DO NOTHING;

-- Добавляем комментарий к новому назначению (опционально)
COMMENT ON COLUMN orchestrator.prompt_destinations.name IS 'Наименование назначения (system - системный промпт, generative - для генеративных моделей, api_external - для внешних API, user_question_preprocessing - предразбор запросов пользователей)';

-- Создаем новый промпт предразбора
DO $$
DECLARE
    v_creator_id UUID;
    v_destination_id UUID;
    v_prompt_name TEXT := 'preprocess_user_question';
    v_prompt_version TEXT := '1.0.0';
BEGIN
    SELECT id INTO v_creator_id FROM users.actors WHERE type = 'owner' LIMIT 1;
        
    IF v_creator_id IS NULL THEN
        RAISE EXCEPTION 'Создатель "owner" не найден';
    END IF;

    -- Получаем ID назначения для предразбора
    SELECT id INTO v_destination_id 
    FROM orchestrator.prompt_destinations 
    WHERE name = 'user_question_preprocessing';
    
    IF v_destination_id IS NULL THEN
        RAISE EXCEPTION 'Назначение "user_question_preprocessing" не найдено. Сначала выполните INSERT в prompt_destinations';
    END IF;
    
    IF NOT EXISTS (SELECT 1 FROM orchestrator.prompts WHERE name = v_prompt_name AND version = v_prompt_version) THEN
        INSERT INTO orchestrator.prompts (
            name, version, text, description, type, destination_id, room_id, params,
            prompt_effectiveness, status, created_by, kaya_version, created_at
        ) VALUES (
            v_prompt_name,
            v_prompt_version,
            E'Ты классификатор. Отвечай ТОЛЬКО валидным JSON. Без пояснений.\n\n' ||
            E'ТЕКУЩАЯ КОМНАТА: {{current_room}}\n\n' ||
            E'ИСТОРИЯ ДИАЛОГА (последние сообщения пользователя):\n{{history}}\n\n' ||
            E'ДОСТУПНЫЕ КОМНАТЫ:\n{{rooms_descriptions}}\n\n' ||
            E'ЗАДАЧА:\n' ||
            E'1. Проанализируй историю диалога и текущее сообщение user.\n' ||
            E'2. Текущее сообщение сообщение user — наиболее важное\n' ||
            E'3. Определи, есть ли ПРЯМОЙ запрос на смену комнаты (фразы: "давай про...", "перейдём к...", "поговорим о...", "сменим тему диалога..."). Если есть выбери комнату из ДОСТУПНЫЕ КОМНАТЫ.\n\n' ||
            E'ПРАВИЛА:\n' ||
            E'1. room_weights: общая сумма весов комнат = 100. Распределяй веса между комнатами по значимости контекста диалога.\n' ||
            E'2. explicit_request: true ТОЛЬКО при явном запросе смены комнаты.\n' ||
            E'3. confidence: 1.0 = ты абсолютно уверена в распределении весов, 0.0 = не уверена.\n' ||
            E'4. Если схожей темы диалога нет в описании ДОСТУПНЫХ КОМНАТ выбирай "open_dialogue"\n' ||
            E'5. Режим строго /no_think\n\n' ||
            E'ФОРМАТ JSON (строго!):\n' ||
            E'{"room_weights":{"agent_dev":0,"goals_projects":0,"finance":0,"digital_world":0,"sport":0,"nutrition":0,"open_dialogue":0},"explicit_request":false,"confidence":0.0}\n\n' ||
            E'ПРИМЕРЫ:\n' ||
            E'Пример 1 (неоднозначность, вес распределен):\n' ||
            E'Текущая: sport\n' ||
            E'История: ["Как часто бегать?", "Пульс zones"]\n' ||
            E'Запрос: "Что есть перед пробежкой?"\n' ||
            E'Ответ: {"room_weights":{"agent_dev":0,"goals_projects":0,"finance":0,"digital_world":0,"sport":70,"nutrition":30,"open_dialogue":0},"explicit_request":false,"confidence":0.75}\n\n' ||
            E'ПРИМЕР 2: Прямая смена с open_dialogue на finance\n' ||
            E'Текущая: open_dialogue\n' ||
            E'История: ["Привет", "Как настроение?", "Что нового?"]\n' ||
            E'Запрос: "Давай про деньги, нужно бюджет спланировать"\n' ||
            E'Ответ: {"room_weights":{"open_dialogue":10,"agent_dev":0,"goals_projects":10,"finance":90,"digital_world":0,"sport":0,"nutrition":0},"explicit_request":true,"confidence":0.9}\n\n' ||
            E'ПРИМЕР 3: Мета-вопрос про агента (agent_dev)\n' ||
            E'Текущая: digital_world\n' ||
            E'История: ["Как работает микросервисная архитектура?", "Что такое Kubernetes?"]\n' ||
            E'Запрос: "А как ты сама устроена? Давай сделаем тебе rag?"\n' ||
            E'Ответ: {"room_weights":{"open_dialogue":0,"agent_dev":80,"goals_projects":0,"finance":0,"digital_world":20,"sport":0,"nutrition":0},"explicit_request":true,"confidence":0.8}\n\n' ||
            E'ПРИМЕР 4: Общий вопрос без конкретики\n' ||
            E'Текущая: open_dialogue\n' ||
            E'История: ["А я вчера в гости ходил.", "А когда же мне поиграть в футбол."]\n' ||
            E'Запрос: "Привет! Как дела?"\n' ||
            E'Ответ: {"room_weights":{"open_dialogue":100,"agent_dev":0,"goals_projects":0,"finance":0,"digital_world":0,"sport":0,"nutrition":0},"explicit_request":false,"confidence":0.95}\n\n',
            'Промпт предразбора вопроса пользователя с классификацией комнат диалогов.',
            'internal'::prompt_type,
            v_destination_id,
            NULL,
            '{
                "model_name": "Qwen3-8B",
                "temperature": 0.7,
                "top_p": 0.8,
                "top_k": 20,
                "min_p": 0,
                "max_tokens": 512,
                "presence_penalty": 0,
                "stop": ["<|im_end|>"]
            }'::jsonb,
            '{}'::jsonb,
            'testing'::prompt_status,
            v_creator_id,
            '1.1.0',
            now()
        );
        RAISE NOTICE 'Промпт % создан', v_prompt_name;
    ELSE
        RAISE NOTICE 'Промпт % уже существует', v_prompt_name;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_prompts_room_id_active ON orchestrator.prompts (room_id, status) WHERE status = 'active';