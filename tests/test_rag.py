"""Тесты chunking и reranking.

Настоящий cross-encoder здесь не грузится — он весит ~600 МБ и в CI не нужен:
проверяется логика сортировки и обрезки, а не качество модели.
"""

from app import rag


class TestChunkText:
    def test_short_text_is_one_chunk(self):
        assert rag.chunk_text("Короткий текст") == ["Короткий текст"]

    def test_long_text_is_split(self):
        chunks = rag.chunk_text("Предложение о фишинге. " * 100, chunk_size=200)
        assert len(chunks) > 1
        assert all(len(c) <= 250 for c in chunks)  # запас на границу разделителя

    def test_empty_text(self):
        assert rag.chunk_text("") == []

    def test_chunks_overlap(self):
        """overlap нужен, чтобы определение не терялось на стыке чанков"""
        text = ". ".join(f"Предложение номер {i}" for i in range(60))
        chunks = rag.chunk_text(text, chunk_size=200, chunk_overlap=50)
        assert len(chunks) > 1


class FakeReranker:
    """Возвращает score = позиция подстроки-маркера: чем раньше, тем релевантнее"""

    def predict(self, pairs):
        return [1.0 if "нужный" in chunk else 0.1 for _, chunk in pairs]


class TestRerank:
    def test_relevant_chunk_moves_up(self, monkeypatch):
        monkeypatch.setattr(rag, "_reranker", FakeReranker())
        candidates = ["мусор один", "мусор два", "нужный фрагмент"]
        assert rag.rerank("вопрос", candidates, top_k=1) == ["нужный фрагмент"]

    def test_top_k_limits_output(self, monkeypatch):
        monkeypatch.setattr(rag, "_reranker", FakeReranker())
        candidates = [f"чанк {i}" for i in range(10)]
        assert len(rag.rerank("вопрос", candidates, top_k=3)) == 3

    def test_empty_candidates(self):
        """Пустой retrieval не должен трогать reranker вообще"""
        assert rag.rerank("вопрос", [], top_k=3) == []
