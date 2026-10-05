# Backend + worker image (shared). CPU only. Tesseract with Hebrew data for OCR.
FROM python:3.12-slim-bookworm

ARG WITH_ML=true
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH HF_HOME=/opt/hf

RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-heb fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv==0.11.6

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN if [ "$WITH_ML" = "true" ]; then uv sync --frozen --no-install-project --extra ml; \
    else uv sync --frozen --no-install-project; fi \
 && rm -rf /root/.cache

# Pre-download the default embedding model at build time (no network needed at runtime).
RUN if [ "$WITH_ML" = "true" ]; then python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('intfloat/multilingual-e5-small')"; fi

COPY . .
RUN useradd --system --uid 10001 app && mkdir -p /data/files && chown -R app /data /app
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
