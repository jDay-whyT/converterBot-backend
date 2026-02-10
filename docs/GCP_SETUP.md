# Google Cloud Platform Setup Guide

This guide provides step-by-step instructions for configuring Google Cloud Platform and GitHub to enable automated deployments for the photo-converter monorepo.

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [GCP Project Setup](#gcp-project-setup)
3. [Workload Identity Federation Setup](#workload-identity-federation-setup)
4. [Artifact Registry Setup](#artifact-registry-setup)
5. [Cloud Run Setup](#cloud-run-setup)
6. [GitHub Secrets Configuration](#github-secrets-configuration)
7. [Troubleshooting](#troubleshooting)

## Prerequisites

- A Google Cloud Platform account with billing enabled
- A GitHub repository with admin access
- `gcloud` CLI installed (for local testing)
- Docker installed (for local testing)

## GCP Project Setup

### 1. Create or Select a GCP Project

```bash
# Create a new project
gcloud projects create YOUR_PROJECT_ID --name="Photo Converter Bot"

# Or list existing projects
gcloud projects list

# Set the project as default
gcloud config set project YOUR_PROJECT_ID
```

### 2. Enable Required APIs

```bash
# Enable necessary APIs
gcloud services enable \
  artifactregistry.googleapis.com \
  run.googleapis.com \
  iamcredentials.googleapis.com \
  cloudresourcemanager.googleapis.com \
  sts.googleapis.com
```

## Workload Identity Federation Setup

Workload Identity Federation (WIF) allows GitHub Actions to authenticate to GCP without storing long-lived service account keys.

### 1. Create a Service Account

```bash
# Create service account for deployments
gcloud iam service-accounts create github-actions-deployer \
  --display-name="GitHub Actions Deployer" \
  --description="Service account for GitHub Actions CI/CD"

# Get the service account email
export SA_EMAIL="github-actions-deployer@YOUR_PROJECT_ID.iam.gserviceaccount.com"
```

### 2. Grant Required Permissions

```bash
# Artifact Registry permissions
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/artifactregistry.writer"

# Cloud Run permissions
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/run.admin"

# Service Account User (required for Cloud Run deployments)
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/iam.serviceAccountUser"
```

### 3. Create Workload Identity Pool

```bash
# Create the workload identity pool
gcloud iam workload-identity-pools create "github-actions-pool" \
  --location="global" \
  --display-name="GitHub Actions Pool"

# Create the workload identity provider
gcloud iam workload-identity-pools providers create-oidc "github-actions-provider" \
  --location="global" \
  --workload-identity-pool="github-actions-pool" \
  --display-name="GitHub Actions Provider" \
  --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
  --attribute-condition="assertion.repository_owner == 'YOUR_GITHUB_USERNAME_OR_ORG'" \
  --issuer-uri="https://token.actions.githubusercontent.com"
```

**Important:** Replace `YOUR_GITHUB_USERNAME_OR_ORG` with your GitHub username or organization name.

### 4. Allow Service Account Impersonation

```bash
# Get the workload identity pool resource name
export WORKLOAD_IDENTITY_POOL_ID="projects/$(gcloud projects describe YOUR_PROJECT_ID --format='value(projectNumber)')/locations/global/workloadIdentityPools/github-actions-pool/providers/github-actions-provider"

# Allow the GitHub Actions to impersonate the service account
gcloud iam service-accounts add-iam-policy-binding "${SA_EMAIL}" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/${WORKLOAD_IDENTITY_POOL_ID}/attribute.repository/YOUR_GITHUB_USERNAME/YOUR_REPO_NAME"
```

**Important:** Replace `YOUR_GITHUB_USERNAME/YOUR_REPO_NAME` with your repository path (e.g., `octocat/my-repo`).

### 5. Get the Workload Identity Provider Resource Name

```bash
# This will be used in GitHub Secrets
gcloud iam workload-identity-pools providers describe "github-actions-provider" \
  --location="global" \
  --workload-identity-pool="github-actions-pool" \
  --format="value(name)"
```

Save this value - you'll need it for `GCP_WIF_PROVIDER` secret.

## Artifact Registry Setup

### 1. Create Docker Repository

```bash
# Create the repository for Docker images
gcloud artifacts repositories create orochimary \
  --repository-format=docker \
  --location=YOUR_REGION \
  --description="Docker images for photo converter bot"

# Example regions: us-central1, europe-west1, asia-east1
```

### 2. Verify Repository Access

```bash
# Configure Docker authentication
gcloud auth configure-docker YOUR_REGION-docker.pkg.dev

# List repositories to verify
gcloud artifacts repositories list --location=YOUR_REGION
```

## Cloud Run Setup

### 1. Create Cloud Run Services

```bash
# Create converter service
gcloud run deploy photo-converter \
  --image=us-docker.pkg.dev/cloudrun/container/hello \
  --region=YOUR_REGION \
  --platform=managed \
  --allow-unauthenticated \
  --service-account="${SA_EMAIL}"

# Create bot service
gcloud run deploy photo-convert-bot \
  --image=us-docker.pkg.dev/cloudrun/container/hello \
  --region=YOUR_REGION \
  --platform=managed \
  --no-allow-unauthenticated \
  --service-account="${SA_EMAIL}"
```

**Note:** These create placeholder services with a hello-world image. The GitHub Actions workflow will deploy your actual containers.

### 2. Configure Service Settings (Optional)

```bash
# Update converter service settings
gcloud run services update photo-converter \
  --region=YOUR_REGION \
  --memory=2Gi \
  --cpu=2 \
  --max-instances=10 \
  --timeout=300

# Update bot service settings
gcloud run services update photo-convert-bot \
  --region=YOUR_REGION \
  --memory=512Mi \
  --cpu=1 \
  --max-instances=5 \
  --timeout=60
```

## GitHub Secrets Configuration

Navigate to your GitHub repository: **Settings → Secrets and variables → Actions → New repository secret**

### Required Secrets

| Secret Name | Description | Example / How to Get |
|-------------|-------------|----------------------|
| `GCP_PROJECT` | Your GCP Project ID | `my-project-12345` |
| `GCP_REGION` | GCP region for resources | `us-central1` |
| `GCP_WIF_PROVIDER` | Workload Identity Provider resource name | From WIF setup step 5 above |
| `GCP_SA_EMAIL` | Service account email | `github-actions-deployer@PROJECT_ID.iam.gserviceaccount.com` |
| `CLOUD_RUN_CONVERTER_SERVICE` | Converter Cloud Run service name | `photo-converter` |
| `CLOUD_RUN_BOT_SERVICE` | Bot Cloud Run service name | `photo-convert-bot` |
| `CONVERTER_API_KEY` | API key for converter authentication | Generate a secure random string |
| `BOT_TOKEN` or `TELEGRAM_BOT_TOKEN` | Telegram bot token | From [@BotFather](https://t.me/botfather) |

### Optional Variables

Navigate to: **Settings → Secrets and variables → Actions → Variables tab → New repository variable**

| Variable Name | Description | Default |
|---------------|-------------|---------|
| `MAX_FILE_MB` | Maximum file size in MB | `40` |
| `ALLOWED_EDITORS` | Comma-separated list of Telegram user IDs | - |
| `CHAT_ID` | Target Telegram chat ID | - |
| `TOPIC_SOURCE_ID` | Source topic ID (for forum chats) | - |
| `TOPIC_CONVERTED_ID` | Converted topic ID (for forum chats) | - |
| `BATCH_WINDOW_SECONDS` | Batch processing window | - |
| `PROGRESS_UPDATE_EVERY` | Progress update frequency | - |

### Creating Secrets via GitHub CLI

```bash
# Install GitHub CLI if needed
# See: https://cli.github.com/

# Authenticate
gh auth login

# Set secrets
gh secret set GCP_PROJECT -b "your-project-id"
gh secret set GCP_REGION -b "us-central1"
gh secret set GCP_WIF_PROVIDER -b "projects/123456789/locations/global/workloadIdentityPools/..."
gh secret set GCP_SA_EMAIL -b "github-actions-deployer@your-project.iam.gserviceaccount.com"
gh secret set CLOUD_RUN_CONVERTER_SERVICE -b "photo-converter"
gh secret set CLOUD_RUN_BOT_SERVICE -b "photo-convert-bot"
gh secret set CONVERTER_API_KEY -b "$(openssl rand -base64 32)"
gh secret set BOT_TOKEN -b "your-telegram-bot-token"
```

## Troubleshooting

### Error: "Unauthenticated request"

This error occurs when Docker cannot authenticate to Artifact Registry.

**Possible causes:**

1. **Missing WIF configuration**
   ```bash
   # Verify WIF provider exists
   gcloud iam workload-identity-pools providers list \
     --workload-identity-pool="github-actions-pool" \
     --location="global"
   ```

2. **Incorrect service account permissions**
   ```bash
   # Check service account IAM bindings
   gcloud projects get-iam-policy YOUR_PROJECT_ID \
     --flatten="bindings[].members" \
     --filter="bindings.members:serviceAccount:github-actions-deployer@*"
   ```

3. **Wrong secret values in GitHub**
   - Double-check all secret values match your GCP configuration
   - Ensure `GCP_WIF_PROVIDER` is the full resource name (starts with `projects/`)

### Error: "Repository not found"

**Solution:**

1. Verify the repository exists:
   ```bash
   gcloud artifacts repositories describe orochimary \
     --location=YOUR_REGION \
     --project=YOUR_PROJECT_ID
   ```

2. If it doesn't exist, create it:
   ```bash
   gcloud artifacts repositories create orochimary \
     --repository-format=docker \
     --location=YOUR_REGION
   ```

### Error: "Permission denied"

**Solution:**

Ensure the service account has the required roles:

```bash
# Add missing permissions
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:github-actions-deployer@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/artifactregistry.writer"
```

### Testing Authentication Locally

```bash
# Authenticate as the service account
gcloud auth activate-service-account --key-file=path/to/key.json

# Configure Docker
gcloud auth configure-docker YOUR_REGION-docker.pkg.dev

# Test pushing an image
docker tag hello-world YOUR_REGION-docker.pkg.dev/YOUR_PROJECT_ID/orochimary/test:latest
docker push YOUR_REGION-docker.pkg.dev/YOUR_PROJECT_ID/orochimary/test:latest
```

### Workflow Debugging

The updated workflow includes detailed logging groups:

- **Verifying GCP authentication**: Shows active service account
- **Verifying Artifact Registry access**: Confirms repository exists
- **Verifying Docker authentication**: Tests Docker login
- **Building Docker image**: Shows build progress
- **Pushing Docker image**: Includes retry logic with detailed error messages

Check the GitHub Actions logs for these sections to identify where the failure occurs.

## Security Best Practices

1. **Limit WIF scope**: The attribute condition should restrict to your specific repository
2. **Least privilege**: Only grant necessary IAM roles to the service account
3. **Rotate secrets**: Periodically regenerate API keys and update GitHub secrets
4. **Monitor access**: Review Cloud Audit Logs for unexpected service account usage
5. **Never commit secrets**: Use GitHub Secrets, never hardcode credentials

## Additional Resources

- [Workload Identity Federation Guide](https://cloud.google.com/iam/docs/workload-identity-federation)
- [Artifact Registry Documentation](https://cloud.google.com/artifact-registry/docs)
- [Cloud Run Documentation](https://cloud.google.com/run/docs)
- [GitHub Actions Security](https://docs.github.com/en/actions/security-guides/security-hardening-for-github-actions)

## Support

If you encounter issues not covered in this guide:

1. Check the [GitHub Actions workflow logs](../../.github/workflows/deploy-photo-converter-bot.yml) for detailed error messages
2. Review [Cloud Logging](https://console.cloud.google.com/logs) for service account activity
3. Verify all secrets and variables are correctly configured in GitHub

---

**Last Updated:** 2026-02-10
