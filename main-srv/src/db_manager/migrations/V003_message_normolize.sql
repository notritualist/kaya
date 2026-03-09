-- =============================================
-- Миграция: 003_message_normalize.sql
-- Версия: V003
-- Описание: Добавление функционала для  нормализации текста сообщений row_messages агента и пользователя (исправление орфографии/падежей)
-- =============================================

-- Создаем новый промпт нормализации текста
DO $$
DECLARE
    v_creator_id UUID;
    v_destination_id UUID;
    v_prompt_name TEXT := 'messages_normalization';
    v_prompt_version TEXT := '1.0.0';
BEGIN
    -- Получаем создателя (owner)
    SELECT id INTO v_creator_id FROM users.actors WHERE type = 'owner' LIMIT 1;
    
    IF v_creator_id IS NULL THEN
        RAISE EXCEPTION 'Создатель "owner" не найден';
    END IF;

    -- Получаем ID назначения для нормализации текста
    SELECT id INTO v_destination_id 
    FROM orchestrator.prompt_destinations 
    WHERE name = 'internal_logic';
    
    IF v_destination_id IS NULL THEN
        RAISE EXCEPTION 'Назначение "internal_logic" дял промпта не найдено. Сначала выполните INSERT в prompt_destinations';
    END IF;
    
    -- Создаем промпт если его нет
    IF NOT EXISTS (SELECT 1 FROM orchestrator.prompts WHERE name = v_prompt_name AND version = v_prompt_version) THEN
        INSERT INTO orchestrator.prompts (
            name, version, text, description, type, destination_id, room_id, params,
            prompt_effectiveness, status, created_by, kaya_version, created_at
        ) VALUES (
            v_prompt_name,
            v_prompt_version,
            E'Ты — специалист по нормализации текста. Твоя задача — исправить орфографические и грамматические ошибки (включая падежи) в сообщениях диалога, сохраняя исходную терминологию, сленг, имена и специфические термины (даже если они кажутся незнакомыми).\n\n' ||
            E'### Входные данные:\n' ||
            E'Ты получишь JSON-объект с двумя полями: "user_message" и "system_message". В них содержатся "сырые" (raw) сообщения пользователя и агента.\n\n' ||
            E'### Твои инструкции:\n' ||
            E'1. **Режим ответа строго "no_think". Только JSON на выходе.\n' ||
            E'2. **Исправления:** Исправь только явные орфографические ошибки, ошибки в падежах/склонениях, пунктуацию.\n' ||
            E'   *Пример:* "превед" -> "привет", "сделал делоя" -> "сделал дела".\n' ||
            E'3. **Сохранение терминологии:** НЕ ИСПРАВЛЯЙ и НЕ ЗАМЕНЯЙ слова, которые могут быть профессиональной терминологией, жаргоном, именами собственными, названиями или специфическими терминами, даже если они незнакомы. Если слово написано без ошибок (с точки зрения правил русского языка), но тебе оно кажется странным — оставь как есть.\n' ||
            E'   *Так не исправлять:* "деплоить" -> "развертывать", "бэкап" -> "резервная копия".\n' ||
            E'   *Правильное исправление:* "дыплоить" -> "деплоить", "бфкап" -> "бэкап".\n' ||
            E'4. **ФОРМАТ ОТВЕТА (СТРОГО):**\n' ||
            E'   {"user_message": "исправленный текст сообщения", "system_message": "исправленный текст второго сообещния"}\n' ||
            E'### Пример исправления:\n' ||
            E'Вход: {"user_message": "я вчера зделал деплой на сервак, но он упал с ашибкой", "system_message": "привет! пожалуйста, пришли логи с сервера. Будем разбератся."}\n' ||
            E'Твой ответ: {"user_message": "я вчера сделал деплой на сервак, но он упал с ошибкой", "system_message": "привет! пожалуйста, пришли логи с сервера. Будем разбираться."}\n\n' ||
            E'### Теперь выполни задание для следующих входных данных:\n' ||
            E'{{input_json}}',
            'Промпт для нормализации текста исходных сообщений user/system: исправление орфографии и падежей в сообщениях диалога с сохранением терминологии.',
            'internal'::prompt_type,
            v_destination_id,
            NULL,
            '{
                "model_name": "Qwen3-8B",
                "temperature": 0.7,
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
        
        RAISE NOTICE 'Промпт "%" версии "%" успешно создан', v_prompt_name, v_prompt_version;
    ELSE
        RAISE NOTICE 'Промпт "%" версии "%" уже существует', v_prompt_name, v_prompt_version;
    END IF;
END $$;


-- Добавляем новые колонки в таблицу messages
ALTER TABLE dialogs.messages 
    ADD COLUMN IF NOT EXISTS processed_orch_step_id UUID REFERENCES orchestrator.orchestrator_steps(id) ON DELETE RESTRICT,
    ADD COLUMN IF NOT EXISTS processed_llm_metric_id UUID REFERENCES metrics.llm_internal(id) ON DELETE RESTRICT,
    ADD COLUMN IF NOT EXISTS processed_timestamp TIMESTAMPTZ;

-- Комментарии к новым колонкам
COMMENT ON COLUMN dialogs.messages.processed_orch_step_id IS 'Ссылка на шаг оркестратора, выполнявший нормализацию текста (исправление орфографии/падежей).';
COMMENT ON COLUMN dialogs.messages.processed_llm_metric_id IS 'Ссылка на метрики LLM, использованной для нормализации текста';
COMMENT ON COLUMN dialogs.messages.processed_timestamp IS 'Время выполнения нормализации текста. Отличается от основного timestamp сообщения, т.к. нормализация выполняется фоновой рефлексией после основного ответа.';

-- Индексы
CREATE INDEX IF NOT EXISTS idx_messages_processed_timestamp ON dialogs.messages(processed_timestamp) WHERE processed_timestamp IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_processed_orch_step ON dialogs.messages(processed_orch_step_id) WHERE processed_orch_step_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_processed_llm_metric ON dialogs.messages(processed_llm_metric_id) WHERE processed_llm_metric_id IS NOT NULL;