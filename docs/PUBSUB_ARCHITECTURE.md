# Pub/Sub Architecture for converterBot

## Overview

The bot uses Google Cloud Pub/Sub for asynchronous, reliable processing of conversion jobs. This architecture ensures:

- **Stability at min=0**: Webhook doesn't drop when bot scales to zero
- **No lost jobs**: Pub/Sub queues jobs and retries on failures
- **Burst tolerance**: Cloud Run scales converter instances to absorb large batches (500+ files)
- **RAW support**: Full support for ARW and other RAW formats

## Architecture

```
Telegram -> Bot (webhook) -> Pub/Sub Topic -> Converter
```

The former standalone `worker` service was merged into `converter` (see git history
prior to this merge) so a file's bytes cross the network exactly once each way
(Telegram -> converter, converter -> Telegram), instead of being relayed twice
(Telegram -> worker -> converter, and back). This also halves the number of
services that can cold-start per burst.

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

3. **Converter (photo-converter)**
   - Receives push from Pub/Sub at `/pubsub/push`
   - Downloads file from Telegram directly (via aiogram `Bot`)
   - Converts RAW/HEIC/WebP/TIFF to JPEG in-process
   - Supports ARW, DNG, CR2, CR3, NEF, RAF, etc.
   - Uploads result to Telegram
   - Idempotent by `file_unique_id` (in-memory per instance — see Known limitations)
   - Also exposes `/convert` (multipart HTTP) for manual testing, guarded by `CONVERTER_API_KEY`

## Known limitations

- Idempotency dedup (`_processed_jobs`) lives in each instance's memory, not a
  shared store. Under Cloud Run autoscaling, a Pub/Sub retry that lands on a
  different instance (or a recycled one) will not be recognized as a duplicate.
  Acceptable for a low-volume personal bot; would need Firestore/Redis if that changes.

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
- `CLOUD_RUN_CONVERTER_SERVICE`

GitHub Actions variables:
- `BOT_URL`
- `ALLOWED_EDITORS`
- `CHAT_ID`
- `TOPIC_SOURCE_ID`
- `TOPIC_CONVERTED_ID`
- `PUBSUB_TOPIC` (optional, default: `tg-convert-jobs`)
- `CONVERTER_MAX_INSTANCES` (optional, default: `20`)
- `CONVERSION_QUALITY` (optional, default: `92`)

### Step 1: Deploy Converter + Bot

```bash
gh workflow run deploy-photo-converter-bot.yml
```

This deploys:
- Converter service (min=0, max=20, cpu=2, mem=4Gi, concurrency=1, timeout=600s) —
  handles both `/convert` (manual HTTP testing) and `/pubsub/push` (the real job path)
- Bot service (min=0, max=10, cpu=1, mem=512Mi, concurrency=30)
- With `ENABLE_WEBHOOK_SETUP=false`

### Step 2: Setup Pub/Sub Infrastructure

```bash
export GCP_PROJECT="your-project"
export GCP_REGION="us-central1"
export CONVERTER_SERVICE_URL="https://photo-converter-xxx.run.app"

./scripts/setup-pubsub.sh
```

This creates:
- Pub/Sub topic: `tg-convert-jobs`
- Push subscription to the converter's `/pubsub/push` endpoint

### Step 3: Setup Telegram Webhook (Manual, Once)

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

# Converter logs (also handles job processing)
gcloud run services logs read photo-converter --region=$GCP_REGION --project=$GCP_PROJECT
```

## Testing

1. Send a test file (HEIC, ARW, DNG, etc.) to the bot
2. Check bot logs: should see `pubsub_published`
3. Check converter logs: should see `job_success`
4. Check Telegram: converted file should appear in target topic

## Troubleshooting

### Webhook keeps getting reset

- Ensure `ENABLE_WEBHOOK_SETUP=false` in bot deployment
- Only run `./scripts/setup-webhook.sh` manually once

### Jobs not processing

- Check Pub/Sub subscription status
- Verify converter is deployed and accessible at `/pubsub/push`
- Check converter logs for errors

### "The request was aborted because there was no available instance"

- This means Cloud Run ran out of converter instances for the burst size —
  raise `CONVERTER_MAX_INSTANCES` (GitHub variable) and redeploy
- Since `concurrency=1` (CPU-bound RAW decoding), only `max-instances` controls
  how many files convert in parallel — do not raise concurrency instead

### High pending_update_count

- Check Pub/Sub dead letter queue
- Verify converter's `max-instances` and timeout settings
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

### Converter
- min-instances: 0
- max-instances: 20 (raise via `CONVERTER_MAX_INSTANCES` for larger bursts)
- cpu: 2
- memory: 4Gi
- concurrency: 1 (CPU-bound RAW decoding — scale via instance count, not concurrency)
- timeout: 600s
- cpu-throttling: true
