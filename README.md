# Converter Bot Backend

Монорепозиторий с двумя Cloud Run сервисами:

1. **`photo-converter`** — HTTP API `POST /convert` (FastAPI + ImageMagick/dcraw).
   - **RAW поддержка** через LibRaw (`dcraw_emu`) с fallback на `dcraw`:
     - **DNG** (включая Apple ProRAW), CR2, CR3, NEF, NRW, ARW, RAF, RW2, ORF, PEF, SRW, X3F, 3FR, IIQ, DCR, KDC, MRW
     - LibRaw 0.21.2 собирается из исходников для максимальной совместимости с ProRAW
     - Fallback на `dcraw` для базовых RAW форматов
   - **HEIF/HEIC поддержка** через `libheif` (`.heic`, `.heif`)
2. **`photo-convert-bot`** — Telegram-бот на aiogram (polling), который отправляет файлы в converter и публикует JPG в целевой топик.

---

## Overview / Architecture

### Зачем разделение на 2 сервиса

- **Изоляция ответственности:**
  - `photo-converter` занимается только конвертацией файлов.
  - `photo-convert-bot` занимается только Telegram polling, batching, прогрессом и отправкой результатов.
- **Независимый деплой:** можно выкатывать bot и converter отдельно в одном workflow.
- **Независимое масштабирование:** настройки Cloud Run и ресурсы можно задавать под профиль нагрузки каждого сервиса.

### Поток запроса

1. Пользователь отправляет `document` в source topic.
2. Бот проверяет доступ пользователя (`ALLOWED_EDITORS`) и что сообщение пришло в нужный чат/топик.
3. Бот скачивает файл из Telegram, вызывает `${CONVERTER_URL}/convert` с заголовком `X-API-KEY`.
4. Converter валидирует `X-API-KEY`, расширение, размер файла и возвращает `image/jpeg`.
5. Бот отправляет JPG в converted topic и обновляет batched progress.

### Аутентификация между сервисами (prod)

- Converter сервис **публичный на уровне Cloud Run IAM** (`allUsers` + `roles/run.invoker`).
- Дополнительная (и обязательная) защита — заголовок `X-API-KEY` со значением `CONVERTER_API_KEY`.
- **Cloud Run ID-token auth не используется** в этой архитектуре.

---

## Env vars

Ниже только переменные, которые реально используются кодом и/или workflow.

### `photo-convert-bot`

Обязательные:

- `BOT_TOKEN`
- `ALLOWED_EDITORS` (поддерживаются разделители: `,`, `|`, пробел)
- `CHAT_ID`
- `TOPIC_SOURCE_ID`
- `TOPIC_CONVERTED_ID`
- `CONVERTER_URL` (в workflow подставляется автоматически из URL converter-сервиса)
- `CONVERTER_API_KEY`

Опциональные:

- `MAX_FILE_MB` (по умолчанию `40`)
- `BATCH_WINDOW_SECONDS` (по умолчанию `120`)
- `PROGRESS_UPDATE_EVERY` (по умолчанию `3`)
- `PORT` (для health HTTP-сервера, по умолчанию `8080`)

### `photo-converter`

Обязательные:

- `CONVERTER_API_KEY`

Опциональные:

- `MAX_FILE_MB` (по умолчанию `40`)

---

## Deploy (GitHub Actions)

Деплой выполняется workflow:

- `.github/workflows/deploy-photo-converter-bot.yml`
- запуск: **Actions → Deploy photo-converter monorepo → Run workflow**

### Что нужно в GitHub Secrets

- `GCP_PROJECT`
- `GCP_WIF_PROVIDER`
- `GCP_SA_EMAIL`
- `GCP_REGION`
- `CLOUD_RUN_CONVERTER_SERVICE`
- `CLOUD_RUN_BOT_SERVICE`
- `CONVERTER_API_KEY`
- `BOT_TOKEN` **или** `TELEGRAM_BOT_TOKEN`

### Что нужно в GitHub Variables

- `ALLOWED_EDITORS`
- `CHAT_ID`
- `TOPIC_SOURCE_ID`
- `TOPIC_CONVERTED_ID`
- `MAX_FILE_MB` (опционально, иначе `40`)
- `BATCH_WINDOW_SECONDS` (опционально)
- `PROGRESS_UPDATE_EVERY` (опционально)

### Как бот получает URL converter-сервиса

В workflow есть шаг `Get converter URL`, который читает:

```bash
gcloud run services describe "${CLOUD_RUN_CONVERTER_SERVICE}" \
  --region="${GCP_REGION}" \
  --project="${GCP_PROJECT}" \
  --format='value(status.url)'
```

Результат пробрасывается в bot как `CONVERTER_URL`.

### Важные Cloud Run настройки для `photo-convert-bot` (prod)

Для снижения cold start и стабильного polling в проде сервис бота должен быть с настройками:

- `min-instances=1`
- `startup-cpu-boost=true`
- `cpu-throttling=false` (`--no-cpu-throttling`)
- ресурсы: `cpu=2`, `memory=1Gi`

Если нужно применить вручную:

```bash
gcloud run services update photo-convert-bot \
  --region="${GCP_REGION}" \
  --min-instances=1 \
  --cpu-boost \
  --no-cpu-throttling \
  --cpu=2 \
  --memory=1Gi
```

---

## Make converter public (Cloud Run IAM)

```bash
gcloud run services add-iam-policy-binding photo-converter \
  --region="${GCP_REGION}" \
  --member="allUsers" \
  --role="roles/run.invoker"
```

Проверка:

```bash
gcloud run services get-iam-policy photo-converter \
  --region="${GCP_REGION}" \
  --format='table(bindings.role, bindings.members)'
```

---

## Runtime notes (bot)

В боте реализовано:

- app-scoped `httpx.AsyncClient` с connection pool.
- тайминги этапов в логах: `tg_download`, `convert`, `tg_upload`, `total`.
- retry для Telegram API вызовов.
- debouncer progress-обновлений + защита от flood control при отправке файлов.

---

## Troubleshooting

### 403 при вызове converter

Проверьте:

1. Converter действительно публичный (`allUsers` + `roles/run.invoker`).
2. Бот отправляет корректный `X-API-KEY`.
3. В bot и converter одинаковое значение `CONVERTER_API_KEY`.

### Пачка «зависает» или обновляется рывками

Обычно это ограничения Telegram (flood control) при частых отправках/редактированиях.
В проекте уже есть retry и debouncer; при высокой нагрузке дополнительно проверьте объем/частоту входящих файлов.

### Медленная обработка

Частые причины в Cloud Run для бота:

- включен CPU throttling;
- недостаточный CPU (например, `cpu=1`);
- отсутствует `min-instances=1` (холодные старты).

### Ошибка "Cannot decode RAW (libraw/dcraw)" при обработке DNG/RAW

Если конвертация падает с 422 ошибкой для DNG или других RAW форматов:

1. **Проверьте наличие инструментов в контейнере:**
   ```bash
   docker exec <container> sh -c "which dcraw_emu && which dcraw"
   ```
   Должны быть установлены оба: `dcraw_emu` (из `libraw-bin`) и `dcraw` (fallback).

2. **Проверьте формат файла:**
   Некоторые DNG файлы (особенно Apple ProRAW) могут требовать больше памяти или времени на обработку.
   Убедитесь, что `MAX_FILE_MB` достаточно велик и контейнер имеет достаточно памяти.

3. **Логи декодирования:**
   Converter логирует все ошибки в stderr. Проверьте Cloud Run логи для деталей:
   ```bash
   gcloud run services logs read photo-converter --region="${GCP_REGION}" --limit=50
   ```

4. **Тестирование локально:**
   ```bash
   cd converter
   docker build -t converter-test .
   docker run -p 8080:8080 -e CONVERTER_API_KEY=test converter-test
   # В другом терминале:
   curl -F "file=@test.dng" -F "quality=92" -H "X-API-KEY: test" \
     http://localhost:8080/convert -o output.jpg
   ```

---

## Локальный запуск (кратко)

### Converter

```bash
cd converter
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export CONVERTER_API_KEY=secret
uvicorn app:app --reload --port 8080
```

### Bot

```bash
cd bot
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export BOT_TOKEN=<TOKEN>
export ALLOWED_EDITORS=1111111,2222222
export CHAT_ID=-100123456
export TOPIC_SOURCE_ID=10
export TOPIC_CONVERTED_ID=11
export CONVERTER_URL=https://<cloud-run-url>
export CONVERTER_API_KEY=<SECRET>
python main.py
```
