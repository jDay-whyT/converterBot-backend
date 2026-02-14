# Pub/Sub Architecture for converterBot

## Overview

The bot now uses Google Cloud Pub/Sub for asynchronous, reliable processing of conversion jobs. This architecture ensures:

- **Stability at min=0**: Webhook doesn't drop when bot scales to zero
- **No lost jobs**: Pub/Sub queues jobs and retries on failures
- **Batch processing**: Workers can process 30+ files concurrently with retries
- **RAW support**: Full support for ARW and other RAW formats

## Architecture

```
Telegram -> Bot (webhook) -> Pub/Sub Topic -> Worker -> Converter
                                    |
                                    v
                              Cloud Storage (optional)
```

### Components

1. **Bot (photo-convert-bot)**
   - Receives webhook from Telegram
   - Validates secret token
   - Publishes job to Pub/Sub topic
   - Returns 200 OK immediately

2. **Pub/Sub**
   - Topic: `tg-convert-jobs`
   - Push Subscription: `tg-convert-jobs-push`
   - Ack deadline: 600s
   - Retry: 10s - 600s exponential backoff

3. **Worker (photo-convert-worker)**
   - Receives push from Pub/Sub
   - Downloads file from Telegram
   - Converts via converter service
   - Uploads result to Telegram
   - Idempotent by `file_unique_id`

4. **Converter (photo-converter)**
   - Converts RAW/HEIC/WebP to JPEG
   - Supports ARW, DNG, CR2, CR3, NEF, RAF, etc.

## Deployment

### Prerequisites

- GCP project with billing enabled
- Cloud Run API enabled
- Pub/Sub API enabled
- Artifact Registry repository created

### Required Secrets

GitHub Actions secrets:
- `GCP_PROJECT`
- `GCP_WIF_PROVIDER`
- `GCP_SA_EMAIL`
- `GCP_REGION`
- `BOT_TOKEN` or `TELEGRAM_BOT_TOKEN`
- `TG_WEBHOOK_SECRET` (Secret Manager version 2)
- `CONVERTER_API_KEY`
- `CLOUD_RUN_BOT_SERVICE`
- `CLOUD_RUN_WORKER_SERVICE`
- `CLOUD_RUN_CONVERTER_SERVICE`

GitHub Actions variables:
- `BOT_URL`
- `ALLOWED_EDITORS`
- `CHAT_ID`
- `TOPIC_SOURCE_ID`
- `TOPIC_CONVERTED_ID`
- `PUBSUB_TOPIC` (optional, default: `tg-convert-jobs`)

### Step 1: Deploy Converter

```bash
gh workflow run deploy-photo-converter-bot.yml
```

This deploys:
- Converter service (min=0, max=3, cpu=2, mem=4Gi, timeout=600s)

### Step 2: Deploy Worker

```bash
gh workflow run deploy-worker.yml
```

This deploys:
- Worker service (min=0, max=2, cpu=1, mem=1Gi, timeout=600s, concurrency=1)

### Step 3: Setup Pub/Sub Infrastructure

```bash
export GCP_PROJECT="your-project"
export GCP_REGION="us-central1"
export WORKER_SERVICE_URL="https://worker-service-xxx.run.app"

./scripts/setup-pubsub.sh
```

This creates:
- Pub/Sub topic: `tg-convert-jobs`
- Push subscription to worker endpoint

### Step 4: Deploy Bot

Update GitHub variables if needed, then:

```bash
gh workflow run deploy-photo-converter-bot.yml
```

This deploys:
- Bot service (min=0, max=10, cpu=1, mem=512Mi, concurrency=30)
- With `ENABLE_WEBHOOK_SETUP=false`

### Step 5: Setup Telegram Webhook (Manual, Once)

```bash
export BOT_TOKEN="your-bot-token"
export BOT_URL="https://bot-service-xxx.run.app"
export TG_WEBHOOK_SECRET="your-secret"

./scripts/setup-webhook.sh
```

This configures Telegram to send updates to your bot webhook.

## Monitoring

### Check Pub/Sub metrics

```bash
gcloud pubsub topics describe tg-convert-jobs --project=$GCP_PROJECT
gcloud pubsub subscriptions describe tg-convert-jobs-push --project=$GCP_PROJECT
```

### Check pending messages

```bash
gcloud pubsub subscriptions pull tg-convert-jobs-push --limit=1 --project=$GCP_PROJECT
```

### Check webhook status

```bash
curl "https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo" | jq .
```

### View logs

```bash
# Bot logs
gcloud run services logs read photo-convert-bot --region=$GCP_REGION --project=$GCP_PROJECT

# Worker logs
gcloud run services logs read photo-convert-worker --region=$GCP_REGION --project=$GCP_PROJECT

# Converter logs
gcloud run services logs read photo-converter --region=$GCP_REGION --project=$GCP_PROJECT
```

## Testing

1. Send a test file (HEIC, ARW, DNG, etc.) to the bot
2. Check bot logs: should see `pubsub_published`
3. Check worker logs: should see `job_success`
4. Check Telegram: converted file should appear in target topic

## Troubleshooting

### Webhook keeps getting reset

- Ensure `ENABLE_WEBHOOK_SETUP=false` in bot deployment
- Only run `./scripts/setup-webhook.sh` manually once

### Jobs not processing

- Check Pub/Sub subscription status
- Verify worker is deployed and accessible
- Check worker logs for errors

### High pending_update_count

- Check Pub/Sub dead letter queue
- Verify worker concurrency and timeout settings
- Check for stuck messages

### ARW files not converting

- Check converter logs for errors
- Verify `dcraw_emu` or `darktable-cli` is available in converter Docker image
- Test with smaller ARW file first

## Cloud Run Configuration

### Bot
- min-instances: 0
- max-instances: 10
- cpu: 1
- memory: 512Mi
- concurrency: 30
- timeout: default (300s)
- cpu-throttling: true

### Worker
- min-instances: 0
- max-instances: 2
- cpu: 1
- memory: 1Gi
- concurrency: 1 (sequential processing)
- timeout: 600s
- cpu-throttling: true

### Converter
- min-instances: 0
- max-instances: 3
- cpu: 2
- memory: 4Gi
- concurrency: 1
- timeout: 600s
- cpu-throttling: true
