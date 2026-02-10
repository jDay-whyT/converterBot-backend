# Converter Bot Backend

Telegram-бот для конвертации входных фото-документов в JPG через отдельный converter service.

## Структура

- `bot/` — aiogram 3.x бот
- `converter/` — FastAPI сервис конвертации (Cloud Run)

## Поддерживаемые форматы

Вход: `.heic`, `.dng`, `.webp`, `.tif`, `.tiff`  
Выход: `.jpg`

По умолчанию:
- `quality=92`
- `-auto-orient`
- `-colorspace sRGB`
- `-strip`

Опционально: `max_side` для ресайза по большей стороне.

---

## Converter (Cloud Run)

### API

`POST /convert`

- multipart field `file`
- optional form fields:
  - `quality` (int, default 92)
  - `max_side` (int, optional)
- header: `X-API-KEY: <CONVERTER_API_KEY>`
- response: `image/jpeg` bytes

### Локальный запуск

```bash
cd converter
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export CONVERTER_API_KEY=secret
uvicorn app:app --reload --port 8080
```

### Деплой в Cloud Run

```bash
cd converter
gcloud builds submit --tag gcr.io/<PROJECT_ID>/converter-bot
gcloud run deploy converter-bot \
  --image gcr.io/<PROJECT_ID>/converter-bot \
  --region <REGION> \
  --platform managed \
  --allow-unauthenticated \
  --set-env-vars CONVERTER_API_KEY=<SECRET>,MAX_FILE_MB=40
```

> Рекомендуется ограничить вызовы по сети/VPC/IAM, даже при API key.

---

## Bot (aiogram)

### Env vars

- `BOT_TOKEN`
- `ALLOWED_EDITORS` — comma-separated user IDs
- `CHAT_ID`
- `TOPIC_SOURCE_ID`
- `TOPIC_CONVERTED_ID`
- `CONVERTER_URL`
- `CONVERTER_API_KEY`
- `MAX_FILE_MB` (default `40`)
- `BATCH_WINDOW_SECONDS` (default `120`)
- `PROGRESS_UPDATE_EVERY` (default `3`)

### Локальный запуск

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

## Поведение бота

- Принимает **только document** в нужном `CHAT_ID + TOPIC_SOURCE_ID`
- Проверяет `user_id` в `ALLOWED_EDITORS`
- Формирует пачку по окну `BATCH_WINDOW_SECONDS` для одного пользователя
- Ведёт **одно progress-сообщение** на пачку и редактирует каждые `PROGRESS_UPDATE_EVERY` файлов
- Ошибки отдельных файлов не останавливают остальную обработку
- Отправляет результат в `TOPIC_CONVERTED_ID` отдельными документами (`.jpg`) без ZIP
- Сохраняет базовое имя (`same stem + .jpg`)

## Тесты

```bash
cd bot
PYTHONPATH=. python -m unittest discover -s tests -p 'test_*.py' -v
```
