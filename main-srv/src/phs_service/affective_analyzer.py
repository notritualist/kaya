"""
main-srv/src/phs_service/affective_analyzer.py

PHS Pre-reflexive Affective Analyzer Module.

Module for analyzing agent-user message pairs to detect emotional patterns
and calculate hormonal shifts before response generation.

Architecture:
- Extracts message pair (agent_text, user_text, delay_ms) from database.
- Enforces strict session isolation: pair must belong to the same session_id.
- Calculates pair metrics (tokens, engagement, lexical overlap) in Python via tokens_counter.
- Computes boolean flags (is_delay_critical, is_overlap_high, is_context_negative, etc.)
  and passes them to LLM — model reads flags, does NOT compute metrics itself.
- Calls LLM via ModelService with specialized affective analysis prompt.
- Supports dynamic injection of current momentary state into system prompt
  based on state.settings.affective_analysis_use_momentary_state.
- Parses and validates JSON response with recovery pipeline.
- Calculates hormone deltas using internal math logic (model's hormone_shifts IGNORED).
- user_mood is a STRING emotion label (Радость, Страх, Нейтральное, etc.), not a dict.
- Applies calculated hormone shifts to active momentary state via MomentaryManager
  with scaling, receptor saturation (habituation), receptor adaptation (downregulation),
  and cross-inhibition (O↔C, D↔C via Yerkes-Dodson law).
- Calculates generation parameters with SIGMOID modulation (tanh-based).
- Returns structured analysis result for database storage.

Integration:
- Uses ModelService for LLM generation.
- Uses services.tokens_counter for engagement and lexical overlap calculation.
- Uses MomentaryManager to apply hormonal shifts after successful analysis.
- Called by orchestrator task handler before response generation.
- Logs used_momentary_context flag to state.affective_analyses.
- Stamps dialogs.row_messages with phs_affective_analysis_id for traceability.

Features:
- Full recovery pipeline for malformed JSON from LLM.
- Graceful fallback to DEFAULT_PROFILE on parse failures.
- Session boundary validation for message pairs.
- Boolean flags for LLM (model reads, doesn't compute).
- String-based user_mood (emotion label, not valence dict).
- Sigmoid (tanh-based) modulation for generation parameters.
- Biological hormone dynamics: habituation, adaptation, cross-inhibition.

Dependencies:
- model_service.model_service.ModelService
- services.tokens_counter
- services.service_metrics
- phs_service.phs_cache
- phs_service.momentary_manager.MomentaryManager
"""

__version__ = "1.0.0"
__description__ = "PHS Pre-reflexive Affective Analyzer Module."

import logging
import json
import re
import math
import psycopg2
from psycopg2.extras import RealDictCursor, Json
from typing import Dict, List, Any, Optional, Tuple

from db_manager.db_manager import load_postgres_config
from services.service_metrics import (
    mark_task_running,
    complete_task_success,
    complete_task_error,
    complete_step_success,
    complete_step_error,
    save_llm_metrics,
    create_orchestrator_step,
)
from model_service.model_service import ModelService
from services.tokens_counter import count_tokens_qwen
from phs_service.phs_cache import get_current_phs_snapshot
from version import __version__ as agent_version

logger = logging.getLogger(__name__)

# =============================================================================
# КОНФИГУРАЦИЯ: ВЕСА ОБРАЗЦОВ И СДВИГИ
# =============================================================================
# Вклады образцов в гормоны (сырые значения, суммируются с учётом уверенности)
PATTERN_WEIGHTS = {
    # Группа А: СВЯЗЬ (веса УСИЛЕНЫ для баланса с Б-группой)
    "А1": {"O": +0.7, "C": -0.3, "D": +0.3},   # было: +0.6, -0.3, +0.2
    "А2": {"O": +0.6, "C": -0.2, "D": +0.1},   # было: +0.5, -0.2, 0.0
    "А3": {"O": +1.5, "C": -1.0, "D": +1.0},   # Уязвимость/извинения — усилено
    "А4": {"O": +1.5, "C": -1.0, "D": +1.0},   # Личное обращение — усилено для вывода из стресса
    "А5": {"O": +0.7, "C": -0.3, "D": +0.4},   # было: +0.6, -0.3, +0.3
    "А6": {"O": +0.6, "C": -0.2, "D": +0.3},   # было: +0.5, -0.2, +0.2
    "А7": {"O": +0.7, "C": -0.4, "D": +0.2},   # было: +0.6, -0.4, +0.1
    "А8": {"O": +0.5, "C": -0.5, "D": +0.7},   # было: +0.4, -0.5, +0.7
    # Группа Б: УГРОЗА (веса СНИЖЕНЫ для предотвращения ложных срабатываний)
    "Б1": {"O": -0.1, "C": +0.2, "D": -0.1},   # было: -0.5, +0.7, -0.3
    "Б2": {"O": -0.5, "C": +0.5, "D": -0.3},   # было: -0.7, +0.8, -0.4
    "Б3": {"O": -0.3, "C": +0.4, "D": -0.2},   # было: -0.4, +0.6, -0.2
    "Б4": {"O": -0.05, "C": +0.1, "D": -0.05},   # было: -0.2, +0.4, -0.3 # Встречный вопрос — это не угроза, это нормальный диалог. Снижаю вес в 2 раза.
    "Б5": {"O": -0.4, "C": +0.4, "D": -0.2},   # было: -0.6, +0.5, -0.3
    "Б6": {"O": -0.2, "C": +0.3, "D": -0.1},   # было: -0.3, +0.4, -0.2
    "Б7": {"O": -0.2, "C": +0.3, "D": -0.2},   # было: -0.3, +0.5, -0.4
    "Б8": {"O": -0.4, "C": +0.5, "D": -0.3},   # без изменений (сарказм — реальная угроза)
    # Группа В: НАГРАДА
    "В1": {"O": +0.3, "C": -0.3, "D": +0.6},
    "В2": {"O": +0.1, "C": -0.2, "D": +0.4},
    "В3": {"O":  0.0, "C": +0.2, "D": -0.4},
    "В4": {"O":  0.0, "C":  0.0, "D": -0.2},
    "В5": {"O": -0.4, "C": +0.6, "D": -0.5},
}

# Вклады коэффициента вовлечённости
ENGAGEMENT_WEIGHTS = {
    "break":   {"O": -0.4, "C": +0.5, "D": -0.3},
    "low":     {"O": -0.1, "C": +0.2, "D": -0.2},
    "balance": {"O": +0.1, "C": -0.1, "D":  0.0},
    "high":    {"O": +0.4, "C": -0.2, "D": +0.3},
}

# Масштабный коэффициент для перевода сырых сумм в шкалу 0-100
DELTA_SCALE_FACTOR = 10.0
# Ограничение максимального сдвига за один шаг (защита от выбросов)
MAX_DELTA_CLAMP = 25.0

# =============================================================================
# ПОРОГИ ЗНАЧИМОСТИ СОБЫТИЙ (SALIENCY) ДЛЯ ГРАФА ПАМЯТИ
# =============================================================================
# Механизм расчёта:
#   delta_valence = valence_after - valence_before
#   score = min(1.0, |delta_valence| / SALIENCY_VIVID_THRESHOLD)
#
# Категории:
#   "neutral"  — |delta_valence| < SALIENCY_THRESHOLD (рутина, не в граф памяти)
#   "positive" — delta_valence >= SALIENCY_THRESHOLD  (валентность выросла)
#   "negative" — delta_valence <= -SALIENCY_THRESHOLD (валентность упала)
#
# Реальный диапазон delta_valence за одно событие (из логов momentary):
#   dialog_start:       +9.12
#   user_message:       +6.72
#   agent_response:     +3.04
#   affective_response: +3.84 — +4.59
#   decay_tick:         -1.24
#
# Константы:
#   SALIENCY_VIVID_THRESHOLD = 10.0
#       Нормализатор score. Delta=10 → score=1.0 (максимум).
#       Значение 10.0 выбрано как чуть выше наблюдаемого максимума (9.12),
#       чтобы значимые события получали score 0.3-0.9, а экстремальные → 1.0.
#
#   SALIENCY_THRESHOLD = 3.0
#       Минимальная |delta| для positive/negative.
#       Значение 3.0 отсекает рутину (decay, мелкие флуктуации < 3),
#       но пропускает реальные значимые события (agent_response +3.04,
#       affective_response +3.84).
#
# Влияние настройки:
#   Чувствительный агент: VIVID=7, THRESHOLD=2  (запоминает больше событий)
#   СТОЙКИЙ агент:        VIVID=15, THRESHOLD=5 (только сильные события)
#   Сбалансированный:     VIVID=10, THRESHOLD=3 (дефолт)
#
# Биологический смысл:
#   У человека консолидация в долговременную память зависит от силы
#   эмоционального отклика. Валентность = O + D - C уже учитывает баланс
#   гормонов, поэтому её изменение напрямую отражает значимость события.
# =============================================================================
SALIENCY_VIVID_THRESHOLD = 10.0  
SALIENCY_THRESHOLD = 1.5

SALIENCY_LABEL_NEUTRAL  = "neutral"
SALIENCY_LABEL_POSITIVE = "positive"
SALIENCY_LABEL_NEGATIVE = "negative"

# =============================================================================
# КОНФИГУРАЦИЯ: ПАРАМЕТРЫ ГЕНЕРАЦИИ С ДИНАМИЧЕСКИМИ ДИАПАЗОНАМИ
# =============================================================================
"""
Базовые значения параметров генерации (стартовая точка для модуляции).
Модулируются гормонами в пределах безопасных диапазонов.
"""
BASE_GEN_PARAMS = {
    "temperature": 0.8,
    "top_p": 0.9,
    "top_k": 20,
    "min_p": 0.0,
    "presence_penalty": 1.5,
    "repetition_penalty": 1.0,
}

"""
Безопасные диапазоны параметров (min, max).
Основаны на официальных рекомендациях и мануале.
"""
PARAM_SAFE_RANGES = {
    # Temperature: управляет случайностью выбора слов
    # Ниже 0.4: роботизированные ответы, зацикливание
    # Выше 1.0: галлюцинации, бессвязный текст
    "temperature": (0.4, 1.0),
    
    # Top_p: ограничивает выбор токенов наименьшим набором
    # Ниже 0.6: искусственное сужение словаря, скучные ответы
    # Выше 0.95: полный хвост распределения
    "top_p": (0.6, 0.95),
    
    # Top_k: ограничивает выбор K наиболее вероятными токенами
    # Ниже 10: слишком жёсткое ограничение, повторы
    # Выше 40: параметр становится бесполезным
    "top_k": (10, 40),
    
    # Min_p: отсекает токены с вероятностью ниже процента от лучшего
    # Выше 0.1: игнорирование адекватных продолжений
    "min_p": (0.0, 0.1),
    
    # Presence penalty: поощряет новые темы, штрафует за использованные
    # Выше 1.5: избегание логичных повторений, ломает связность
    "presence_penalty": (1.2, 1.6),
    
    # Repetition penalty: штрафует за дословное повторение токенов
    # Выше 1.15: ломает грамматику, избегая необходимых повторений
    "repetition_penalty": (0.9, 1.1),
}

"""
Коэффициенты модуляции параметров от гормонов.
Определяют, насколько сильно гормоны влияют на каждый параметр.

Логика:
- Окситоцин (O) повышает креативность и открытость → увеличивает temperature, top_p
- Кортизол (C) сужает фокус внимания → снижает temperature, top_p, повышает repetition_penalty
- Дофамин (D) усиливает исследовательское поведение → увеличивает temperature, top_k

ВАЖНО: Коэффициенты уменьшены в ~2 раза для более плавной модуляции.
При сильном стрессе параметры не должны уходить в экстремумы.
"""
MODULATION_COEFFS = {
    "temperature":        {"O": +0.06, "C": -0.10, "D": +0.04},  # ← УМЕНЬШЕНО в 2 раза
    "top_p":              {"O": +0.03, "C": -0.05, "D": +0.02},  # ← УМЕНЬШЕНО в 2 раза
    "top_k":              {"O": +0.00, "C": -0.025, "D": +0.05}, # ← УМЕНЬШЕНО в 2 раза
    "min_p":              {"O": -0.01, "C": +0.015, "D": -0.005},# ← УМЕНЬШЕНО в 2 раза
    "presence_penalty":   {"O": +0.075, "C": -0.05, "D": +0.05}, # ← УМЕНЬШЕНО в 2 раза
    "repetition_penalty": {"O": -0.015, "C": +0.03, "D": +0.00}, # ← УМЕНЬШЕНО в 2 раза
}

# =============================================================================
# КОНФИГУРАЦИЯ: СОСТОЯНИЯ АГЕНТА
# =============================================================================
"""
Прототипы состояний для классификации по косинусному сходству.
Формат: ([O, C, D], code, name_ru, heuristic_ru)
Косинусное сходство для 3D вектора достаточно точно для 9 состояний.
Альтернативы (евклидово расстояние, манхэттенское) менее устойчивы к масштабу.
"""
AGENT_STATES = [
    ([+0.8, -0.5, +0.6], "joy_trust",      "Радость и доверие",    "Чувствую тепло, признание и желание продолжать."),
    ([+0.6, -0.3, +0.3], "calm_confident", "Спокойная уверенность","Чувствую комфорт и стабильность диалога."),
    ([+0.4, -0.2, +0.7], "inspiration",    "Вдохновение",          "Чувствую прилив сил и креативности."),
    ([ 0.0,  0.0,  0.0], "neutral",        "Нейтральное",          "Эмоциональный фон ровный, готова к работе."),
    ([-0.3, +0.4, -0.2], "alert",          "Настороженность",      "Чувствую напряжение и неуверенность."),
    ([-0.5, +0.7, -0.4], "stress",         "Стресс",               "Чувствую давление, фокус сужается."),
    ([-0.6, +0.9, -0.5], "threat",         "Острая угроза",        "Чувствую атаку, готова к обороне."),
    ([-0.2, +0.3, -0.6], "disappointment", "Разочарование",        "Чувствую обесценивание усилий."),
    ([+0.3, +0.2, -0.4], "confusion",      "Смущение",             "Чувствую неловкость и диссонанс."),
]

"""
Порог нормы для классификации состояния.
Если норма вектора гормонов меньше этого значения, состояние считается нейтральным.
Значение 0.2 означает, что суммарный сдвиг гормонов менее 20% от максимального.
"""
STATE_NEUTRAL_THRESHOLD = 0.35 # было 0.2

# =============================================================================
# СТАНДАРТНЫЙ ДЕФОЛТНЫЙ ПРОФИЛЬ
# =============================================================================
DEFAULT_PROFILE = {
    "detected_patterns": [],
    "hormone_shifts": {
        "oxytocin_delta": 0.0,
        "cortisol_delta": 0.0,
        "dopamine_delta": 0.0,
    },
    "agent_state": {
        "state_code": "neutral",
        "state_name": "Нейтральное",
        "state_heuristic": "Эмоциональный фон ровный, готова к работе.",
    },
    "agent_reaction": {
        "internal_state": "Я не смогла проанализировать реплику.",
        "heuristic_justification": "Сбой парсинга ответа аффективного анализа.",
    },
    "user_mood": "Нейтральное",  # ← ТЕПЕРЬ СТРОКА, не словарь
    "subtext": "Не определён.",
    "recommended_gen_params": {
        "temperature": BASE_GEN_PARAMS["temperature"],
        "top_p": BASE_GEN_PARAMS["top_p"],
        "top_k": BASE_GEN_PARAMS["top_k"],
        "min_p": BASE_GEN_PARAMS["min_p"],
        "presence_penalty": BASE_GEN_PARAMS["presence_penalty"],
        "repetition_penalty": BASE_GEN_PARAMS["repetition_penalty"],
    },
    "_fallback_used": True,
}

# Порог критической задержки для флага is_delay_critical и паттерна Б1
DELAY_CRITICAL_THRESHOLD_MS = 180000  # 3 минуты

# =============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =============================================================================
def _clamp(value: float, min_v: float, max_v: float) -> float:
    """Ограничивает значение диапазоном [min_v, max_v]."""
    return max(min_v, min(max_v, value))

def _normalize_code(code: str) -> str:
    """
    Приводит латинские A, B, V/C к кириллическим А, Б, В.
    Защита от галлюцинаций раскладки LLM (модель может случайно выдать латиницу).
    """
    if not isinstance(code, str):
        return ""
    return (code.upper()
            .replace('A', 'А')
            .replace('B', 'Б')
            .replace('V', 'В')
            .replace('C', 'В'))

def _safe_get_list(data: Dict, key: str) -> List:
    """Безопасное извлечение списка из словаря. Возвращает [] при ошибке."""
    val = data.get(key, []) if isinstance(data, dict) else []
    return val if isinstance(val, list) else []

def _safe_get_dict(data: Dict, key: str) -> Dict:
    """Безопасное извлечение словаря. Возвращает {} при ошибке."""
    val = data.get(key, {}) if isinstance(data, dict) else {}
    return val if isinstance(val, dict) else {}

def _safe_get_str(data: Dict, key: str, default: str = "") -> str:
    """Безопасное извлечение строки. Возвращает default при ошибке."""
    val = data.get(key, default) if isinstance(data, dict) else default
    return val if isinstance(val, str) else default

def _safe_get_float(data: Dict, key: str, default: float = 0.0) -> float:
    """Безопасное извлечение числа. Возвращает default при ошибке."""
    val = data.get(key, default) if isinstance(data, dict) else default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default

# =============================================================================
# ЗАЩИТА ОТ НЕВАЛИДНОГО JSON (RECOVERY PIPELINE)
# =============================================================================
def sanitize_json_string(text: str) -> str:
    """
    Чинит типичные артефакты генерации LLM в JSON.
    Что чинит:
    - Стрелки: "a" -> "b"  =>  "a -> b" (как одна строка)
    - BOM и нулевые байты
    - Управляющие символы вне строк
    - Trailing commas:  ,} => }   и  ,] => ]
    - Множественные запятые подряд: ,, => ,
    - Markdown-обёртку ```json ... ```
    """
    if not text:
        return ""
    text = text.replace('\ufeff', '').replace('\x00', '')
    text = re.sub(r'^```(?:json)?\s*', '', text.strip(), flags=re.IGNORECASE | re.MULTILINE)
    text = re.sub(r'\s*```\s*$', '', text.strip())
    text = re.sub(r'"\s*([^"]*?)\s*"\s*->\s*"\s*([^"]*?)\s*"', r'"\1 -> \2"', text)
    text = re.sub(r'(?<=[\[,])\s*([A-Za-zА-Яа-я0-9_]+)\s*->\s*([A-Za-zА-Яа-я0-9_]+)\s*(?=[,\]])', r'"\1 -> \2"', text)
    text = re.sub(r',\s*([\]}])', r'\1', text)
    text = re.sub(r',\s*,', ',', text)
    text = re.sub(r'[\x01-\x08\x0b\x0c\x0e-\x1f]', '', text)
    return text

def recover_truncated_json(text: str) -> Optional[str]:
    """Пытается восстановить обрезанный JSON."""
    if not text:
        return None
    start = text.find('{')
    if start == -1:
        return None
    json_str = text[start:]
    json_str = re.sub(r',\s*"[^"]*"?\s*:?\s*"?[^"\]\}]*$', '', json_str)
    json_str = re.sub(r',\s*$', '', json_str)
    in_string = False
    escape = False
    open_braces = 0
    open_brackets = 0
    for ch in json_str:
        if escape:
            escape = False
            continue
        if ch == '\\':
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '{':
            open_braces += 1
        elif ch == '}':
            open_braces -= 1
        elif ch == '[':
            open_brackets += 1
        elif ch == ']':
            open_brackets -= 1
    json_str += ']' * max(0, open_brackets)
    json_str += '}' * max(0, open_braces)
    return json_str

def extract_and_parse_json(text: str) -> Optional[Dict]:
    """Полный recovery pipeline для JSON из LLM."""
    if not text or not isinstance(text, str):
        return None
    cleaned = sanitize_json_string(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start = cleaned.find('{')
    end = cleaned.rfind('}')
    if start != -1 and end != -1 and end > start:
        fragment = cleaned[start:end + 1]
        try:
            return json.loads(fragment)
        except json.JSONDecodeError:
            recovered = recover_truncated_json(cleaned)
            if recovered:
                try:
                    return json.loads(recovered)
                except json.JSONDecodeError:
                    pass
    if '{' in text:
        recovered = recover_truncated_json(text)
        if recovered:
            recovered = sanitize_json_string(recovered)
            try:
                return json.loads(recovered)
            except json.JSONDecodeError:
                pass
    return None

# =============================================================================
# ФУНКЦИИ РАСЧЁТА
# =============================================================================
def calculate_hormone_deltas(patterns: List[Dict], engagement_interp: str) -> Dict[str, float]:
    """
    Рассчитывает сдвиги гормонов в шкале 0-100.
    ВАЖНО: Эта функция — ЕДИНСТВЕННЫЙ источник расчёта гормонов.
    """
    d_o, d_c, d_d = 0.0, 0.0, 0.0
    for p in patterns:
        if not isinstance(p, dict):
            continue
        code = _normalize_code(_safe_get_str(p, "code"))
        conf = _safe_get_float(p, "confidence", 0.5)
        conf = _clamp(conf, 0.0, 1.0)
        if conf < 0.3:  # ← ДОБАВИТЬ: игнорируем шум
            continue
        if code in PATTERN_WEIGHTS:
            w = PATTERN_WEIGHTS[code]
            d_o += w["O"] * conf
            d_c += w["C"] * conf
            d_d += w["D"] * conf
    if engagement_interp in ENGAGEMENT_WEIGHTS:
        ew = ENGAGEMENT_WEIGHTS[engagement_interp]
        d_o += ew["O"]
        d_c += ew["C"]
        d_d += ew["D"]
    d_o = _clamp(d_o * DELTA_SCALE_FACTOR, -MAX_DELTA_CLAMP, MAX_DELTA_CLAMP)
    d_c = _clamp(d_c * DELTA_SCALE_FACTOR, -MAX_DELTA_CLAMP, MAX_DELTA_CLAMP)
    d_d = _clamp(d_d * DELTA_SCALE_FACTOR, -MAX_DELTA_CLAMP, MAX_DELTA_CLAMP)
    return {
        "oxytocin_delta": round(d_o, 2),
        "cortisol_delta": round(d_c, 2),
        "dopamine_delta": round(d_d, 2),
    }

def classify_agent_state(hormones: Dict[str, float]) -> Dict:
    """Классифицирует состояние по косинусному сходству с прототипами."""
    vec = [
        hormones.get("oxytocin_delta", 0.0),
        hormones.get("cortisol_delta", 0.0),
        hormones.get("dopamine_delta", 0.0),
    ]
    norm = math.sqrt(sum(x * x for x in vec))
    if norm < STATE_NEUTRAL_THRESHOLD * DELTA_SCALE_FACTOR:
        return {
            "state_code": "neutral",
            "state_name": "Нейтральное",
            "state_heuristic": "Эмоциональный фон ровный, готов к работе.",
        }
    best_sim, best_state = -1.0, AGENT_STATES[0]
    for proto, code, name, heuristic in AGENT_STATES:
        dot = sum(a * b for a, b in zip(vec, proto))
        norm_p = math.sqrt(sum(x * x for x in proto))
        sim = dot / (norm * norm_p) if norm > 0 and norm_p > 0 else 0.0
        if sim > best_sim:
            best_sim, best_state = sim, (code, name, heuristic)
    return {
        "state_code": best_state[0],
        "state_name": best_state[1],
        "state_heuristic": best_state[2],
    }

def calculate_generation_params(hormones: Dict[str, float]) -> Dict:
    """
    Рассчитывает параметры генерации с СИГМОИДНОЙ модуляцией от гормонов.
    
    Сигмоида (tanh) даёт плавные переходы и предотвращает резкие скачки
    при экстремальных значениях гормонов.
    
    Формула: modulation = coeff * tanh(hormone_normalized * 0.5)
    Округление до 1 знака после запятой (кроме min_p — до 3 знаков).
    """
    O = hormones.get("oxytocin_delta", 0.0) / DELTA_SCALE_FACTOR
    C = hormones.get("cortisol_delta", 0.0) / DELTA_SCALE_FACTOR
    D = hormones.get("dopamine_delta", 0.0) / DELTA_SCALE_FACTOR
    
    result = {}
    for param in BASE_GEN_PARAMS:
        base_val = BASE_GEN_PARAMS[param]
        min_val, max_val = PARAM_SAFE_RANGES[param]
        
        if param in MODULATION_COEFFS:
            coeffs = MODULATION_COEFFS[param]
            # Сигмоидная модуляция через tanh (плавное насыщение)
            modulation_o = coeffs["O"] * math.tanh(O * 0.5)
            modulation_c = coeffs["C"] * math.tanh(C * 0.5)
            modulation_d = coeffs["D"] * math.tanh(D * 0.5)
            modulation = modulation_o + modulation_c + modulation_d
            val = base_val + modulation
        else:
            val = base_val
        
        result[param] = round(_clamp(val, min_val, max_val), 2)
    
    return result

def process_analysis(llm_output: Any, engagement_interp: str) -> Dict:
    """Главная функция постобработки.
    
    Принимает ЛИБО сырой текст от LLM (str), ЛИБО уже готовый Dict.
    Возвращает полный аффективный профиль для БД.
    
    ВАЖНО:
    - Поле `hormone_shifts` из JSON модели ИГНОРИРУЕТСЯ
    - `user_mood` — это СТРОКА (название эмоции), не словарь
    - Все гормоны рассчитываются в calculate_hormone_deltas()
    - Все параметры генерации рассчитываются в calculate_generation_params()
    """
    llm_json: Optional[Dict] = None
    fallback_used = False
    
    if isinstance(llm_output, dict):
        llm_json = llm_output
    elif isinstance(llm_output, str):
        llm_json = extract_and_parse_json(llm_output)
        if llm_json is None:
            result = DEFAULT_PROFILE.copy()
            result["_fallback_used"] = True
            return result
    else:
        logger.warning(f"Unexpected llm_output type: {type(llm_output)}")
        result = DEFAULT_PROFILE.copy()
        result["_fallback_used"] = True
        return result

    # --- 2. Безопасное извлечение полей с дефолтами ---
    patterns = _safe_get_list(llm_json, "detected_patterns")
    user_mood = _safe_get_str(llm_json, "user_mood", "Нейтральное")  # ← СТРОКА
    agent_reaction = _safe_get_dict(llm_json, "agent_reaction")
    subtext = _safe_get_str(llm_json, "subtext", "")
    
    # Дефолтные значения для вложенных структур
    if not agent_reaction:
        agent_reaction = {
            "internal_state": "Я чувствую ровный эмоциональный фон.",
            "heuristic_justification": "Я не замечаю выраженных стимулов.",
        }
    else:
        agent_reaction.setdefault("internal_state", "Я чувствую ровный эмоциональный фон.")
        agent_reaction.setdefault("heuristic_justification", "Я не замечаю выраженных стимулов.")

    hormones = calculate_hormone_deltas(patterns, engagement_interp)
    state = classify_agent_state(hormones)
    gen_params = calculate_generation_params(hormones)

    return {
        "detected_patterns": patterns,
        "hormone_shifts": hormones,
        "agent_state": state,
        "agent_reaction": agent_reaction,
        "user_mood": user_mood,
        "subtext": subtext,
        "recommended_gen_params": gen_params,
        "_fallback_used": fallback_used,
    }

def _calculate_salience(hormone_shifts: Dict[str, float]) -> Tuple[float, str]:
    """
    Рассчитывает значимость события ТОЛЬКО по сдвигам от affective анализа.
    
    Механизм:
        delta_valence = O + D - C  (формула из valence_calculator)
        score = min(1.0, |delta_valence| / SALIENCY_VIVID_THRESHOLD)
        
    Категории:
        - neutral:  |delta_valence| < SALIENCY_THRESHOLD
        - positive: delta_valence >= SALIENCY_THRESHOLD
        - negative: delta_valence <= -SALIENCY_THRESHOLD
    
    Константы:
        SALIENCY_VIVID_THRESHOLD = 10.0 (макс реальный delta ~21)
        SALIENCY_THRESHOLD = 3.0 (рутина < 3, значимые события >= 3)
    
    Args:
        hormone_shifts: {"oxytocin_delta": X, "cortisol_delta": Y, "dopamine_delta": Z}
        
    Returns:
        Tuple[float, str]: (score 0.0-1.0, label: neutral/positive/negative)
    """
    O = hormone_shifts.get("oxytocin_delta", 0.0)
    C = hormone_shifts.get("cortisol_delta", 0.0)
    D = hormone_shifts.get("dopamine_delta", 0.0)
    
    # Формула валентности из valence_calculator: O + D - C
    delta_valence = O + D - C
    abs_delta = abs(delta_valence)
    
    score = min(1.0, abs_delta / SALIENCY_VIVID_THRESHOLD)
    
    if abs_delta < SALIENCY_THRESHOLD:
        label = SALIENCY_LABEL_NEUTRAL
    elif delta_valence >= 0:
        label = SALIENCY_LABEL_POSITIVE
    else:
        label = SALIENCY_LABEL_NEGATIVE
    
    return round(score, 2), label

# =============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ БД
# =============================================================================
def _calculate_pair_metrics(agent_text: str, user_text: str, delay_ms: int = 0) -> Dict[str, Any]:
    """
    Рассчитывает метрики пары и булевы флаги для LLM (модель не считает, а читает).
    
    Вычисляет:
    - Длины в токенах (через count_tokens_qwen)
    - Коэффициент вовлеченности E
    - Лексическое перекрытие (Jaccard similarity)
    - Булевы флаги для условий паттернов
    
    Args:
        agent_text: Текст реплики агента
        user_text: Текст реплики пользователя
        delay_ms: Задержка ответа в мс
        
    Returns:
        Dict с метриками и флагами для вставки в prompt
    """
    agent_tokens = count_tokens_qwen(agent_text) if agent_text else 0
    user_tokens = count_tokens_qwen(user_text) if user_text else 0
    
    # Коэффициент вовлеченности
    e = (user_tokens / max(agent_tokens, 1)) * 1.0
    if e < 0.2: 
        engagement_interp = "break"
    elif e < 0.6: 
        engagement_interp = "low"
    elif e <= 1.4: 
        engagement_interp = "balance"
    else: 
        engagement_interp = "high"
    
    # Лексическое перекрытие (Jaccard similarity на уровне слов)
    agent_words = set(agent_text.lower().split()) if agent_text else set()
    user_words = set(user_text.lower().split()) if user_text else set()
    
    if agent_words and user_words:
        intersection = agent_words & user_words
        union = agent_words | user_words
        lexical_overlap = len(intersection) / len(union) if union else 0.0
    else:
        lexical_overlap = 0.0
    
    # Контекст агента для is_context_negative
    agent_lower = (agent_text or "").lower()
    
    return {
        # Базовые метрики
        "agent_tokens": agent_tokens,
        "user_tokens": user_tokens,
        "engagement_coef": round(e, 3),
        "engagement_interp": engagement_interp,
        "lexical_overlap": round(lexical_overlap, 3),
        # Булевы флаги для LLM (модель читает, не считает)
        "is_delay_critical": str(delay_ms > DELAY_CRITICAL_THRESHOLD_MS).lower(), # Из константы
        "agent_has_question": str("?" in (agent_text or "")).lower(),
        "is_overlap_high": str(lexical_overlap > 0.4).lower(),
        "is_overlap_medium": str(lexical_overlap > 0.2).lower(),
        "is_overlap_low": str(lexical_overlap < 0.3).lower(),
        "is_engagement_low": str(e < 0.2).lower(),
        "is_context_negative": str(any(
            w in agent_lower 
            for w in ["ошибка", "неидеально", "сомневаюсь", "плохо", "провал"]
        )).lower(),
        "has_question_mark": str("?" in (user_text or "")).lower(),
    }

def _fetch_message_pair(conn, user_message_id: str) -> Tuple[Optional[str], str, int, str]:
    """
    Извлекает пару реплик (Агент → Пользователь) и рассчитывает задержку в мс.
    
    ВАЖНО: Использует колонку timestamp, так как created_at в row_messages отсутствует.
    Проверяет сессионную принадлежность пары.
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT 
                usr.row_text AS user_text, usr.timestamp AS user_at, usr.session_id AS user_session_id,
                agn.row_text AS agent_text, agn.timestamp AS agent_at, agn.session_id AS agent_session_id
            FROM dialogs.row_messages usr
            LEFT JOIN dialogs.row_messages agn ON usr.parent_message_id = agn.id
            WHERE usr.id = %s
        """, (user_message_id,))
        row = cur.fetchone()
        
        if not row:
            raise ValueError(f"Сообщение пользователя {user_message_id} не найдено в БД.")
            
        user_text = row['user_text'] or ""
        session_id = str(row['user_session_id'])
        
        agent_text = None
        if row['agent_text'] and row['agent_session_id'] == row['user_session_id']:
            agent_text = row['agent_text']
        elif row['agent_text']:
            logger.warning("Parent message belongs to different session. Rejecting pair.")
        
        silence_ms = 0
        if row['agent_at'] and row['user_at']:
            delta = row['user_at'] - row['agent_at']
            silence_ms = int(delta.total_seconds() * 1000)
            
        return agent_text, user_text, silence_ms, session_id

def _calculate_engagement(agent_text: str, user_text: str) -> str:
    """Рассчитывает коэффициент вовлеченности."""
    len_a = count_tokens_qwen(agent_text)
    len_u = count_tokens_qwen(user_text)
    e = (len_u / max(len_a, 10)) * 1.0
    if e < 0.2:
        return "break"
    elif e < 0.6:
        return "low"
    elif e <= 1.4:
        return "balance"
    else:
        return "high"

# =============================================================================
# ОСНОВНОЙ ОБРАБОТЧИК
# =============================================================================
def _get_momentary_state_projection(conn, actor_id: str) -> Optional[str]:
    """
    Загружает текстовую проекцию текущего momentary состояния для актора.
    
    Используется для внедрения в системный промпт анализа, если включена настройка.
    Возвращает поле content из state.self_knowledge, связанного через momentary.state_id.
    
    Args:
        conn: Активное соединение с PostgreSQL
        actor_id: UUID актора
        
    Returns:
        str | None: Текстовое описание состояния или None, если не найдено.
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT sk.content
            FROM state.momentary m
            JOIN state.self_knowledge sk ON m.state_id = sk.id
            WHERE m.actor_id = %s AND m.is_active = TRUE
            LIMIT 1
        """, (actor_id,))
        row = cur.fetchone()
        return row['content'] if row else None

def handle_affective_analysis(task_id: str, input_data: Dict[str, Any]) -> None:
    """
    Пайплайн аффективного анализа пары реплик.
    
    Логика:
    1. Помечает задачу running.
    2. Извлекает сообщение и родительскую реплику агента с проверкой session_id.
       Если реплики из разных сессий — анализ пропускается (fallback).
    3. Получает PHS-срез и загружает настройку affective_analysis_use_momentary_state.
    4. Если настройка включена — внедряет текущее состояние в системный промпт.
    5. Если реплика агента отсутствует или сессии не совпадают — применяет fallback.
    6. Иначе вызывает LLM, парсит ответ, рассчитывает сдвиги.
    7. Сохраняет результат в state.affective_analyses с флагом used_momentary_context.
    8. Получает valence ДО сдвига из активного momentary.
    9. Применяет гормональный сдвиг к momentary через MomentaryManager.apply_affective_shift
       (с масштабированием, габитуацией, адаптацией и cross-inhibition).
    10. Получает valence ПОСЛЕ сдвига из нового momentary.
    11. Рассчитывает salience (значимость события для графа памяти) через
        _calculate_salience(valence_before, valence_after).
    12. Обновляет трассировку в dialogs.row_messages:
        - user_message: phs_affective_analysis_id + salience
        - parent agent_message: тот же salience (пара = одно событие)
    13. Завершает задачу/шаг.
    
    Salience механизм:
        delta_valence = valence_after - valence_before
        score = min(1.0, |delta_valence| / SALIENCY_VIVID_THRESHOLD)
        label = neutral (|delta| < SALIENCY_THRESHOLD) 
              | positive (delta >= SALIENCY_THRESHOLD)
              | negative (delta <= -SALIENCY_THRESHOLD)
        
        Константы SALIENCY_VIVID_THRESHOLD и SALIENCY_THRESHOLD настраиваются
        вверху модуля. Используются для графа памяти: positive/negative события
        сохраняются как эпизодическая память, neutral — отбрасываются как рутина.
    
    Страховки:
    - Проверка session_id для пары реплик (изоляция сессий).
    - При ошибках парсинга JSON используется recovery pipeline.
    - При сбоях LLM или отсутствии контекста применяется DEFAULT_PROFILE.
    - Ошибка применения сдвига не ломает анализ (логируется как warning).
    - valence_before/valence_after инициализированы нулями до блока сдвига.
    - analysis_id извлекается сразу после INSERT, доступен во всех последующих блоках.
    - Все операции обернуты в try/except с корректным завершением задачи как failed.
    """
    mark_task_running(task_id)
    db_config = load_postgres_config()
    conn = None
    step_id = None

    try:
        conn = psycopg2.connect(**db_config)
        message_id = input_data.get('message_id')
        if not message_id:
            raise ValueError("message_id is missing from input_data.")

        logger.info(f"Affective analysis for message {message_id}")

        user_actor_id = input_data.get('user_actor_id')
        if not user_actor_id or not isinstance(user_actor_id, str):
            raise ValueError("The key 'user_actor_id' is missing or incorrect in the task's input_data.")
        baseline_id, momentary_id = get_current_phs_snapshot(db_config, user_actor_id)
        
        # Извлекаем пару с проверкой сессии
        agent_text, user_text, silence_ms, session_id = _fetch_message_pair(conn, message_id)

        # Загружаем настройку использования momentary контекста
        use_momentary_context = False
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value_float FROM state.settings WHERE param_name = %s",
                ("affective_analysis_use_momentary_state",)
            )
            row = cur.fetchone()
            use_momentary_context = bool(row and row[0] and float(row[0]) > 0.0)

        # Загружаем проекцию состояния, если настройка включена
        state_projection = None
        if use_momentary_context:
            # Явная проверка типа для устранения ошибок Pylance
            if user_actor_id and isinstance(user_actor_id, str):
                state_projection = _get_momentary_state_projection(conn, user_actor_id)
                if state_projection:
                    logger.debug(f"Injecting momentary state into prompt for actor {user_actor_id[:8]}")
                else:
                    logger.warning("use_momentary_state enabled but no state projection found")
            else:
                logger.warning("use_momentary_state enabled but user_actor_id is missing or invalid")
        
        # Инициализация pair_metrics для Pylance (заполнится в обеих ветках)
        pair_metrics = _calculate_pair_metrics(agent_text or "", user_text, silence_ms) 

        # Проверка на наличие валидной пары в рамках сессии
        if not agent_text or not agent_text.strip():
            # Если это первое сообщение (нет parent), это ожидаемая ситуация — логгируем как DEBUG
            if input_data.get('parent_message_id') is None:
                logger.debug("First message in the dialogue: agent's response is missing. Applying fallback profile.")
            else:
                logger.warning("Agent's response is missing or the pair is cross-session. Fallback profile.")
            
            analysis_result = process_analysis(None, "balance")
            analysis_result["_fallback_reason"] = "no_agent_context_or_cross_session"
            llm_metric_id = None
            used_context_flag = False
        else:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, text, params FROM orchestrator.prompts 
                    WHERE name = 'phs_affective_analysis' AND version = '1.0.0' AND status = 'testing'
                    ORDER BY created_at DESC LIMIT 1
                """)
                prompt_row = cur.fetchone()
            if not prompt_row:
                raise RuntimeError("Prompt phs_affective_analysis not found.")

            prompt_id = prompt_row['id']
            prompt_text = prompt_row['text']
            model_params = prompt_row['params'] or {}

            # Внедряем состояние в системный промпт, если включено
            if use_momentary_context and state_projection:
                injection = (
                    f"\n\n### ТВОЁ ТЕКУЩЕЕ СОСТОЯНИЕ\n"
                    f"{state_projection}\n\n"
                    f"Это состояние модулирует качество и глубину твоего анализа. "
                    f"Используй его для оценки контекста и чувствительности к паттернам.\n\n"
                )
                prompt_text = prompt_text.replace("### СИСТЕМНАЯ ИНСТРУКЦИЯ", f"### СИСТЕМНАЯ ИНСТРУКЦИЯ{injection}")
                used_context_flag = True
            else:
                used_context_flag = False

            # Рассчитываем метрики пары в Python (LLM не должна считать токены и параметры)
            pair_metrics = _calculate_pair_metrics(agent_text, user_text, silence_ms)
            
            step_input = {
                "message_id": message_id,
                "prompt_id": str(prompt_id),
                "agent_text_len": pair_metrics["agent_tokens"],
                "user_text_len": pair_metrics["user_tokens"],
                "silence_ms": silence_ms,
                "session_id": session_id,
                "used_momentary_context": used_context_flag,
                "lexical_overlap": pair_metrics["lexical_overlap"],
                "engagement_coef": pair_metrics["engagement_coef"],
                # Флаги для трассировки
                "flags": {
                    "is_delay_critical": pair_metrics["is_delay_critical"],
                    "is_engagement_low": pair_metrics["is_engagement_low"],
                    "is_overlap_high": pair_metrics["is_overlap_high"],
                    "is_context_negative": pair_metrics["is_context_negative"],
                    "has_question_mark": pair_metrics["has_question_mark"],
                    "agent_has_question": pair_metrics["agent_has_question"],
                }
            }
            
            step_id = create_orchestrator_step(
                task_id=task_id,
                step_number=1,
                step_type_name="phs_affective_analysis",
                input_data=step_input,
                baseline_id=baseline_id,
                momentary_id=momentary_id
            )
            
            # Новый user_payload с флагами (модель читает, не считает)
            user_payload = (
                f"ВХОДНЫЕ ДАННЫЕ:\n"
                f"Собственная_реплика: {agent_text}\n"
                f"Реплика_собеседника: {user_text if user_text else '[ПУСТАЯ СТРОКА (МОЛЧАНИЕ)]'}\n"
                f"задержка_ответа_мс: {silence_ms}\n"
                f"---\n"
                f"ПЕРЕДАННЫЕ МНЕ РАССЧИТАННЫЕ МЕТРИКИ И ФЛАГИ "
                f"(НЕ ВЫЧИСЛЯТЬ ИХ САМОСТОЯТЕЛЬНО, ИСПОЛЬЗОВАТЬ ГОТОВЫЕ):\n"
                f"- коэффициент_вовлеченности: {pair_metrics['engagement_coef']} ({pair_metrics['engagement_interp']})\n"
                f"- лексическое_перекрытие: {pair_metrics['lexical_overlap']}\n"
                f"- is_delay_critical: {pair_metrics['is_delay_critical']}\n"
                f"- agent_has_question: {pair_metrics['agent_has_question']}\n"
                f"- is_overlap_high: {pair_metrics['is_overlap_high']}\n"
                f"- is_overlap_medium: {pair_metrics['is_overlap_medium']}\n"
                f"- is_overlap_low: {pair_metrics['is_overlap_low']}\n"
                f"- is_engagement_low: {pair_metrics['is_engagement_low']}\n"
                f"- is_context_negative: {pair_metrics['is_context_negative']}\n"
                f"- has_question_mark: {pair_metrics['has_question_mark']}\n\n"
                f"Использовать эти значения для проверки условий паттернов.\n"
                f"Выполнить анализ и вернуть ТОЛЬКО валидный JSON без markdown-разметки. "
                f"БЫТЬ МАКСИМАЛЬНО КРАТКОЙ В ТЕКСТОВЫХ ПОЛЯХ."
            )
            messages = [
                {"role": "system", "content": prompt_text},
                {"role": "user", "content": user_payload}
            ]

            model_name = model_params.get('model_name')
            if not model_name:
                raise ValueError("model_name is missing from the prompt parameters.")

            # Фильтруем параметры аналогично response_composer.py
            # КРИТИЧНО: chat_template_kwargs с enable_thinking=false обязателен для Qwen3.5
            # Без него модель уходит в бесконечный thinking и возвращает пустой content
            safe_params = {
                k: v for k, v in model_params.items()
                if k in [
                    "temperature", "top_p", "top_k", "min_p", "max_tokens",
                    "presence_penalty", "repetition_penalty", "stop", "chat_template_kwargs"
                ]
            }

            logger.debug(f"Calling ModelService (step_id: {step_id}, used_context: {used_context_flag})")
            model_service = ModelService()
            response = model_service.generate(
                messages=messages,
                model_name=model_name,
                **safe_params
            )

            raw_content = response.get("content") or response.get("response") or ""
            if not isinstance(raw_content, str):
                raw_content = ""
            raw_content = raw_content.strip()

            metrics = response.get("metrics", {})
            timings = metrics.get("timings", {})
            usage = metrics.get("usage", {})

            llm_metric_id = save_llm_metrics(
                orchestrator_step_id=step_id,
                prompt_id=str(prompt_id),
                host=response.get("host", "local"),
                model=model_name,
                param=model_params,
                cache_n=timings.get("cache_n", 0),
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
                host_nctx=metrics.get("host_nctx", 0),
                prompt_ms=timings.get("prompt_ms", 0.0),
                prompt_per_token_ms=timings.get("prompt_per_token_ms", 0.0),
                prompt_per_second=timings.get("prompt_per_second", 0.0),
                predicted_per_second=timings.get("predicted_per_second", 0.0),
                resp_time=timings.get("predicted_ms", 0.0) / 1000.0,
                net_latency=0.0,
                full_time=0.0,
                error_status=False
            )

            engagement_interp = _calculate_engagement(agent_text, user_text)
            analysis_result = process_analysis(raw_content, engagement_interp)

        # Извлекаем только коды паттернов для TEXT[] колонки
        patterns_raw = analysis_result.get("detected_patterns", [])
        pattern_codes = []
        for p in patterns_raw:
            if isinstance(p, dict):
                code = p.get("code", "")
                if code:
                    pattern_codes.append(code)
            elif isinstance(p, str):
                pattern_codes.append(p)

        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO state.affective_analyses (
                    orchestrator_step_id, baseline_id, momentary_id, input_pair,
                    analysis_raw, detected_patterns, hormone_shifts, agent_state,
                    agent_reaction, user_mood, subtext, recommended_gen_params,
                    used_momentary_context, agent_version, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                RETURNING id
            """, (
                step_id,
                baseline_id,
                momentary_id,
                Json({
                    "agent_text": agent_text or "",
                    "user_text": user_text,
                    "silence_ms": silence_ms,
                    "session_id": session_id,
                    "agent_tokens": pair_metrics["agent_tokens"],
                    "user_tokens": pair_metrics["user_tokens"],
                    "engagement_coef": pair_metrics["engagement_coef"],
                    "engagement_interp": pair_metrics["engagement_interp"],
                    "lexical_overlap": pair_metrics["lexical_overlap"],
                    "is_delay_critical": pair_metrics["is_delay_critical"],
                    "agent_has_question": pair_metrics["agent_has_question"],
                    "is_overlap_high": pair_metrics["is_overlap_high"],
                    "is_overlap_medium": pair_metrics["is_overlap_medium"],
                    "is_overlap_low": pair_metrics["is_overlap_low"],
                    "is_engagement_low": pair_metrics["is_engagement_low"],
                    "is_context_negative": pair_metrics["is_context_negative"],
                    "has_question_mark": pair_metrics["has_question_mark"],
                }),
                Json(analysis_result),
                pattern_codes,  # ← ИСПРАВЛЕНО: массив строк вместо массива словарей
                Json(analysis_result.get("hormone_shifts", {})),
                json.dumps(analysis_result.get("agent_state", {}), ensure_ascii=False),
                json.dumps(analysis_result.get("agent_reaction", {}), ensure_ascii=False),
                analysis_result.get("user_mood", "Нейтральное"),
                analysis_result.get("subtext", ""),
                Json(analysis_result.get("recommended_gen_params", {})),
                used_context_flag,
                agent_version
            ))        
            analysis_id = str(cur.fetchone()[0]) 
            
        # === Получаем valence_before ===
        # Читаем momentary_id из row_messages для ДАННОГО сообщения —
        # это momentary на момент СОЗДАНИЯ сообщения (ДО user_message event).
        # Это даёт нам valence до ВСЕХ сдвигов, связанных с этой парой реплик.
        valence_before = 0.0
        valence_after = 0.0
        
        with conn.cursor() as cur:
            cur.execute(
                "SELECT momentary_id FROM dialogs.row_messages WHERE id = %s",
                (message_id,)
            )
            row = cur.fetchone()
            original_momentary_id = row[0] if row and row[0] else momentary_id
        
        if original_momentary_id:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT valence FROM state.momentary WHERE id = %s",
                    (original_momentary_id,)
                )
                row = cur.fetchone()
                if row:
                    valence_before = float(row[0])
                    valence_after = valence_before  # Дефолт: если сдвиг не применится
        
        # === Применение сдвига к momentary ===
        shift_result = None
        if not analysis_result.get("_fallback_used", False):
            raw_deltas = analysis_result.get("hormone_shifts", {})
            has_nonzero = any(
                abs(float(raw_deltas.get(k, 0.0))) > 0.01
                for k in ["oxytocin_delta", "cortisol_delta", "dopamine_delta"]
            )
            
            if has_nonzero:
                try:
                    from phs_service.phs_cache import get_momentary_manager
                    momentary_mgr = get_momentary_manager(db_config)
                    
                    shift_result = momentary_mgr.apply_affective_shift(
                        actor_id=user_actor_id,
                        deltas=raw_deltas,
                        step_id=step_id
                    )
                    if shift_result and shift_result.get("applied"):
                        new_momentary_id = shift_result.get("momentary_id_after")
                        if new_momentary_id:
                            with conn.cursor() as cur:
                                cur.execute(
                                    "SELECT valence FROM state.momentary WHERE id = %s",
                                    (new_momentary_id,)
                                )
                                row = cur.fetchone()
                                if row:
                                    valence_after = float(row[0])
                        logger.info(
                            f"Affective shift: {shift_result['momentary_id_before'][:8]} "
                            f"-> {shift_result['momentary_id_after'][:8]}, "
                            f"valence: {valence_before:.2f} -> {valence_after:.2f}"
                        )
                except Exception as shift_exc:
                    logger.warning(f"Failed to apply affective shift: {shift_exc}")
        
        # === Расчёт salience по СЫРЫМ дельтам из анализа ===
        # Сырые дельты — это реальный вклад модели, без искажений от габитуации/сатурации
        raw_deltas = analysis_result.get("hormone_shifts", {})
        salience_score, salience_label = _calculate_salience(raw_deltas)

        logger.info(
            f"Salience calculation: O={raw_deltas.get('oxytocin_delta', 0):.2f}, "
            f"C={raw_deltas.get('cortisol_delta', 0):.2f}, "
            f"D={raw_deltas.get('dopamine_delta', 0):.2f}, "
            f"delta_valence={raw_deltas.get('oxytocin_delta', 0) + raw_deltas.get('dopamine_delta', 0) - raw_deltas.get('cortisol_delta', 0):.2f}, "
            f"score={salience_score}, label={salience_label}"
        )
        
        # === Формирование step_output ===
        # Маппинг строки настроения (из нового промпта) в валентность
        mood_to_valence = {
            "Радость": "позитив", "Удивление": "позитив",
            "Страх": "негатив", "Гнев": "негатив", 
            "Отвращение": "негатив", "Печаль": "негатив",
            "Нейтральное": "нейтрально"
        }

        # === UPDATE row_messages (analysis_id уже определён) ===
        with conn.cursor() as cur:
            # Обновляем user_message
            cur.execute("""
                UPDATE dialogs.row_messages
                SET phs_affective_analysis_id = %s,
                    phs_affective_analysis_at = NOW(),
                    event_salience_score = %s,
                    event_salience_label = %s
                WHERE id = %s
            """, (analysis_id, salience_score, salience_label, message_id))
            
            # Обновляем parent agent_message
            cur.execute("""
                UPDATE dialogs.row_messages
                SET event_salience_score = %s,
                    event_salience_label = %s
                WHERE id = (
                    SELECT parent_message_id 
                    FROM dialogs.row_messages 
                    WHERE id = %s
                )
            """, (salience_score, salience_label, message_id))
            
            conn.commit()
        
        step_output = {
            "analysis_id": str(analysis_id),
            "detected_patterns": analysis_result.get("detected_patterns", []),
            "user_mood": str(analysis_result.get("user_mood", "Нейтральное")).strip().capitalize(),
            "llm_metric_id": str(llm_metric_id) if llm_metric_id else None,
            "fallback_used": analysis_result.get("_fallback_used", False),
            "used_momentary_context": used_context_flag,
            "affective_shift_applied": bool(shift_result and shift_result.get("applied")),
            "momentary_id_after": shift_result.get("momentary_id_after") if shift_result else None
        }

        if step_id:
            complete_step_success(step_id, step_output)
        complete_task_success(task_id, step_output)
        logger.info(f"Analysis completed. analysis_id: {analysis_id}, used_context: {used_context_flag}")

    except Exception as e:
        logger.exception(f"Error in handle_affective_analysis (task_id: {task_id})")
        if conn:
            conn.rollback()
        error_msg = str(e)
        if step_id:
            complete_step_error(step_id, error_module="affective_analyzer", error_message=error_msg)
        complete_task_error(task_id, error_module="affective_analyzer", error_message=error_msg)
    finally:
        if conn:
            conn.close()