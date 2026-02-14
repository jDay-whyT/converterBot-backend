#!/bin/bash
set -euo pipefail

# Setup Telegram webhook (run once manually after deployment)

: "${BOT_TOKEN:?BOT_TOKEN must be set}"
: "${BOT_URL:?BOT_URL must be set (e.g., https://bot-service-xxx.run.app)}"
: "${TG_WEBHOOK_SECRET:?TG_WEBHOOK_SECRET must be set}"

WEBHOOK_URL="${BOT_URL}/telegram/webhook"

echo "=== Setting Telegram Webhook ==="
echo "Webhook URL: $WEBHOOK_URL"

response=$(curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook" \
  -H "Content-Type: application/json" \
  -d "{
    \"url\": \"${WEBHOOK_URL}\",
    \"secret_token\": \"${TG_WEBHOOK_SECRET}\",
    \"allowed_updates\": [\"message\"],
    \"drop_pending_updates\": false
  }")

echo "Response: $response"

# Check webhook info
echo ""
echo "=== Webhook Info ==="
curl -s "https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo" | jq .

echo ""
echo "=== Done ==="
echo "Webhook configured successfully!"
