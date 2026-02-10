#!/bin/bash
# Script to verify GCP configuration and generate correct secret values

set -euo pipefail

echo "=== GCP Configuration Verification ==="
echo ""

# Get user input
read -p "Enter your GCP Project ID: " PROJECT_ID
read -p "Enter your GCP Region (e.g., us-central1): " REGION

echo ""
echo "=== Verifying GCP Project ==="
if gcloud projects describe "$PROJECT_ID" &>/dev/null; then
  echo "✅ Project exists: $PROJECT_ID"
  PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")
  echo "   Project Number: $PROJECT_NUMBER"
else
  echo "❌ Project not found: $PROJECT_ID"
  exit 1
fi

echo ""
echo "=== Checking Required APIs ==="
required_apis=(
  "artifactregistry.googleapis.com"
  "run.googleapis.com"
  "iamcredentials.googleapis.com"
  "sts.googleapis.com"
)

for api in "${required_apis[@]}"; do
  if gcloud services list --enabled --project="$PROJECT_ID" --filter="name:$api" --format="value(name)" | grep -q "$api"; then
    echo "✅ $api"
  else
    echo "❌ $api (NOT ENABLED)"
    read -p "   Enable this API? (y/n): " enable
    if [[ "$enable" == "y" ]]; then
      gcloud services enable "$api" --project="$PROJECT_ID"
      echo "   ✅ Enabled"
    fi
  fi
done

echo ""
echo "=== Checking Service Account ==="
SA_NAME="github-actions-deployer"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

if gcloud iam service-accounts describe "$SA_EMAIL" --project="$PROJECT_ID" &>/dev/null; then
  echo "✅ Service account exists: $SA_EMAIL"
else
  echo "❌ Service account not found: $SA_EMAIL"
  read -p "   Create service account? (y/n): " create
  if [[ "$create" == "y" ]]; then
    gcloud iam service-accounts create "$SA_NAME" \
      --display-name="GitHub Actions Deployer" \
      --project="$PROJECT_ID"
    echo "   ✅ Created"
  else
    exit 1
  fi
fi

echo ""
echo "=== Checking Service Account Permissions ==="
required_roles=(
  "roles/artifactregistry.writer"
  "roles/run.admin"
  "roles/iam.serviceAccountUser"
)

for role in "${required_roles[@]}"; do
  if gcloud projects get-iam-policy "$PROJECT_ID" \
      --flatten="bindings[].members" \
      --filter="bindings.members:serviceAccount:${SA_EMAIL} AND bindings.role:${role}" \
      --format="value(bindings.role)" | grep -q "$role"; then
    echo "✅ $role"
  else
    echo "❌ $role (NOT GRANTED)"
    read -p "   Grant this role? (y/n): " grant
    if [[ "$grant" == "y" ]]; then
      gcloud projects add-iam-policy-binding "$PROJECT_ID" \
        --member="serviceAccount:${SA_EMAIL}" \
        --role="$role" \
        --quiet
      echo "   ✅ Granted"
    fi
  fi
done

echo ""
echo "=== Checking Workload Identity Pool ==="
POOL_NAME="github-actions-pool"
PROVIDER_NAME="github-actions-provider"

if gcloud iam workload-identity-pools describe "$POOL_NAME" \
    --location="global" \
    --project="$PROJECT_ID" &>/dev/null; then
  echo "✅ Workload Identity Pool exists: $POOL_NAME"
else
  echo "❌ Workload Identity Pool not found: $POOL_NAME"
  read -p "   Create pool? (y/n): " create_pool
  if [[ "$create_pool" == "y" ]]; then
    gcloud iam workload-identity-pools create "$POOL_NAME" \
      --location="global" \
      --display-name="GitHub Actions Pool" \
      --project="$PROJECT_ID"
    echo "   ✅ Created"
  else
    exit 1
  fi
fi

echo ""
echo "=== Checking Workload Identity Provider ==="
if gcloud iam workload-identity-pools providers describe "$PROVIDER_NAME" \
    --workload-identity-pool="$POOL_NAME" \
    --location="global" \
    --project="$PROJECT_ID" &>/dev/null; then
  echo "✅ Workload Identity Provider exists: $PROVIDER_NAME"
else
  echo "❌ Workload Identity Provider not found: $PROVIDER_NAME"
  read -p "   Enter your GitHub username or org: " GITHUB_OWNER
  read -p "   Create provider? (y/n): " create_provider
  if [[ "$create_provider" == "y" ]]; then
    gcloud iam workload-identity-pools providers create-oidc "$PROVIDER_NAME" \
      --location="global" \
      --workload-identity-pool="$POOL_NAME" \
      --display-name="GitHub Actions Provider" \
      --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
      --attribute-condition="assertion.repository_owner == '${GITHUB_OWNER}'" \
      --issuer-uri="https://token.actions.githubusercontent.com" \
      --project="$PROJECT_ID"
    echo "   ✅ Created"
  else
    exit 1
  fi
fi

echo ""
echo "=== Getting WIF Provider Resource Name ==="
WIF_PROVIDER=$(gcloud iam workload-identity-pools providers describe "$PROVIDER_NAME" \
  --workload-identity-pool="$POOL_NAME" \
  --location="global" \
  --project="$PROJECT_ID" \
  --format="value(name)")
echo "✅ $WIF_PROVIDER"

echo ""
echo "=== Checking Service Account Impersonation ==="
read -p "Enter your GitHub repository (e.g., username/repo-name): " GITHUB_REPO

WORKLOAD_IDENTITY_POOL_ID="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_NAME}/providers/${PROVIDER_NAME}"
MEMBER="principalSet://iam.googleapis.com/${WORKLOAD_IDENTITY_POOL_ID}/attribute.repository/${GITHUB_REPO}"

if gcloud iam service-accounts get-iam-policy "$SA_EMAIL" \
    --project="$PROJECT_ID" \
    --flatten="bindings[].members" \
    --filter="bindings.members:${MEMBER}" \
    --format="value(bindings.role)" | grep -q "roles/iam.workloadIdentityUser"; then
  echo "✅ Service account impersonation configured"
else
  echo "❌ Service account impersonation NOT configured"
  read -p "   Configure impersonation? (y/n): " configure
  if [[ "$configure" == "y" ]]; then
    gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
      --role="roles/iam.workloadIdentityUser" \
      --member="$MEMBER" \
      --project="$PROJECT_ID"
    echo "   ✅ Configured"
  fi
fi

echo ""
echo "=== Checking Artifact Registry ==="
AR_REPO="orochimary"

if gcloud artifacts repositories describe "$AR_REPO" \
    --location="$REGION" \
    --project="$PROJECT_ID" &>/dev/null; then
  echo "✅ Artifact Registry repository exists: $AR_REPO"
else
  echo "❌ Artifact Registry repository not found: $AR_REPO"
  read -p "   Create repository? (y/n): " create_ar
  if [[ "$create_ar" == "y" ]]; then
    gcloud artifacts repositories create "$AR_REPO" \
      --repository-format=docker \
      --location="$REGION" \
      --description="Docker images for photo converter bot" \
      --project="$PROJECT_ID"
    echo "   ✅ Created"
  fi
fi

echo ""
echo "=== Checking Cloud Run Services ==="
CONVERTER_SERVICE="photo-converter"
BOT_SERVICE="photo-convert-bot"

for service in "$CONVERTER_SERVICE" "$BOT_SERVICE"; do
  if gcloud run services describe "$service" \
      --region="$REGION" \
      --project="$PROJECT_ID" &>/dev/null; then
    echo "✅ Cloud Run service exists: $service"
  else
    echo "⚠️  Cloud Run service not found: $service"
    echo "   (Will be created on first deployment)"
  fi
done

echo ""
echo "=========================================="
echo "=== GitHub Secrets Configuration ==="
echo "=========================================="
echo ""
echo "Copy these values to your GitHub repository secrets:"
echo ""
echo "GCP_PROJECT:"
echo "  $PROJECT_ID"
echo ""
echo "GCP_REGION:"
echo "  $REGION"
echo ""
echo "GCP_SA_EMAIL:"
echo "  $SA_EMAIL"
echo ""
echo "GCP_WIF_PROVIDER:"
echo "  $WIF_PROVIDER"
echo ""
echo "CLOUD_RUN_CONVERTER_SERVICE:"
echo "  $CONVERTER_SERVICE"
echo ""
echo "CLOUD_RUN_BOT_SERVICE:"
echo "  $BOT_SERVICE"
echo ""
echo "=========================================="
echo ""
echo "✅ Verification complete!"
echo ""
echo "Next steps:"
echo "1. Update GitHub secrets with the values above"
echo "2. Ensure CONVERTER_API_KEY and TELEGRAM_BOT_TOKEN are set"
echo "3. Run the GitHub Actions workflow"
