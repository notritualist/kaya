"""
main-srv/src/phs_service/state_classifier.py

PHS State classification service for PHS using vector similarity search.

Features:
- Classifies hormonal vectors by finding the closest emotion prototype in DB.
- Prototypes stored in state.self_knowledge with entry_type='emotion_prototype'.
- Uses pgvector cosine distance for efficient matching.
- Decision logic constants configurable for thresholds and behavior.
- No hardcoded state parameters; all prototypes managed via DB/migrations.

Architecture:
- Stateless classifier: reads prototypes from DB on each classification.
- Prototypes initialized by migration V004, vectors computed on first encoder run.
- Reusable for baseline, momentary, and any hormonal profile classification.
"""

version = "1.1.0"
description = "PHS State Classifier"

import logging
import psycopg2
from psycopg2.extras import RealDictCursor
from typing import Dict, Any, List
from dataclasses import dataclass

from phs_service.vector_encoder import HormonalVectorEncoder
from phs_service.valence_calculator import compute_valence

logger = logging.getLogger(__name__)

# =============================================================================
# КОНСТАНТЫ ЛОГИКИ КЛАССИФИКАЦИИ
# =============================================================================
# Пороговые значения и настройки принятия решений, не параметры состояний

#: Минимальная уверенность классификации для принятия состояния.
#: Если confidence ниже порога, состояние считается неопределённым.
CLASSIFICATION_MIN_CONFIDENCE: float = 0.35

#: Максимальное количество возвращаемых кандидатов при отладке.
CLASSIFICATION_TOP_K: int = 3

#: Тип записи в self_knowledge для прототипов эмоций.
ENTRY_TYPE_PROTOTYPE: str = "emotion_prototype"


@dataclass
class StateMatch:
    """
    Результат классификации состояния.
    
    Содержит информацию о ближайшем прототипе и метрики сходства.
    """
    state_id: str
    state_code: str
    state_name: str
    description: str
    core_affect: str
    distance: float
    confidence: float


class StateClassifier:
    """
    Классификатор гормональных состояний по векторному сходству.
    
    Использует RFF-векторы и косинусное расстояние для сопоставления
    текущего профиля с эталонными прототипами, хранящимися в БД.
    Прототипы управляются через миграции и таблицу state.self_knowledge.
    """

    def __init__(self, db_config: Dict[str, Any]):
        """
        Инициализация классификатора.
        
        Args:
            db_config: Параметры подключения к PostgreSQL.
        """
        self.db_config = db_config
        self.encoder = HormonalVectorEncoder(db_config)
        logger.debug("StateClassifier initialized.")

    def _ensure_prototype_vectors(self) -> None:
        """
        Проверяет и вычисляет векторы для прототипов, если они отсутствуют.
        
        Вызывается лениво при первой классификации.
        Генерирует векторы через HormonalVectorEncoder и сохраняет в БД.
        """
        with psycopg2.connect(**self.db_config) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Находим прототипы без вектора
                cur.execute(
                    """
                    SELECT id, state_code, cortisol, dopamine, oxytocin, valence
                    FROM state.self_knowledge
                    WHERE entry_type = %s AND prototype_vector IS NULL
                    """,
                    (ENTRY_TYPE_PROTOTYPE,)
                )
                prototypes = cur.fetchall()
                
                if not prototypes:
                    return
                
                logger.info(
                    f"Found {len(prototypes)} prototypes without vectors. Computing and saving..."
                )
                
                for proto in prototypes:
                    vector = self.encoder.encode(
                        cortisol=proto["cortisol"],
                        dopamine=proto["dopamine"],
                        oxytocin=proto["oxytocin"],
                        valence=proto["valence"]
                    )
                    
                    cur.execute(
                        """
                        UPDATE state.self_knowledge
                        SET prototype_vector = %s
                        WHERE id = %s
                        """,
                        (vector, proto["id"])
                    )
                
                conn.commit()
                logger.info("Prototype vectors computed and saved.")

    def classify_vector(self, state_vector: List[float]) -> StateMatch:
        """
        Классифицирует вектор состояния по ближайшему прототипу.
        
        Выполняет поиск в state.self_knowledge через косинусное расстояние.
        Автоматически вычисляет векторы прототипов при первом вызове.
        
        Args:
            state_vector: Вектор состояния (128 float).
            
        Returns:
            StateMatch: Информация о ближайшем состоянии и метрики сходства.
            
        Raises:
            RuntimeError: Если прототипы отсутствуют в БД.
        """
        # Ленивая инициализация векторов
        self._ensure_prototype_vectors()
        
        with psycopg2.connect(**self.db_config) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Поиск ближайшего прототипа
                cur.execute(
                    """
                    SELECT id, state_code, content, core_affect,
                           prototype_vector <=> %s::halfvec AS distance
                    FROM state.self_knowledge
                    WHERE entry_type = %s AND is_active = TRUE
                    ORDER BY distance ASC
                    LIMIT 1
                    """,
                    (state_vector, ENTRY_TYPE_PROTOTYPE)
                )
                row = cur.fetchone()
                
                if not row:
                    raise RuntimeError(
                        "No emotion prototypes found in state.self_knowledge. "
                        "Ensure migration V004 was applied."
                    )
                
                distance = float(row["distance"])
                confidence = max(0.0, 1.0 - distance)
                
                # Извлекаем state_name из content или отдельного поля если нужно
                # Пока используем state_code как имя для простоты
                state_name = row["state_code"].replace("_", " ").title()
                
                return StateMatch(
                    state_id=str(row["id"]),
                    state_code=row["state_code"],
                    state_name=state_name,
                    description=row["content"],
                    core_affect=row["core_affect"] or "",
                    distance=distance,
                    confidence=confidence
                )

    def classify_profile(
        self,
        cortisol: float,
        dopamine: float,
        oxytocin: float
    ) -> tuple[StateMatch, List[float], float]:
        """
        Классифицирует гормональный профиль.
        
        Принимает уровни гормонов, вычисляет валентность и вектор,
        затем возвращает классификацию.
        
        Args:
            cortisol: Уровень кортизола [0..100].
            dopamine: Уровень дофамина [0..100].
            oxytocin: Уровень окситоцина [0..100].
            
        Returns:
            Кортеж: (StateMatch, вектор, валентность).
        """
        valence = compute_valence(cortisol, dopamine, oxytocin)
        vector = self.encoder.encode(cortisol, dopamine, oxytocin, valence)
        match = self.classify_vector(vector)
        return match, vector, valence