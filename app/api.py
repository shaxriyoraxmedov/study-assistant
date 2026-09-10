"""REST API поверх агента.

Слой намеренно тонкий: вся логика живёт в agent.py и rag.py, здесь только
HTTP-транспорт, валидация входа и перевод ошибок в коды ответа. Благодаря
этому UI и API используют один и тот же код агента, а не две его версии.

Запуск: uvicorn app.api:app --host 0.0.0.0 --port 8000
Документация: http://localhost:8000/docs
"""

import json
from typing import Literal

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app import rag
from app.agent import (
    CHAT_MODEL,
    client,
    delete_fact,
    ensure_memory_collection,
    get_facts_with_ids,
    run_study_assistant,
    stream_study_assistant,
    MEMORY_COLLECTION,
)

app = FastAPI(
    title="Study Assistant API",
    description="RAG-агент с долгой памятью: вопросы по конспектам, Википедия, вычисления.",
    version="1.0.0",
)


# ============================================================
# СХЕМЫ
# ============================================================

class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000,
                          examples=["Что такое фишинг?"])
    history: list[Message] = Field(default_factory=list,
                                   description="Предыдущие реплики диалога")
    stream: bool = Field(default=False,
                         description="Отдавать ответ по мере генерации (SSE)")


class TraceStep(BaseModel):
    tool: str
    args: dict
    result: str


class ChatResponse(BaseModel):
    answer: str
    trace: list[TraceStep] = Field(description="Какие инструменты вызвал агент")
    new_facts: list[str] = Field(description="Что запомнил о пользователе")


class Fact(BaseModel):
    id: str
    fact: str


class IndexResponse(BaseModel):
    chunks: int
    status: str = "indexed"


class NotesStatus(BaseModel):
    indexed: bool
    chunks: int


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    model: str
    qdrant: bool


# ============================================================
# ЧАТ
# ============================================================

@app.post("/chat", response_model=ChatResponse, tags=["chat"])
def chat(request: ChatRequest):
    """Задать вопрос агенту и получить ответ целиком.

    Для длинных ответов удобнее /chat/stream — там токены приходят по мере
    генерации, а не после 10-30 секунд молчания.
    """
    if request.stream:
        raise HTTPException(
            status_code=400,
            detail="Для стриминга используйте POST /chat/stream",
        )

    history = [m.model_dump() for m in request.history]
    try:
        answer, trace, new_facts = run_study_assistant(request.question, history=history)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Ошибка агента: {e}") from e

    return ChatResponse(answer=answer, trace=trace, new_facts=new_facts)


@app.post("/chat/stream", tags=["chat"])
def chat_stream(request: ChatRequest):
    """То же самое, но потоком Server-Sent Events.

    Каждое событие — JSON одного из типов: tool (агент вызвал инструмент),
    result (инструмент отработал), token (кусок ответа), done (итог).
    """
    history = [m.model_dump() for m in request.history]

    def events():
        try:
            for ev in stream_study_assistant(request.question, history=history):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:
            err = {"type": "error", "message": str(e)[:300]}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        # выключаем буферизацию nginx, иначе поток копится и приходит целиком
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ============================================================
# КОНСПЕКТЫ
# ============================================================

@app.post("/notes", response_model=IndexResponse, tags=["notes"])
async def upload_notes(file: UploadFile = File(..., description="Текстовый файл .txt или .md")):
    """Загрузить конспект. Индексация заменяет предыдущий документ."""
    if not file.filename or not file.filename.lower().endswith((".txt", ".md")):
        raise HTTPException(status_code=400, detail="Ожидается файл .txt или .md")

    raw = await file.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Файл должен быть в кодировке UTF-8") from None

    if not text.strip():
        raise HTTPException(status_code=400, detail="Файл пустой")

    try:
        chunks = rag.index_document(text)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Не удалось проиндексировать: {e}") from e

    return IndexResponse(chunks=chunks)


@app.get("/notes/status", response_model=NotesStatus, tags=["notes"])
def notes_status():
    """Сколько фрагментов конспекта проиндексировано."""
    try:
        return NotesStatus(**rag.notes_status())
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Qdrant недоступен: {e}") from e


# ============================================================
# ПАМЯТЬ
# ============================================================

@app.get("/memory", response_model=list[Fact], tags=["memory"])
def list_memory():
    """Все факты, которые агент запомнил о пользователе."""
    try:
        return [Fact(id=pid, fact=fact) for pid, fact in get_facts_with_ids()]
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Память недоступна: {e}") from e


@app.delete("/memory/{fact_id}", status_code=204, tags=["memory"])
def forget_fact(fact_id: str):
    """Удалить один факт — агент иногда запоминает лишнее."""
    try:
        delete_fact(fact_id)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Не удалось удалить: {e}") from e


@app.delete("/memory", status_code=204, tags=["memory"])
def clear_memory():
    """Стереть всю память о пользователе."""
    try:
        if client.collection_exists(MEMORY_COLLECTION):
            client.delete_collection(MEMORY_COLLECTION)
        ensure_memory_collection()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Не удалось очистить: {e}") from e


# ============================================================
# СЛУЖЕБНОЕ
# ============================================================

@app.get("/health", response_model=HealthResponse, tags=["service"])
def health():
    """Статус сервиса и зависимостей — для оркестратора и мониторинга."""
    try:
        client.get_collections()
        qdrant_ok = True
    except Exception:
        qdrant_ok = False

    return HealthResponse(
        status="ok" if qdrant_ok else "degraded",
        model=CHAT_MODEL,
        qdrant=qdrant_ok,
    )
