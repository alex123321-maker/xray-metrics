# xray-metrics

Сбор метрик из Xray (3xui) через кастомный json‑exporter, vmagent и VictoriaMetrics с визуализацией в Grafana.

## Состав

- **json-exporter**: читает JSON из Xray `/debug/vars` и отдаёт Prometheus‑метрики.
- **vmagent**: собирает метрики у экспортёра и пишет в VictoriaMetrics.
- **VictoriaMetrics**: хранение метрик.
- **Grafana**: дашборды.

## Запуск

1. Убедитесь, что Xray отдаёт JSON по `EXPORTER_SOURCE`.
2. Проверьте `.env`.
3. Запустите:
   - `docker compose up -d`

## Конфиги

- Скрейп: `config/scrape.yml`
- Дашборды: `config/grafana-dashboards/`
- Provisioning: `config/grafana-provisioning/`

## Переменные окружения

См. `.env`.

## Примечания

- Grafana слушает порт `${GRAFANA_PORT:-3000}`.
- VictoriaMetrics слушает порт `${VM_PORT:-8428}`.
- Экспортёр слушает `${EXPORTER_PORT:-9108}`.
