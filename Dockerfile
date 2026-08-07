FROM python:3.12-slim

# не /app: пакет проекта тоже называется app/, и /app/app читается как ошибка
WORKDIR /srv/study-assistant

# Зависимости отдельным слоем — правка кода не пересобирает torch.
#
# CPU-индекс PyTorch обязателен: дефолтный пакет тянет CUDA-рантайм NVIDIA
# (~2.7 ГБ) + triton (~0.7 ГБ), из-за чего образ весил 9 ГБ вместо 3.
# GPU здесь не нужен — reranker явно запускается на CPU (см. rag.py),
# а Ollama работает на хосте и в контейнер не входит.
COPY requirements.txt .
RUN pip install --no-cache-dir \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        -r requirements.txt

COPY app/ ./app/
COPY streamlit_app.py .
# без базовой темы Streamlit подставит свою — в контейнере фон уезжал бы в синий
COPY .streamlit/ ./.streamlit/

# cross-encoder скачивается с HuggingFace при первом вопросе к конспектам;
# кладём кэш внутрь образа, чтобы перезапуск контейнера не тянул модель заново
ENV HF_HOME=/srv/study-assistant/.cache/huggingface

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health')"

CMD ["streamlit", "run", "streamlit_app.py", "--server.port=8501", "--server.address=0.0.0.0"]
