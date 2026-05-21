# Converter Bot Backend

Монорепозиторий с тремя Cloud Run сервисами для конвертации фото в JPEG через Telegram.

---

## Архитектура

```
Telegram → photo-convert-bot → Pub/Sub → photo-convert-worker → photo-converter
                                                    ↓
                                              Telegram (result)
```

1. **`photo-convert-bot`** — aiohttp webhook-сервер. Принимает Telegram-апдейты, проверяет пользователя (`ALLOWED_EDITORS`) и чат/топик, публикует задание в Pub/Sub.
2. **`photo-convert-worker`** — FastAPI + Pub/Sub push endpoint. Скачивает файл из Telegram, отправляет в converter, загружает JPG обратно в Telegram.
3. **`photo-converter`** — FastAPI HTTP API `POST /convert`. Конвертирует изображения через ImageMagick / dcraw / darktable / heif-convert.

### Поддерживаемые форматы (converter)

- **RAW:** DNG (включая Apple ProRAW), CR2, CR3, NEF, NRW, ARW, RAF, RW2, ORF, PEF, SRW, X3F, 3FR, IIQ, DCR, KDC, MRW
- **HEIF/HEIC:** `.heic`, `.heif`
- **Стандартные:** JPEG, PNG, TIFF, WebP

Цепочка RAW-декодеров: `exiftool preview → darktable-cli → rawtherapee-cli → dcraw_emu → dcraw`

---

## Env vars

### `photo-convert-bot`

| Переменная | Обязательная | Описание |
|---|---|---|
| `BOT_TOKEN` | ✓ | Telegram bot token |
| `TG_WEBHOOK_SECRET` | ✓ | Secret для заголовка `X-Telegram-Bot-Api-Secret-Token` |
| `ALLOWED_EDITORS` | ✓ | User ID через `,` `\|` или пробел |
| `CHAT_ID` | ✓ | ID чата |
| `TOPIC_SOURCE_ID` | ✓ | ID топика источника (thread_id) |
| `GCP_PROJECT` | ✓ | GCP project ID |
| `PUBSUB_TOPIC` | ✓ | Pub/Sub topic name |
| `ENABLE_WEBHOOK_SETUP` | — | `true` чтобы вызвать setWebhook при старте (default: `false`) |
| `BOT_URL` | — | Публичный URL бота (нужен если `ENABLE_WEBHOOK_SETUP=true`) |
| `PORT` | — | HTTP порт (default: `8080`) |

### `photo-convert-worker`

| Переменная | Обязательная | Описание |
|---|---|---|
| `BOT_TOKEN` | ✓ | Telegram bot token |
| `CHAT_ID` | ✓ | ID чата |
| `TOPIC_CONVERTED_ID` | ✓ | ID топика для результатов (thread_id) |
| `CONVERTER_URL` | ✓ | URL converter-сервиса |
| `CONVERTER_API_KEY` | ✓ | API ключ для converter |
| `MAX_FILE_MB` | — | Максимальный размер файла (default: `40`) |
| `CONVERSION_TIMEOUT_SECONDS` | — | Таймаут запроса к converter (default: `600`) |
| `CONVERSION_QUALITY` | — | JPEG quality 1–100 (default: `92`) |

### `photo-converter`

| Переменная | Обязательная | Описание |
|---|---|---|
| `CONVERTER_API_KEY` | ✓ | API ключ (заголовок `X-API-KEY`) |
| `MAX_FILE_MB` | — | Максимальный размер файла (default: `40`) |
| `SUBPROCESS_TIMEOUT_SECONDS` | — | Таймаут внешних процессов (default: `90`) |
| `MAGICK_TIMEOUT_SECONDS` | — | Таймаут ImageMagick (default: `90`) |
| `DCRAW_TIMEOUT_SECONDS` | — | Таймаут dcraw/dcraw_emu (default: `120`) |
| `DARKTABLE_TIMEOUT_SECONDS` | — | Таймаут darktable-cli (default: `180`) |

---

## Cloud Run конфигурация

### `photo-convert-bot`
- `min-instances=1`, `startup-cpu-boost=true`, `cpu-throttling=false`
- `cpu=2`, `memory=1Gi`

### `photo-convert-worker`
- `min-instances=0`, **`max-instances=1`**, `startup-cpu-boost=true`
- `cpu=1`, `memory=1Gi`

> `max-instances=1` обязателен — дедупликация заданий (`_processed_jobs`) in-memory и не расшаривается между инстансами.

### Pub/Sub подписка (`tg-convert-jobs-push`)
- `min-retry-delay=30s`, `max-retry-delay=300s`
- `ackDeadlineSeconds=600`

30s retry delay нужен для cold start воркера: при `min-instances=0` инстанс поднимается ~10–15s, retry через 30s уже попадёт в живой инстанс.

Применить вручную:
```bash
gcloud pubsub subscriptions update tg-convert-jobs-push \
  --project=<GCP_PROJECT> \
  --min-retry-delay=30s \
  --max-retry-delay=300s
```

---

## Deploy (GitHub Actions)

Workflow: `.github/workflows/deploy-photo-converter-bot.yml`
Запуск: **Actions → Deploy photo-converter monorepo → Run workflow**

### GitHub Secrets

- `GCP_PROJECT`, `GCP_WIF_PROVIDER`, `GCP_SA_EMAIL`, `GCP_REGION`
- `CLOUD_RUN_CONVERTER_SERVICE`, `CLOUD_RUN_BOT_SERVICE`, `CLOUD_RUN_WORKER_SERVICE`
- `CONVERTER_API_KEY`, `BOT_TOKEN`, `TG_WEBHOOK_SECRET`

### GitHub Variables

- `ALLOWED_EDITORS`, `CHAT_ID`, `TOPIC_SOURCE_ID`, `TOPIC_CONVERTED_ID`
- `GCP_PUBSUB_TOPIC`, `MAX_FILE_MB` (опционально)

---

## Локальный запуск

### Converter

```bash
cd converter
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export CONVERTER_API_KEY=secret
uvicorn app:app --reload --port 8080
```

### Worker

```bash
cd worker
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export BOT_TOKEN=<TOKEN>
export CHAT_ID=-100123456
export TOPIC_CONVERTED_ID=11
export CONVERTER_URL=http://localhost:8080
export CONVERTER_API_KEY=secret
uvicorn main:app --reload --port 8081
```

---

## Troubleshooting

### Воркер не обрабатывает задания (cold start burst)

Симптом: в логах `photo-convert-worker` много `"no available instance"` за ~30 секунд.

Причина: `min-instances=0`, Pub/Sub не ждёт пока инстанс поднимется.

Проверьте retry policy подписки:
```bash
gcloud pubsub subscriptions describe tg-convert-jobs-push \
  --project=<GCP_PROJECT> --format="yaml(retryPolicy)"
# Ожидаемо: minimumBackoff: 30s, maximumBackoff: 300s
```

### 401 от webhook бота

1. `TG_WEBHOOK_SECRET` в env совпадает с тем что передано в `setWebhook`.
2. Telegram шлёт заголовок `X-Telegram-Bot-Api-Secret-Token`.

### 403/401 при вызове converter

1. Worker отправляет правильный `X-API-KEY`.
2. `CONVERTER_API_KEY` одинаковый в worker и converter.
3. Converter публичный: `allUsers` + `roles/run.invoker`.

### 422 при конвертации RAW

```bash
gcloud logging read \
  "resource.labels.service_name=photo-converter AND severity>=WARNING" \
  --project=<GCP_PROJECT> --limit=20
```

Проверьте `raw_step=* status=fail` в логах — цепочка декодеров пишет причину каждого шага.
