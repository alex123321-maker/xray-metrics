# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY json_exporter.py /app/json_exporter.py

# Минимальные зависимости + curl для healthcheck
RUN pip install --no-cache-dir prometheus-client \
 && apt-get update && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# Нерутовый пользователь
RUN useradd -m -u 10001 appuser
USER appuser

# Порт метрик
EXPOSE 9108

# Значения по умолчанию (можно переопределить при запуске)
ENV LISTEN="0.0.0.0:9108" \
    TIMEOUT="5" \
    DELAY_UNIT="ms"

# Healthcheck дергает /metrics
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${LISTEN##*:}/metrics" || exit 1

# Можно передавать параметры через ENV (SOURCE и др.) или через аргументы
ENTRYPOINT ["python", "/app/json_exporter.py"]
# CMD можно не задавать — если установите ENV SOURCE. Либо:
# CMD ["--source", "/data/stats.json"]

