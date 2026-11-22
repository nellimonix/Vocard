FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends age tzdata zstd \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir paramiko==3.5.1

WORKDIR /backup
COPY worker.py .
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 0:0
ENTRYPOINT ["python", "/backup/worker.py"]
