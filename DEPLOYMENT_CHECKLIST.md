# Deployment Checklist

Quick reference for verifying your GCP deployment setup.

## ✅ Pre-Deployment Checklist

### GitHub Secrets (Required)

Verify all secrets are set in **GitHub Repository → Settings → Secrets and variables → Actions**:

- [ ] `GCP_PROJECT` - Your GCP project ID
- [ ] `GCP_REGION` - Deployment region (e.g., `us-central1`)
- [ ] `GCP_WIF_PROVIDER` - Full Workload Identity Provider resource name
- [ ] `GCP_SA_EMAIL` - Service account email
- [ ] `CLOUD_RUN_CONVERTER_SERVICE` - Converter service name
- [ ] `CLOUD_RUN_BOT_SERVICE` - Bot service name
- [ ] `CONVERTER_API_KEY` - API key for authentication
- [ ] `BOT_TOKEN` or `TELEGRAM_BOT_TOKEN` - Telegram bot token

### GCP Prerequisites

- [ ] GCP project created with billing enabled
- [ ] Required APIs enabled:
  - [ ] `artifactregistry.googleapis.com`
  - [ ] `run.googleapis.com`
  - [ ] `iamcredentials.googleapis.com`
  - [ ] `cloudresourcemanager.googleapis.com`
  - [ ] `sts.googleapis.com`

### Service Account Setup

- [ ] Service account created
- [ ] Service account has required roles:
  - [ ] `roles/artifactregistry.writer`
  - [ ] `roles/run.admin`
  - [ ] `roles/iam.serviceAccountUser`

### Workload Identity Federation

- [ ] Workload Identity Pool created
- [ ] Workload Identity Provider configured
- [ ] Service account can be impersonated by GitHub Actions
- [ ] Attribute condition restricts to your repository

### Artifact Registry

- [ ] Docker repository created (name: `orochimary`)
- [ ] Repository location matches `GCP_REGION`
- [ ] Service account has write access

### Cloud Run Services

- [ ] Converter service exists
- [ ] Bot service exists
- [ ] Services configured with appropriate resources

## 🔍 Quick Verification Commands

### Check GCP Configuration

```bash
# Set your project
export PROJECT_ID="your-project-id"
export REGION="us-central1"
export SA_EMAIL="github-actions-deployer@${PROJECT_ID}.iam.gserviceaccount.com"

# Verify service account exists
gcloud iam service-accounts describe "${SA_EMAIL}"

# Check service account permissions
gcloud projects get-iam-policy "${PROJECT_ID}" \
  --flatten="bindings[].members" \
  --filter="bindings.members:serviceAccount:${SA_EMAIL}"

# Verify Artifact Registry repository
gcloud artifacts repositories describe orochimary \
  --location="${REGION}" \
  --project="${PROJECT_ID}"

# Verify Cloud Run services
gcloud run services describe photo-converter --region="${REGION}"
gcloud run services describe photo-convert-bot --region="${REGION}"

# Check WIF provider
gcloud iam workload-identity-pools providers describe github-actions-provider \
  --workload-identity-pool=github-actions-pool \
  --location=global
```

### Verify GitHub Secrets

```bash
# Using GitHub CLI
gh secret list

# Check if specific secrets exist (won't show values)
gh secret list | grep -E 'GCP_PROJECT|GCP_REGION|GCP_WIF_PROVIDER|GCP_SA_EMAIL'
```

## 🚨 Common Issues and Quick Fixes

### "Unauthenticated request" Error

**Cause:** WIF authentication failed

**Fix:**
1. Verify `GCP_WIF_PROVIDER` secret is the full resource name
2. Check service account impersonation binding:
   ```bash
   gcloud iam service-accounts get-iam-policy "${SA_EMAIL}"
   ```

### "Repository not found" Error

**Cause:** Artifact Registry repository doesn't exist

**Fix:**
```bash
gcloud artifacts repositories create orochimary \
  --repository-format=docker \
  --location="${REGION}"
```

### "Permission denied" Error

**Cause:** Service account lacks required permissions

**Fix:**
```bash
# Grant Artifact Registry Writer role
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/artifactregistry.writer"
```

### Workflow Fails at "Validate required configuration"

**Cause:** Missing GitHub secrets

**Fix:** Review the error message to identify which secret is missing, then add it in GitHub Settings

### Bot Gets 403 Error When Calling Converter

**Cause:** Cloud Run converter service requires authentication but bot doesn't have proper permissions

**Fix Options:**

**Option 1: Public converter (simpler)**
```bash
gcloud run services update "${CLOUD_RUN_CONVERTER_SERVICE}" \
  --region="${REGION}" \
  --allow-unauthenticated
```

**Option 2: Private converter with IAM (more secure)**
1. Keep converter with authentication enabled
2. Grant bot service account access to invoke converter:
```bash
# Get the bot service account
BOT_SA=$(gcloud run services describe "${CLOUD_RUN_BOT_SERVICE}" \
  --region="${REGION}" \
  --format="value(spec.template.spec.serviceAccountName)")

# Grant run.invoker role to bot service account for converter service
gcloud run services add-iam-policy-binding "${CLOUD_RUN_CONVERTER_SERVICE}" \
  --region="${REGION}" \
  --member="serviceAccount:${BOT_SA}" \
  --role="roles/run.invoker"
```

**Note:** The bot now automatically includes ID tokens when calling the converter, so both options work seamlessly.

### ALLOWED_EDITORS Format Issues

**Cause:** Incorrect environment variable format

**Fix:** Ensure `ALLOWED_EDITORS` in GitHub Variables uses pipe-separated format:
```
# Correct format (pipe-separated):
123456789|987654321|555555555

# Also supported (comma-separated):
123456789,987654321,555555555

# Also supported (space-separated):
123456789 987654321 555555555
```

Verify in Cloud Run:
```bash
gcloud run services describe "${CLOUD_RUN_BOT_SERVICE}" \
  --region="${REGION}" \
  --format="value(spec.template.spec.containers[0].env)"
```

## 📚 Detailed Setup Guide

For comprehensive setup instructions, see [docs/GCP_SETUP.md](docs/GCP_SETUP.md)

## 🔄 Testing the Deployment

### Manual Workflow Trigger

1. Go to **Actions** tab in GitHub
2. Select "Deploy photo-converter monorepo"
3. Click "Run workflow"
4. Select branch and click "Run workflow"

### Monitor Deployment

Watch the workflow execution for these stages:
- ✅ Authentication and validation
- ✅ Docker image build
- ✅ Image push to Artifact Registry
- ✅ Cloud Run deployment

### Verify Deployment Success

```bash
# Check deployed services
gcloud run services describe photo-converter --region="${REGION}" --format="value(status.url)"
gcloud run services describe photo-convert-bot --region="${REGION}" --format="value(status.url)"

# Test converter endpoint
CONVERTER_URL=$(gcloud run services describe photo-converter --region="${REGION}" --format="value(status.url)")
curl -X POST "${CONVERTER_URL}/convert" \
  -H "X-API-Key: YOUR_CONVERTER_API_KEY" \
  -F "file=@test-image.jpg"
```

## 📞 Getting Help

If you're still experiencing issues:

1. **Check workflow logs**: GitHub Actions provides detailed logs for each step
2. **Review Cloud Logs**: Visit [GCP Cloud Logging](https://console.cloud.google.com/logs)
3. **Verify configuration**: Double-check all secrets match your GCP setup
4. **Test locally**: Try building and pushing images from your local machine

---

**Quick Setup:** First time deploying? Follow these docs in order:
1. [GCP_SETUP.md](docs/GCP_SETUP.md) - Complete setup guide
2. This checklist - Verify everything is configured
3. Trigger workflow - Test the deployment
