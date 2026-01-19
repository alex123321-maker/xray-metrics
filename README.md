# xray-metrics

Сбор метрик из Xray (3xui) через кастомный json‑exporter, vmagent и VictoriaMetrics с визуализацией в Grafana.

## Состав

- **json-exporter**: читает JSON из Xray `/debug/vars` и отдаёт Prometheus‑метрики.
- **xray-checker**: проверяет прокси из подписки и отдаёт метрики статуса/задержки.
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

Дополнительно для Xray API (uptime/online):

- `XRAY_API_SOURCE` — HTTP(S) URL Xray API.
- `XRAY_API_TIMEOUT` — таймаут запроса к API (сек).

Маппинг пользователей (email -> подпись):

- `USER_MAP_FILE` — путь к JSON файлу со словарём или массивом объектов.

Интеграция 3x-ui API (email/подпись, онлайн):

- `UI_HOST`, `UI_PORT`, `UI_BASEPATH`, `UI_SCHEME` — адрес панели.
- `UI_USERNAME`, `UI_PASSWORD` — логин/пароль (cookie-сессия).
- `UI_BEARER_TOKEN` — Bearer token, если включён.
- `UI_API_KEY` — apiKey header, если требуется.
- `UI_LOGIN_PATH`, `UI_INBOUNDS_PATH`, `UI_ONLINE_PATH` — пути API.
- `UI_INSECURE=true` — отключить проверку TLS (если свой сертификат).

Дополнительно для xray-checker:

- `XRAY_CHECKER_SUBSCRIPTION_URL` — ссылка на подписку.
Примечание: xray-checker запущен в постоянном режиме и отдаёт метрики на
`/metrics`.

## Примечания

- Grafana слушает порт `${GRAFANA_PORT:-3000}`.
- VictoriaMetrics слушает порт `${VM_PORT:-8428}`.
- Экспортёр слушает `${EXPORTER_PORT:-9108}`.
