FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml /app/
COPY research/master-prd.md /app/research/master-prd.md
COPY src /app/src
COPY migrations /app/migrations
COPY scripts /app/scripts

RUN pip install ".[server]"

CMD ["python", "-m", "agp", "serve", "--host", "0.0.0.0", "--port", "7860"]
