# Converter Bot Backend

Монорепозиторий с двумя Cloud Run сервисами для конвертации фото в JPEG через Telegram.

---

## Архитектура

```
Telegram → photo-convert-bot → Pub/Sub → photo-converter → Telegram (result)
```

1. **`photo-convert-bot`** — aiohttp webhook-сервер. Принимает Telegram-апдейты, проверяет пользователя (`ALLOWED_EDITORS`) и чат/топик, публикует задание в Pub/Sub.
2. **`photo-converter`** — FastAPI сервис с двумя входами:
   - `POST /pubsub/push` — реальный путь заданий: скачивает файл из Telegram напрямую, конвертирует, загружает JPG обратно в Telegram.
   - `POST /convert` — ручное HTTP API (multipart) для тестов, за `X-API-KEY`.

   Раньше между bot и converter стоял отдельный `photo-convert-worker`, который скачивал файл и пересылал те же байты в converter по HTTP — RAW-файл (может быть 20-40MB) дважды проходил по сети до конвертации. Worker слили в converter, чтобы файл шёл по сети один раз в каждую сторону и не было двойного cold start на пачку.

### Поддерживаемые форматы

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

### `photo-converter`

| Переменная | Обязательная | Описание |
|---|---|---|
| `BOT_TOKEN` | ✓ | Telegram bot token (для скачивания/загрузки файлов в `/pubsub/push`) |
| `CHAT_ID` | ✓ | ID чата, куда шлётся результат |
| `TOPIC_CONVERTED_ID` | ✓ | ID топика для результатов (thread_id) |
| `CONVERTER_API_KEY` | ✓ | API ключ для `/convert` (заголовок `X-API-KEY`) |
| `MAX_FILE_MB` | — | Максимальный размер файла для `/convert` (default: `40`) |
| `CONVERSION_QUALITY` | — | JPEG quality 1–100 для job-пайплайна (default: `92`) |
| `SUBPROCESS_TIMEOUT_SECONDS` | — | Таймаут внешних процессов (default: `90`) |
| `EXIFTOOL_PREVIEW_TIMEOUT_SECONDS` | — | Таймаут извлечения embedded-превью из RAW, до 3 попыток подряд (default: `20`) |
| `MAGICK_TIMEOUT_SECONDS` | — | Таймаут ImageMagick (default: `90`) |
| `DCRAW_TIMEOUT_SECONDS` | — | Таймаут dcraw/dcraw_emu (default: `120`) |
| `DARKTABLE_TIMEOUT_SECONDS` | — | Таймаут darktable-cli (default: `180`) |

---

## Cloud Run конфигурация

### `photo-convert-bot`
- `min-instances=0`, `max-instances=10`, `cpu-throttling=true`
- `cpu=1`, `memory=512Mi`, `concurrency=30`

### `photo-converter`
- `min-instances=0` (осознанный выбор — сервис используется нечасто, но пачками по 500+ файлов; холодный старт дешевле, чем держать инстанс постоянно)
- `max-instances` — управляется GitHub-переменной `CONVERTER_MAX_INSTANCES` (default `20`)
- `cpu=2`, `memory=4Gi`, **`concurrency=1`** (RAW-декодирование — CPU-bound subprocess, конкурентность внутри инстанса не даст выигрыша)
- `timeout=600`

> Раз `concurrency=1` и работа CPU-bound, единственный рычаг пропускной способности — число инстансов (`max-instances`). Именно его надо поднимать при пачках в сотни файлов, а не concurrency.

> Дедупликация заданий (`_processed_jobs`) — in-memory на инстанс, не расшарена между инстансами. При нескольких параллельных инстансах (что теперь норма при бурстах) повторная доставка от Pub/Sub может не распознаться как дубликат, если попадёт на другой инстанс. Для нечастого личного бота это приемлемый компромисс; если объём вырастет — нужно вынести в Firestore/Redis.

### Pub/Sub подписка (`tg-convert-jobs-push`)
- `ackDeadlineSeconds=600`
- `min-retry-delay` / `max-retry-delay` — учитывайте cold start converter'а (~10–15s к min-instances=0): слишком короткий retry delay попадёт в ещё не поднявшийся инстанс и снова получит "no available instance".

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
- `CLOUD_RUN_CONVERTER_SERVICE`, `CLOUD_RUN_BOT_SERVICE`
- `CONVERTER_API_KEY`, `BOT_TOKEN`, `TG_WEBHOOK_SECRET`

### GitHub Variables

- `ALLOWED_EDITORS`, `CHAT_ID`, `TOPIC_SOURCE_ID`, `TOPIC_CONVERTED_ID`
- `PUBSUB_TOPIC`, `MAX_FILE_MB`, `CONVERTER_MAX_INSTANCES`, `CONVERSION_QUALITY` (опционально)

После первого деплоя converter'а (или при смене URL) перезапустить `scripts/setup-pubsub.sh` с новым `CONVERTER_SERVICE_URL`.

---

## Локальный запуск

### Converter (обслуживает и `/convert`, и `/pubsub/push`)

```bash
cd converter
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export CONVERTER_API_KEY=secret
export BOT_TOKEN=<TOKEN>
export CHAT_ID=-100123456
export TOPIC_CONVERTED_ID=11
uvicorn app:app --reload --port 8080
```

---

## Troubleshooting

### "The request was aborted because there was no available instance"

Симптом: в логах `photo-converter` много таких ошибок при пачковой загрузке.

Причина: `min-instances=0` + `concurrency=1`, а размер пачки превысил `max-instances`. Дополнительно cold start (~10-15s) съедает время до того, как новый инстанс станет доступен.

Проверить/поднять лимит:
```bash
gcloud run services describe <CLOUD_RUN_CONVERTER_SERVICE> \
  --region=<GCP_REGION> --project=<GCP_PROJECT> \
  --format="value(spec.template.metadata.annotations['autoscaling.knative.dev/maxScale'])"
```
Поднять через переменную `CONVERTER_MAX_INSTANCES` и передеплоить, либо вручную:
```bash
gcloud run services update <CLOUD_RUN_CONVERTER_SERVICE> \
  --region=<GCP_REGION> --project=<GCP_PROJECT> \
  --max-instances=40
```

Также проверьте retry policy подписки — она должна давать инстансу время подняться:
```bash
gcloud pubsub subscriptions describe tg-convert-jobs-push \
  --project=<GCP_PROJECT> --format="yaml(retryPolicy)"
```

### 401 от webhook бота

1. `TG_WEBHOOK_SECRET` в env совпадает с тем что передано в `setWebhook`.
2. Telegram шлёт заголовок `X-Telegram-Bot-Api-Secret-Token`.

### 401 при ручном вызове `/convert`

1. Заголовок `X-API-KEY` передан и совпадает с `CONVERTER_API_KEY` на сервисе.

### 422 при конвертации RAW

```bash
gcloud logging read \
  "resource.labels.service_name=photo-converter AND severity>=WARNING" \
  --project=<GCP_PROJECT> --limit=20
```

Проверьте `raw_step=* status=fail` в логах — цепочка декодеров пишет причину каждого шага.
