#!/bin/bash
set -euo pipefail

# Setup Pub/Sub infrastructure for converterBot

# Required environment variables
: "${GCP_PROJECT:?GCP_PROJECT must be set}"
: "${GCP_REGION:?GCP_REGION must be set}"
: "${WORKER_SERVICE_URL:?WORKER_SERVICE_URL must be set (e.g., https://worker-service-xxx.run.app)}"

TOPIC_NAME="${PUBSUB_TOPIC:-tg-convert-jobs}"
SUBSCRIPTION_NAME="${PUBSUB_SUBSCRIPTION:-tg-convert-jobs-push}"
WORKER_SA="${WORKER_SERVICE_ACCOUNT:-}"

echo "=== Creating Pub/Sub Topic ==="
if gcloud pubsub topics describe "$TOPIC_NAME" --project="$GCP_PROJECT" &>/dev/null; then
  echo "Topic $TOPIC_NAME already exists"
else
  gcloud pubsub topics create "$TOPIC_NAME" \
    --project="$GCP_PROJECT"
  echo "Topic $TOPIC_NAME created"
fi

echo ""
echo "=== Creating Push Subscription ==="

PUSH_ENDPOINT="${WORKER_SERVICE_URL}/pubsub/push"

# Check if subscription exists
if gcloud pubsub subscriptions describe "$SUBSCRIPTION_NAME" --project="$GCP_PROJECT" &>/dev/null; then
  echo "Subscription $SUBSCRIPTION_NAME already exists"
  echo "To update it, delete first: gcloud pubsub subscriptions delete $SUBSCRIPTION_NAME --project=$GCP_PROJECT"
else
  # Create push subscription
  if [[ -n "$WORKER_SA" ]]; then
    echo "Creating subscription with OIDC authentication using service account: $WORKER_SA"
    gcloud pubsub subscriptions create "$SUBSCRIPTION_NAME" \
      --topic="$TOPIC_NAME" \
      --push-endpoint="$PUSH_ENDPOINT" \
      --push-auth-service-account="$WORKER_SA" \
      --ack-deadline=600 \
      --min-retry-delay=10s \
      --max-retry-delay=600s \
      --project="$GCP_PROJECT"
  else
    echo "Creating subscription without authentication (worker must allow unauthenticated)"
    gcloud pubsub subscriptions create "$SUBSCRIPTION_NAME" \
      --topic="$TOPIC_NAME" \
      --push-endpoint="$PUSH_ENDPOINT" \
      --ack-deadline=600 \
      --min-retry-delay=10s \
      --max-retry-delay=600s \
      --project="$GCP_PROJECT"
  fi
  echo "Subscription $SUBSCRIPTION_NAME created"
fi

echo ""
echo "=== Pub/Sub Setup Complete ==="
echo "Topic: $TOPIC_NAME"
echo "Subscription: $SUBSCRIPTION_NAME"
echo "Push endpoint: $PUSH_ENDPOINT"
echo ""
echo "Next steps:"
echo "1. Ensure worker service is deployed and accessible"
echo "2. Set PUBSUB_TOPIC=$TOPIC_NAME in bot environment variables"
echo "3. Test by sending a file to the bot"
