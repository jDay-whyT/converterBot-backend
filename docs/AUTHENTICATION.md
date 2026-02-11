# Authentication Architecture

This document explains the authentication mechanisms in the photo converter bot system.

## Overview

The system uses a **dual-layer authentication** approach for maximum security:

1. **Cloud Run IAM Authentication** (Optional) - Service-to-service authentication using ID tokens
2. **Application-level API Key** (Required) - Custom API key validation

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
                                    • X-API-KEY                           • ID Token (if required)
                                    • Authorization: Bearer <ID_TOKEN>    • X-API-KEY
```

## Layer 1: Cloud Run IAM Authentication (Optional)

### How It Works

When the converter service is deployed with authentication required (default Cloud Run behavior):

- Cloud Run verifies the caller's identity using Google Cloud ID tokens
- The bot automatically obtains ID tokens using Application Default Credentials (ADC)
- ID tokens are cached for 55 minutes and refreshed automatically
- If ID token cannot be obtained, the bot falls back to API key only (for `--allow-unauthenticated` services)

### Implementation

The bot's `ConverterClient` class handles ID token management:

```python
def _get_id_token(self) -> str | None:
    """Get Google Cloud ID token for authenticating to Cloud Run services."""
    # Uses google.auth library to obtain ID tokens
    # Caches tokens for 55 minutes
    # Falls back gracefully if not running on GCP
```

### Configuration Options

#### Option 1: Public Converter (Simpler)

Deploy converter without authentication requirement:

```bash
gcloud run services update photo-converter \
  --region=us-central1 \
  --allow-unauthenticated
```

**Pros:**
- Simpler setup
- No IAM configuration needed
- Still protected by API key

**Cons:**
- Anyone with the URL + API key can access
- No GCP identity-based access control

#### Option 2: Private Converter (More Secure)

Keep authentication enabled and grant bot access:

```bash
# Get bot service account
BOT_SA=$(gcloud run services describe photo-convert-bot \
  --region=us-central1 \
  --format="value(spec.template.spec.serviceAccountName)")

# Grant run.invoker role
gcloud run services add-iam-policy-binding photo-converter \
  --region=us-central1 \
  --member="serviceAccount:${BOT_SA}" \
  --role="roles/run.invoker"
```

**Pros:**
- Defense in depth (two authentication layers)
- GCP-native access control
- Audit logs for service-to-service calls

**Cons:**
- Requires IAM configuration
- More complex troubleshooting

## Layer 2: Application-level API Key (Required)

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

### Security Best Practices

- Use a cryptographically random key (minimum 32 bytes)
- Rotate keys periodically
- Never commit keys to version control
- Store in GitHub Secrets, not Variables

## Troubleshooting

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
     --format="value(status.conditions[0].message)"
   ```

2. **Check bot logs for ID token status:**
   ```bash
   gcloud logs read --service=photo-convert-bot --limit=50 | grep "ID token"
   ```

   Expected messages:
   - Success: `Successfully obtained ID token for Cloud Run authentication`
   - Fallback: `Could not obtain ID token (service might be public)`

3. **Verify IAM permissions (if using Option 2):**
   ```bash
   gcloud run services get-iam-policy photo-converter --region=us-central1
   ```

   Should show:
   ```yaml
   members:
   - serviceAccount:bot-service-account@project.iam.gserviceaccount.com
   role: roles/run.invoker
   ```

**Solutions:**

- **For 403 with "Connector is closed"**: httpx client lifecycle issue (should not happen with current code)
- **For 403 with no ID token**: Use Option 1 (allow unauthenticated) or fix IAM permissions
- **For 403 with invalid API key**: Check `CONVERTER_API_KEY` matches in both services

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
- [ ] Use `--no-allow-unauthenticated` for converter (Option 2) in production
- [ ] Grant minimal IAM permissions (run.invoker only)
- [ ] Monitor Cloud Logging for unauthorized access attempts
- [ ] Keep `ALLOWED_EDITORS` list minimal and up-to-date
- [ ] Use Cloud Run ingress controls if needed:
  ```bash
  gcloud run services update photo-converter \
    --ingress=internal  # Only accessible from same project
  ```

## Testing Authentication

### Test Converter Endpoint

```bash
# Get converter URL
CONVERTER_URL=$(gcloud run services describe photo-converter \
  --region=us-central1 \
  --format="value(status.url)")

# Test with API key only (works with --allow-unauthenticated)
curl -X POST "${CONVERTER_URL}/convert" \
  -H "X-API-Key: ${CONVERTER_API_KEY}" \
  -F "file=@test-image.heic" \
  -F "quality=92" \
  -o output.jpg

# Test with ID token (required without --allow-unauthenticated)
ID_TOKEN=$(gcloud auth print-identity-token)
curl -X POST "${CONVERTER_URL}/convert" \
  -H "X-API-Key: ${CONVERTER_API_KEY}" \
  -H "Authorization: Bearer ${ID_TOKEN}" \
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

# View ID token acquisition logs
gcloud logs read --service=photo-convert-bot \
  --filter='textPayload:"ID token"' \
  --limit=20
```

## References

- [Cloud Run Authentication](https://cloud.google.com/run/docs/authenticating/service-to-service)
- [Google Auth Python Library](https://google-auth.readthedocs.io/)
- [Cloud Run IAM Roles](https://cloud.google.com/run/docs/reference/iam/roles)
