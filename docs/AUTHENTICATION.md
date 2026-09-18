# Authentication Model

Converter (`photo-converter`) has two entry points with two different auth mechanisms.

## `/pubsub/push` — the real job path

- Bot publishes a job to the `tg-convert-jobs` Pub/Sub topic; it never calls converter directly.
- Pub/Sub delivers it via a push subscription to `{CONVERTER_URL}/pubsub/push`.
- Converter is deployed `--allow-unauthenticated` so the push subscription can reach it.
- If you want to lock this down instead, create the subscription with
  `--push-auth-service-account` (see `scripts/setup-pubsub.sh`) and switch the
  service to `--no-allow-unauthenticated`, granting that service account
  `roles/run.invoker` on converter.

## `/convert` — manual/HTTP testing path

- Guarded by a static header: `X-API-KEY: <CONVERTER_API_KEY>`.
- No Cloud Run ID token is requested, cached, or attached.
- No `Authorization: Bearer ...` header is used.
- Verify access:

```bash
curl -X POST "${CONVERTER_URL}/convert" \
  -H "X-API-KEY: ${CONVERTER_API_KEY}" \
  -F "file=@test-image.jpg"
```

## Deployment notes

Set `CONVERTER_API_KEY` in the converter's environment (used only for the
`/convert` header check — `/pubsub/push` doesn't use it). `BOT_TOKEN`, `CHAT_ID`
and `TOPIC_CONVERTED_ID` must also be set on converter now, since it downloads
from and uploads to Telegram directly (see `docs/PUBSUB_ARCHITECTURE.md`).
