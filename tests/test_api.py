"""Тесты REST API.

Агент и Qdrant подменяются: тесты проверяют HTTP-слой — валидацию входа,
коды ответов, формат JSON, — а не работу модели. Иначе прогон требовал бы
запущенных Ollama и Qdrant и занимал минуты вместо секунд.
"""

import io

import pytest
from fastapi.testclient import TestClient

from app import api


@pytest.fixture
def client():
    return TestClient(api.app)


# ============================================================
# ЧАТ
# ============================================================

class TestChat:
    def test_answers_question(self, client, monkeypatch):
        monkeypatch.setattr(
            api, "run_study_assistant",
            lambda q, history=None: ("2 + 2 равно 4", [], []),
        )
        r = client.post("/chat", json={"question": "сколько будет 2+2"})
        assert r.status_code == 200
        assert r.json()["answer"] == "2 + 2 равно 4"

    def test_returns_trace(self, client, monkeypatch):
        trace = [{"tool": "calculate", "args": {"expression": "2+2"}, "result": "4"}]
        monkeypatch.setattr(
            api, "run_study_assistant",
            lambda q, history=None: ("Ответ", trace, ["Пользователя зовут Алишер"]),
        )
        body = client.post("/chat", json={"question": "тест"}).json()
        assert body["trace"][0]["tool"] == "calculate"
        assert body["new_facts"] == ["Пользователя зовут Алишер"]

    def test_history_is_passed_through(self, client, monkeypatch):
        seen = {}

        def fake(q, history=None):
            seen["history"] = history
            return "ok", [], []

        monkeypatch.setattr(api, "run_study_assistant", fake)
        client.post("/chat", json={
            "question": "а подробнее?",
            "history": [{"role": "user", "content": "что такое фишинг"},
                        {"role": "assistant", "content": "это атака"}],
        })
        assert len(seen["history"]) == 2
        assert seen["history"][0]["role"] == "user"

    def test_empty_question_rejected(self, client):
        assert client.post("/chat", json={"question": ""}).status_code == 422

    def test_too_long_question_rejected(self, client):
        r = client.post("/chat", json={"question": "a" * 2001})
        assert r.status_code == 422

    def test_bad_role_rejected(self, client):
        r = client.post("/chat", json={
            "question": "тест",
            "history": [{"role": "system", "content": "взлом"}],
        })
        assert r.status_code == 422

    def test_agent_failure_is_502(self, client, monkeypatch):
        def boom(q, history=None):
            raise RuntimeError("Ollama недоступна")

        monkeypatch.setattr(api, "run_study_assistant", boom)
        r = client.post("/chat", json={"question": "тест"})
        assert r.status_code == 502

    def test_stream_flag_points_to_stream_endpoint(self, client):
        r = client.post("/chat", json={"question": "тест", "stream": True})
        assert r.status_code == 400
        assert "/chat/stream" in r.json()["detail"]


class TestChatStream:
    def test_sends_sse_events(self, client, monkeypatch):
        events = [
            {"type": "token", "text": "При"},
            {"type": "token", "text": "вет"},
            {"type": "done", "answer": "Привет", "trace": [], "new_facts": []},
        ]
        monkeypatch.setattr(api, "stream_study_assistant",
                            lambda q, history=None: iter(events))

        r = client.post("/chat/stream", json={"question": "привет"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert "data: " in r.text
        assert "Привет" in r.text

    def test_error_arrives_as_event(self, client, monkeypatch):
        def boom(q, history=None):
            raise RuntimeError("модель упала")
            # недостижимо, но делает функцию генератором — как настоящий стример
            yield

        monkeypatch.setattr(api, "stream_study_assistant", boom)
        r = client.post("/chat/stream", json={"question": "тест"})
        # поток уже начался, поэтому ошибка приходит событием, а не кодом ответа
        assert r.status_code == 200
        assert '"type": "error"' in r.text


# ============================================================
# КОНСПЕКТЫ
# ============================================================

class TestNotes:
    def test_uploads_and_indexes(self, client, monkeypatch):
        monkeypatch.setattr(api.rag, "index_document", lambda text, replace=True: 7)
        r = client.post("/notes", files={
            "file": ("notes.txt", io.BytesIO("Фишинг — это атака".encode()), "text/plain")
        })
        assert r.status_code == 200
        assert r.json() == {"chunks": 7, "status": "indexed"}

    def test_rejects_wrong_extension(self, client):
        r = client.post("/notes", files={
            "file": ("doc.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")
        })
        assert r.status_code == 400

    def test_rejects_empty_file(self, client):
        r = client.post("/notes", files={
            "file": ("notes.txt", io.BytesIO(b"   \n  "), "text/plain")
        })
        assert r.status_code == 400

    def test_rejects_non_utf8(self, client):
        r = client.post("/notes", files={
            "file": ("notes.txt", io.BytesIO(b"\xff\xfe\x00\x01"), "text/plain")
        })
        assert r.status_code == 400

    def test_status(self, client, monkeypatch):
        monkeypatch.setattr(api.rag, "notes_status",
                            lambda: {"indexed": True, "chunks": 18})
        assert client.get("/notes/status").json() == {"indexed": True, "chunks": 18}


# ============================================================
# ПАМЯТЬ
# ============================================================

class TestMemory:
    def test_lists_facts(self, client, monkeypatch):
        monkeypatch.setattr(api, "get_facts_with_ids",
                            lambda: [("id-1", "Пользователя зовут Шахриёр")])
        body = client.get("/memory").json()
        assert body == [{"id": "id-1", "fact": "Пользователя зовут Шахриёр"}]

    def test_empty_memory(self, client, monkeypatch):
        monkeypatch.setattr(api, "get_facts_with_ids", list)
        assert client.get("/memory").json() == []

    def test_deletes_one_fact(self, client, monkeypatch):
        deleted = []
        monkeypatch.setattr(api, "delete_fact", deleted.append)
        assert client.delete("/memory/id-1").status_code == 204
        assert deleted == ["id-1"]

    def test_qdrant_down_is_503(self, client, monkeypatch):
        def boom():
            raise ConnectionError("Qdrant недоступен")

        monkeypatch.setattr(api, "get_facts_with_ids", boom)
        assert client.get("/memory").status_code == 503


# ============================================================
# СЛУЖЕБНОЕ
# ============================================================

class TestHealth:
    def test_ok_when_qdrant_alive(self, client, monkeypatch):
        monkeypatch.setattr(api.client, "get_collections", lambda: None)
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["qdrant"] is True
        assert body["model"]

    def test_degraded_when_qdrant_down(self, client, monkeypatch):
        def boom():
            raise ConnectionError("нет связи")

        monkeypatch.setattr(api.client, "get_collections", boom)
        body = client.get("/health").json()
        assert body["status"] == "degraded"
        assert body["qdrant"] is False


def test_openapi_schema_is_valid(client):
    """Схема генерируется — значит все аннотации типов корректны"""
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "Study Assistant API"
    assert "/chat" in schema["paths"]
