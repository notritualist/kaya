"""
Создание коллекции kaya_db в Qdrant для векторов 2560d
С оптимизацией: квантование INT8, on_disk_payload, настройки под high-dimensional данные
"""

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, 
    VectorParams, 
    HnswConfigDiff,
    OptimizersConfigDiff,
    ScalarQuantization,
    ScalarQuantizationConfig,
    ScalarType,
    WalConfigDiff
)

def create_kaya_db_collection():
    """Создание коллекции для памяти агента с оптимизацией под 2560d векторы"""
    
    # Подключение к локальному Qdrant
    client = QdrantClient("localhost", port=6333)
    
    # Проверка существования коллекции
    collection_name = "kaya_db"
    
    # Если коллекция существует, спросим что делать
    if client.collection_exists(collection_name):
        print(f"⚠️  Коллекция '{collection_name}' уже существует")
        response = input("Удалить и создать заново? (y/n): ")
        if response.lower() == 'y':
            client.delete_collection(collection_name)
            print(f"🗑️  Коллекция удалена")
        else:
            print("🚫 Операция отменена")
            return
    
    # Оптимальная конфигурация для 2560d векторов
    client.create_collection(
        collection_name=collection_name,
        vectors_config=VectorParams(
            size=2560,                          # Размерность векторов
            distance=Distance.COSINE,            # COSINE лучше для high-dimensional
            on_disk=False                        # Вектора в памяти (с учетом квантования)
        ),
        hnsw_config=HnswConfigDiff(
            m=16,                                # Для 2560d норм, можно и 32 если данных много
            ef_construct=100,                     # Качество построения индекса
            full_scan_threshold=10000,            # Полный скан до построения индекса
            max_indexing_threads=0,                # Auto
            on_disk=False                           # HNSW граф в памяти для скорости
        ),
        optimizers_config=OptimizersConfigDiff(
            deleted_threshold=0.2,                  # Порог удаления для оптимизации
            vacuum_min_vector_number=1000,           # Минимум векторов для оптимизации
            default_segment_number=2,                 # Начальное количество сегментов
            memmap_threshold=50000,                   # Увеличено! Только после 50к на диск
            indexing_threshold=10000,                  # HNSW после 10к точек
            flush_interval_sec=5,                       # Сброс на диск каждые 5 сек
            max_optimization_threads=2,                  # Ограничим потоки оптимизации
        ),
        wal_config=WalConfigDiff(
            wal_capacity_mb=1024,                      # Размер WAL
            wal_segments_ahead=0                         # Количество сегментов вперед
        ),
        quantization_config=ScalarQuantization(
            scalar=ScalarQuantizationConfig(
                type=ScalarType.INT8,                    # Квантование в int8
                quantile=0.99,                             # 99% квантиль для точности
                always_ram=True                             # Всегда держать сжатые вектора в RAM
            )
        ),
        on_disk_payload=True,                            # Payload на диск, вектора в RAM
        replication_factor=1,                              # Без репликации для теста
        shard_number=1                                      # Один шард
    )
    
    print(f"✅ Коллекция '{collection_name}' создана успешно")
    print(f"\n📊 КОНФИГУРАЦИЯ:")
    print(f"   Векторы: {2560}d, COSINE")
    print(f"   HNSW: m={16}, ef_construct={100}")
    print(f"   Payload: на диске (экономия RAM)")
    print(f"   Memmap порог: 50000 векторов")
    print(f"   Индексация: после 10000 векторов")
    
    # Детальная информация о квантовании
    print(f"\n🔧 КВАНТОВАНИЕ (экономия памяти):")
    print(f"   Тип: INT8 (4x сжатие)")
    print(f"   Оригинал: 2560 * 4 байта = 10 KB/вектор")
    print(f"   Сжатый: 2560 * 1 байт = 2.5 KB/вектор")
    print(f"   Экономия: 75% RAM")
    print(f"   always_ram: Да (сжатые вектора всегда в памяти)")
    
    # Проверка состояния коллекции
    info = client.get_collection(collection_name)
    print(f"\n📈 ИТОГОВАЯ КОНФИГУРАЦИЯ ИЗ QDRANT:")
    print(f"   Статус: {info.status}")
    print(f"   Количество сегментов: {info.segments_count}")
    print(f"   Квантование: {info.config.quantization_config is not None}")
    
    # Советы по использованию
    print(f"\n💡 СОВЕТЫ ПО ИСПОЛЬЗОВАНИЮ:")
    print("   1. При поиске добавляй oversampling для рескоринга:")
    print("      search(..., limit=100, oversampling=10.0) -> вернет top-10 с переранжированием")
    print("   2. Следи за метриками: indexed_vectors_count должен расти")
    print("   3. RAM расход: N * 2.5 KB для сжатых векторов")

def recreate_with_custom_settings():
    """Функция для пересоздания с кастомными настройками"""
    client = QdrantClient("localhost", port=6333)
    collection_name = "kaya_db"
    
    # Удаляем если есть
    if client.collection_exists(collection_name):
        client.delete_collection(collection_name)
        print("🗑️  Старая коллекция удалена")
    
    # Здесь можно вызвать create с другими параметрами
    create_kaya_db_collection()

if __name__ == "__main__":
    create_kaya_db_collection()