"""Индексация конспектов и retrieval с cross-encoder reranking.

Перенесено из Недели 2 с двумя изменениями под продакшн:
  - адреса Qdrant/Ollama берутся из окружения (в Docker это не localhost);
  - reranker грузится лениво, а не на импорте. Модель весит ~600 МБ и тянется
    с HuggingFace: на импорте она ломала бы и старт UI, и тесты в CI.
"""

import os

import ollama
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams
from langchain_text_splitters import RecursiveCharacterTextSplitter

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
EMBED_MODEL = "bge-m3"
EMBEDDING_DIM = 1024  # размерность bge-m3
NOTES_COLLECTION = "rag_notes"
RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"

client = QdrantClient(url=QDRANT_URL)
ollama_client = ollama.Client(host=OLLAMA_HOST)

_reranker = None


def get_reranker():
    """Загружает cross-encoder при первом обращении и держит в памяти."""
    global _reranker
    if _reranker is None:
        from sentence_transformers import CrossEncoder

        _reranker = CrossEncoder(RERANKER_MODEL, device="cpu")
    return _reranker


def get_embedding(text: str) -> list[float]:
    return ollama_client.embeddings(model=EMBED_MODEL, prompt=text)["embedding"]


def chunk_text(text: str, chunk_size: int = 500, chunk_overlap: int = 50) -> list[str]:
    """chunk_size=500 выбран на Дне 10: 100 рвал определения посреди мысли,
    500 держит термин вместе с его расшифровкой."""
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", ". ", " "],
    )
    return splitter.split_text(text)


def index_document(text: str, replace: bool = True) -> int:
    """Индексирует документ, возвращает число чанков.

    replace=True пересоздаёт коллекцию — так ведёт себя загрузка файла в UI:
    новый конспект заменяет старый, иначе агент отвечает по смеси документов.
    """
    chunks = chunk_text(text)
    if not chunks:
        return 0

    if replace or not client.collection_exists(NOTES_COLLECTION):
        if client.collection_exists(NOTES_COLLECTION):
            client.delete_collection(NOTES_COLLECTION)
        client.create_collection(
            collection_name=NOTES_COLLECTION,
            vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
        )

    points = [
        PointStruct(id=i, vector=get_embedding(chunk), payload={"text": chunk})
        for i, chunk in enumerate(chunks)
    ]
    client.upsert(collection_name=NOTES_COLLECTION, points=points)
    return len(chunks)


def retrieve_candidates(question: str, top_k: int = 10) -> list[str]:
    if not client.collection_exists(NOTES_COLLECTION):
        return []
    results = client.query_points(
        collection_name=NOTES_COLLECTION, query=get_embedding(question), limit=top_k
    ).points
    return [r.payload["text"] for r in results]


def rerank(question: str, candidates: list[str], top_k: int = 3) -> list[str]:
    """Cross-encoder переоценивает кандидатов, глядя на пару (вопрос, чанк) целиком.

    Эмбеддинги сравнивают два независимо посчитанных вектора и потому путают
    близкие темы; reranker читает вопрос и чанк вместе, поэтому top-10 → top-3
    заметно чище на сравнительных вопросах.
    """
    if not candidates:
        return []
    scores = get_reranker().predict([[question, chunk] for chunk in candidates])
    scored = sorted(zip(scores, candidates), key=lambda pair: pair[0], reverse=True)
    return [chunk for _, chunk in scored[:top_k]]


def search_notes(question: str, top_k: int = 3) -> list[str]:
    """Полный retrieval: широкий поиск по эмбеддингам, затем reranking."""
    return rerank(question, retrieve_candidates(question, top_k=10), top_k=top_k)


def notes_status() -> dict:
    """Сколько чанков проиндексировано — UI показывает это в сайдбаре."""
    if not client.collection_exists(NOTES_COLLECTION):
        return {"indexed": False, "chunks": 0}
    return {"indexed": True, "chunks": client.count(NOTES_COLLECTION).count}
