# Authentication Model

This project now uses a single authentication mechanism for converter requests.

## Current behavior

- Converter Cloud Run is public via IAM (`allUsers` has `roles/run.invoker`).
- Bot calls `POST {CONVERTER_URL}/convert`.
- Bot sends only one auth header: `X-API-KEY: <CONVERTER_API_KEY>`.
- No Cloud Run ID token is requested, cached, or attached.
- No `Authorization: Bearer ...` header is used.

## Deployment notes

1. Keep the converter service publicly invokable:

```bash
gcloud run services add-iam-policy-binding "${CLOUD_RUN_CONVERTER_SERVICE}" \
  --region="${REGION}" \
  --member="allUsers" \
  --role="roles/run.invoker"
```

2. Set the same `CONVERTER_API_KEY` in bot and converter environments.

3. Verify converter access:

```bash
curl -X POST "${CONVERTER_URL}/convert" \
  -H "X-API-KEY: ${CONVERTER_API_KEY}" \
  -F "file=@test-image.jpg"
```
