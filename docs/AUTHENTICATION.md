# Authentication Architecture

This document explains the authentication mechanisms in the photo converter bot system.

## Overview

The system uses **API Key authentication** for securing access to the converter service:

- **Application-level API Key** (Required) - Custom API key validation via X-API-KEY header

## Authentication Flow

```
┌─────────────┐                     ┌──────────────────┐                    ┌─────────────┐
│             │  1. Download file   │                  │  2. Convert file   │             │
│  Telegram   │ ─────────────────> │   Bot Service    │ ─────────────────> │  Converter  │
│             │                     │  (Cloud Run)     │                    │  (Cloud Run)│
│             │  4. Send converted  │                  │  3. Return JPG     │             │
│             │ <───────────────── │                  │ <───────────────── │             │
└─────────────┘                     └──────────────────┘                    └─────────────┘
                                            │                                       │
                                            │                                       │
                                    Auth Headers:                         Validates:
                                    • X-API-KEY                           • X-API-KEY
```

## API Key Authentication

### How It Works

- Converter service validates `X-API-KEY` header on every request
- API key is configured via `CONVERTER_API_KEY` environment variable
- Same key used by both converter (validation) and bot (sending)

### Configuration

1. **Generate a strong API key:**
   ```bash
   # Generate random key
   openssl rand -base64 32
   ```

2. **Set as GitHub Secret:**
   ```bash
   gh secret set CONVERTER_API_KEY --body "your-generated-key"
   ```

3. **Deploy:** Workflow automatically sets the key for both services

### Deployment Options

#### Option 1: Public Converter (Recommended)

Deploy converter without Cloud Run authentication requirement:

```bash
gcloud run services update photo-converter \
  --region=us-central1 \
  --allow-unauthenticated
```

**Pros:**
- Simpler setup
- No IAM configuration needed
- Protected by API key

**Cons:**
- Anyone with the URL + API key can access
- No GCP identity-based access control

#### Option 2: Private Converter

Keep Cloud Run authentication enabled:

```bash
gcloud run services update photo-converter \
  --region=us-central1 \
  --no-allow-unauthenticated
```

**Note:** This requires additional IAM configuration for bot service account access.

### Security Best Practices

- Use a cryptographically random key (minimum 32 bytes)
- Rotate keys periodically
- Never commit keys to version control
- Store in GitHub Secrets, not Variables

## Troubleshooting

### 401 Unauthorized Error

**Symptoms:**
```
httpx.HTTPStatusError: 401 Unauthorized
detail: "invalid api key"
```

**Diagnosis:**

1. **Check bot environment:**
   ```bash
   gcloud run services describe photo-convert-bot \
     --region=us-central1 \
     --format="json" | jq '.spec.template.spec.containers[0].env[] | select(.name=="CONVERTER_API_KEY")'
   ```

2. **Check converter environment:**
   ```bash
   gcloud run services describe photo-converter \
     --region=us-central1 \
     --format="json" | jq '.spec.template.spec.containers[0].env[] | select(.name=="CONVERTER_API_KEY")'
   ```

**Solution:**

Keys must match. Redeploy if they don't:
```bash
gh workflow run deploy-photo-converter-bot.yml
```

### 403 Forbidden Error

**Symptoms:**
```
httpx.HTTPStatusError: 403 Forbidden
```

**Diagnosis:**

1. **Check converter authentication setting:**
   ```bash
   gcloud run services describe photo-converter \
     --region=us-central1 \
     --format="value(metadata.annotations)"
   ```

2. **Verify service is public:**
   ```bash
   gcloud run services get-iam-policy photo-converter --region=us-central1
   ```

   For public access, should show:
   ```yaml
   members:
   - allUsers
   role: roles/run.invoker
   ```

**Solution:**

Use `--allow-unauthenticated` deployment option (Option 1 above).

### Environment Variable Issues

**CONVERTER_URL Not Set**

**Symptoms:**
```
ValueError: Missing required environment variable: CONVERTER_URL
```

**Solution:**

The workflow automatically retrieves the converter URL. Ensure:
1. Converter is deployed first (workflow does this via `needs: deploy_converter`)
2. Converter service exists and is accessible
3. Workflow has necessary permissions to describe Cloud Run services

**ALLOWED_EDITORS Format**

**Accepted Formats:**
```bash
# Pipe-separated (recommended for GitHub Variables)
ALLOWED_EDITORS=123456789|987654321|555555555

# Comma-separated
ALLOWED_EDITORS=123456789,987654321,555555555

# Space-separated
ALLOWED_EDITORS="123456789 987654321 555555555"
```

**Verification:**
```python
# In bot logs during startup
logging.info("Configuration loaded successfully")
# Check that bot accepts commands from authorized users
```

## Security Checklist

- [ ] Use strong random API keys (minimum 32 bytes)
- [ ] Rotate API keys periodically
- [ ] Keep `ALLOWED_EDITORS` list minimal and up-to-date
- [ ] Use Cloud Run ingress controls if needed:
  ```bash
  gcloud run services update photo-converter \
    --ingress=internal  # Only accessible from same project
  ```
- [ ] Monitor Cloud Logging for unauthorized access attempts

## Testing Authentication

### Test Converter Endpoint

```bash
# Get converter URL
CONVERTER_URL=$(gcloud run services describe photo-converter \
  --region=us-central1 \
  --format="value(status.url)")

# Test with API key
curl -X POST "${CONVERTER_URL}/convert" \
  -H "X-API-Key: ${CONVERTER_API_KEY}" \
  -F "file=@test-image.heic" \
  -F "quality=92" \
  -o output.jpg
```

### Monitor Authentication Events

```bash
# View authentication logs
gcloud logs read --service=photo-converter \
  --filter='httpRequest.status=401 OR httpRequest.status=403' \
  --limit=50
```

## References

- [Cloud Run Authentication](https://cloud.google.com/run/docs/authenticating/service-to-service)
- [Cloud Run IAM Roles](https://cloud.google.com/run/docs/reference/iam/roles)
