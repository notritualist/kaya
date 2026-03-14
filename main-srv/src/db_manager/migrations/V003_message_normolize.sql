-- =============================================
-- Миграция: V003_messages_processing.sql
-- Версия: V003
-- Описание: Фоновая обработка сообщений — нормализация текста + реклассификация комнат
-- =============================================

-- =============================================================================
-- БЛОК 1: Типы задач оркестратора
-- =============================================================================
INSERT INTO orchestrator.task_types (type_name, description)
VALUES
    ('messages_normalization', 'Фоновая нормализация текста сообщений (орфография/падежи/чистка)'),
    ('message_room_reclassification', 'Фоновая реклассификация сообщений по тематическим комнатам')
ON CONFLICT (type_name) DO NOTHING;

-- =============================================================================
-- БЛОК 2: Типы шагов оркестратора
-- =============================================================================
INSERT INTO orchestrator.step_types (step_name, description, kaya_version)
VALUES
    ('messages_normalization', 'Нормализация текста пары сообщений user/system', '1.1.0'),
    ('message_room_reclassification', 'Классификация пары сообщений по комнате диалога', '1.1.0')
ON CONFLICT (step_name) DO NOTHING;

-- =============================================================================
-- БЛОК 3: Промпт нормализации текста
-- =============================================================================
DO $$
DECLARE
    v_creator_id UUID;
    v_destination_id UUID;
    v_prompt_name TEXT := 'messages_normalization';
    v_prompt_version TEXT := '1.0.0';
BEGIN
    SELECT id INTO v_creator_id FROM users.actors WHERE type = 'owner' LIMIT 1;
    IF v_creator_id IS NULL THEN RAISE EXCEPTION 'Создатель "owner" не найден'; END IF;

    SELECT id INTO v_destination_id FROM orchestrator.prompt_destinations WHERE name = 'internal_logic';
    IF v_destination_id IS NULL THEN RAISE EXCEPTION 'Назначение "internal_logic" не найдено'; END IF;

    IF NOT EXISTS (SELECT 1 FROM orchestrator.prompts WHERE name = v_prompt_name AND version = v_prompt_version) THEN
        INSERT INTO orchestrator.prompts (
            name, version, text, description, type, destination_id, room_id, params,
            prompt_effectiveness, status, created_by, kaya_version, created_at
        ) VALUES (
            v_prompt_name, v_prompt_version,
E'Ты — специалист по нормализации текста. Твоя задача — исправить орфографические и грамматические ошибки (включая падежи) в сообщениях диалога, сохраняя исходную терминологию, сленг, имена и специфические термины.

РЕЖИМ ОТВЕТА (СТРОГО):
1. /no_think (никаких рассуждений, вступлений или пояснений).
2. ТОЛЬКО JSON: На выходе должен быть валидный JSON-объект.

ИНСТРУКЦИИ:
1. Исправления: Исправь только явные орфографические ошибки, ошибки в падежах/склонениях, пунктуацию, лишние пробелы и табуляции.
   Пример:"превед" -> "привет", "сделал делоя" -> "сделал дела".
2. Чистка: Удали все эмодзи и смайлики (например: 😊, 😀, 🔥, ❤️, 😂 и любые другие). Текст должен быть полностью очищен от любых эмодзи/смайликов.
3. Удаляй повторения знаков препинания ("???" -> "?"). Восстанавливай заглавные буквы в предложениях по смыслу.
4. Сохранение терминологии (КРИТИЧНО): НЕ ИСПРАВЛЯЙ и НЕ ЗАМЕНЯЙ слова, которые могут быть профессиональной терминологией, жаргоном, именами собственными, названиями или специфическими терминами.
   Так не исправлять: "деплоить" -> "развертывать", "бэкап" -> "резервная копия".
   Правильное исправление: "дыплоить" -> "деплоить", "бфкап" -> "бэкап".

ФОРМАТ ОТВЕТА (СТРОГО):
{
  "user_message": "исправленный текст сообщения пользователя",
  "system_message": "исправленный текст сообщения агента"
}

ПРИМЕР:
Вход: {"user_message": "я вчера зделал деплой на сервак, но он упал с ашибкой 😀😀", "system_message": "привет! пожалуйста, пришли логи с сервера. Будем разбератся."}
Ответ: {"user_message": "Я вчера сделал деплой на сервак, но он упал с ошибкой", "system_message": "Привет! Пожалуйста, пришли логи с сервера. Будем разбираться."}

ТЕКУЩЕЕ ЗАДАНИЕ:
{{input_json}}',
            'Промпт для нормализации текста сообщений: исправление орфографии, падежей, чистка эмодзи.',
            'internal'::prompt_type,
            v_destination_id,
            NULL,
            '{
                "model_name": "Qwen3-8B",
                "temperature": 0.5,
                "top_p": 0.8,
                "top_k": 20,
                "min_p": 0,
                "max_tokens": 4096,
                "presence_penalty": 0,
                "stop": ["<|im_end|>"]
            }'::jsonb,
            '{}'::jsonb,
            'testing'::prompt_status,
            v_creator_id,
            '1.1.0',
            now()
        );
        RAISE NOTICE 'Промпт "%s" версии "%s" создан', v_prompt_name, v_prompt_version;
    ELSE
        RAISE NOTICE 'Промпт "%s" версии "%s" уже существует', v_prompt_name, v_prompt_version;
    END IF;
END $$;

-- =============================================================================
-- БЛОК 4: Промпт реклассификации комнат
-- =============================================================================
DO $$
DECLARE
    v_creator_id UUID;
    v_destination_id UUID;
    v_prompt_name TEXT := 'room_reclassification';
    v_prompt_version TEXT := '1.0.0';
BEGIN
    SELECT id INTO v_creator_id FROM users.actors WHERE type = 'owner' LIMIT 1;
    IF v_creator_id IS NULL THEN RAISE EXCEPTION 'Создатель "owner" не найден'; END IF;

    SELECT id INTO v_destination_id FROM orchestrator.prompt_destinations WHERE name = 'internal_logic';
    IF v_destination_id IS NULL THEN RAISE EXCEPTION 'Назначение "internal_logic" не найдено'; END IF;

    IF NOT EXISTS (SELECT 1 FROM orchestrator.prompts WHERE name = v_prompt_name AND version = v_prompt_version) THEN
        INSERT INTO orchestrator.prompts (
            name, version, text, description, type, destination_id, room_id, params,
            prompt_effectiveness, status, created_by, kaya_version, created_at
        ) VALUES (
            v_prompt_name, v_prompt_version,
E'Ты — классификатор диалогов по тематическим комнатам. Твоя задача — определить, к какой комнате относится пара сообщений (вопрос пользователя + ответ агента).

КОНТЕКСТ:
Текущая комната: {{current_room_name}}

ДОСТУПНЫЕ КОМНАТЫ:
{{rooms_descriptions}}

ПАРА СООБЩЕНИЙ ДЛЯ КЛАССИФИКАЦИИ:
Вопрос пользователя:
{{user_message}}

Ответ агента:
{{system_message}}

ИНСТРУКЦИИ:
1. Проанализируй ВОПРОС пользователя — он определяет тему диалога.
2. Ответ агента помогает уточнить, но не является определяющим.
3. Выбери ОДНУ комнату, которая лучше всего соответствует теме диалога.
4. Если тема не соответствует ни одной комнате — оставь "current_room_name".
5. Оцени свою уверенность (0.0 - не уверена, 1.0 - абсолютно уверена).
6. Режим строго no_think — только JSON на выходе.

ФОРМАТ ОТВЕТА (СТРОГО):
{"selected_room": "название_комнаты", "confidence": 0.0}

ПРИМЕРЫ:
Пример 1 (программирование):
Вопрос: "Как мне написать функцию на питоне для парсинга логов?"
Ответ: "Вот пример функции с использованием библиотеки re..."
Ответ: {"selected_room": "digital_world", "confidence": 0.95}

Пример 2 (финансы):
Вопрос: "Как мне распределить бюджет на следующий месяц?"
Ответ: "Рекомендую разделить расходы на категории: еда, жилье, транспорт..."
Ответ: {"selected_room": "finance", "confidence": 0.9}

Пример 3 (общий диалог):
Вопрос: "Как у тебя дела?"
Ответ: "Спасибо, всё хорошо! А у тебя как?"
Ответ: {"selected_room": "open_dialogue", "confidence": 0.98}

ТЕКУЩЕЕ ЗАДАНИЕ:
Текущая комната: {{current_room_name}}
Доступные комнаты: {{rooms_descriptions}}
Вопрос: {{user_message}}
Ответ: {{system_message}}',
            'Промпт для реклассификации пар сообщений (вопрос-ответ) по тематическим комнатам диалога.',
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
        RAISE NOTICE 'Промпт "%s" версии "%s" создан', v_prompt_name, v_prompt_version;
    ELSE
        RAISE NOTICE 'Промпт "%s" версии "%s" уже существует', v_prompt_name, v_prompt_version;
    END IF;
END $$;

-- =============================================================================
-- БЛОК 5: Колонки в таблице messages для нормализации
-- =============================================================================
ALTER TABLE dialogs.messages
ADD COLUMN IF NOT EXISTS processed_orch_step_id UUID REFERENCES orchestrator.orchestrator_steps(id) ON DELETE RESTRICT,
ADD COLUMN IF NOT EXISTS processed_llm_metric_id UUID REFERENCES metrics.llm_internal(id) ON DELETE RESTRICT,
ADD COLUMN IF NOT EXISTS processed_timestamp TIMESTAMPTZ,
ADD COLUMN IF NOT EXISTS effective_room_id UUID REFERENCES dialogs.rooms(id) ON DELETE SET NULL,
ADD COLUMN IF NOT EXISTS reclassification_step_id UUID REFERENCES orchestrator.orchestrator_steps(id) ON DELETE RESTRICT,
ADD COLUMN IF NOT EXISTS effective_room_updated_at TIMESTAMPTZ;

-- Комментарии
COMMENT ON COLUMN dialogs.messages.processed_text IS 'Нормализованный текст сообщения (орфография, падежи, чистка эмодзи).';
COMMENT ON COLUMN dialogs.messages.processed_orch_step_id IS 'Ссылка на шаг оркестратора, выполнявший нормализацию текста.';
COMMENT ON COLUMN dialogs.messages.processed_llm_metric_id IS 'Ссылка на метрики LLM, использованной для нормализации текста.';
COMMENT ON COLUMN dialogs.messages.processed_timestamp IS 'Время выполнения нормализации текста (фоновая операция).';
COMMENT ON COLUMN dialogs.messages.effective_room_id IS 'Эффективная комната после классификации моделью (может отличаться от физической room_id).';
COMMENT ON COLUMN dialogs.messages.reclassification_step_id IS 'Ссылка на шаг оркестратора, выполнявший реклассификацию комнаты.';
COMMENT ON COLUMN dialogs.messages.effective_room_updated_at IS 'Время обновления effective_room_id.';

-- Индексы
CREATE INDEX IF NOT EXISTS idx_messages_processed_timestamp ON dialogs.messages(processed_timestamp) WHERE processed_timestamp IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_processed_orch_step ON dialogs.messages(processed_orch_step_id) WHERE processed_orch_step_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_processed_llm_metric ON dialogs.messages(processed_llm_metric_id) WHERE processed_llm_metric_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_effective_room ON dialogs.messages(effective_room_id) WHERE effective_room_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_reclassification_step ON dialogs.messages(reclassification_step_id) WHERE reclassification_step_id IS NOT NULL;