# deploy

**Goldenrod on Cloud Run, against ClickHouse Cloud.** Written down rather than remembered:
a deploy that only exists in somebody's shell history is a deploy that cannot be repeated
on the day it matters.

The image is built by Cloud Build from source, so no local Docker daemon is involved. The
[`Dockerfile`](../Dockerfile) is at the repo root because `--source .` looks for it there.

## What runs where

| | |
|---|---|
| Interface + Gate + Extractor | Cloud Run, one container |
| ClickHouse | ClickHouse Cloud, reached **only** through the `mcp-clickhouse` server inside that container |
| Gemini | Vertex AI, through the Cloud Run service account — **no API key exists anywhere** |
| Identity | the platform's. Cloud IAP writes `X-Goog-Authenticated-User-Email`; see [Access](#access-on-the-hosted-url) |

The container launches the MCP server over stdio and speaks JSON-RPC to it, exactly as on a
laptop, using the same [`.mcp.json`](../.mcp.json). Nothing in `services/` opens a database
connection of its own — that is the ClickHouse track requirement, and it is satisfied by
the same code path in both places.

## 1. Prerequisites, once

```bash
winget install Google.CloudSDK        # or https://cloud.google.com/sdk/docs/install
gcloud init
gcloud config set project PROJECT_ID
```

The project needs **billing enabled** — Cloud Run, Cloud Build and Vertex AI all require it.

```bash
gcloud services enable \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    aiplatform.googleapis.com \
    secretmanager.googleapis.com \
    artifactregistry.googleapis.com
```

Then authenticate locally. This is also what lets the Extractor run from your machine —
`GeminiBackend` picks Vertex AI whenever `GOOGLE_CLOUD_PROJECT` is set:

```bash
gcloud auth application-default login
```

## 2. A ClickHouse Cloud service

Create one at <https://clickhouse.cloud>. Keep the host, the user and the password. The
password goes into Secret Manager and never into the repo:

```bash
printf '%s' 'THE_PASSWORD' | gcloud secrets create clickhouse-password --data-file=-
```

## 3. A service account for the running service

Least privilege: it calls Vertex AI and reads one secret. Nothing else.

```bash
gcloud iam service-accounts create goldenrod-run \
    --display-name "Goldenrod Cloud Run service"

gcloud projects add-iam-policy-binding PROJECT_ID \
    --member "serviceAccount:goldenrod-run@PROJECT_ID.iam.gserviceaccount.com" \
    --role roles/aiplatform.user

gcloud secrets add-iam-policy-binding clickhouse-password \
    --member "serviceAccount:goldenrod-run@PROJECT_ID.iam.gserviceaccount.com" \
    --role roles/secretmanager.secretAccessor
```

## 4. Load the production into ClickHouse Cloud

From your machine, pointed at the cloud service. The loaders are unchanged — they reach
ClickHouse through the MCP server here exactly as they do locally.

```bash
export CLICKHOUSE_HOST=xxx.clickhouse.cloud
export CLICKHOUSE_PORT=8443
export CLICKHOUSE_USER=default
export CLICKHOUSE_PASSWORD=...
export CLICKHOUSE_SECURE=true
export CLICKHOUSE_DATABASE=goldenrod

python -m services.loader.schema && python -m services.loader.schema --check
python -m services.loader.seed --dir data/fixtures
python -m services.extractor data/fixtures/script-v1.fountain \
    --production fayoum --revision white-2026-08-01     -o data/fixtures/graph-v1.json
python -m services.extractor data/fixtures/script-v2.fountain \
    --production fayoum --revision goldenrod-2026-08-29 -o data/fixtures/graph-v2.json
python -m services.loader data/fixtures/graph-v1.json
python -m services.loader data/fixtures/graph-v2.json
python -m services.gate data/fixtures/call-sheet.json --pages data/fixtures/script-v2.fountain
```

Those exports can live in `.env` instead — it is read at import and only ever fills gaps,
so a real environment variable always wins over it.

**Do this before deploying.** The check fails loudly rather than reporting an all-clear it
has not earned, so a service deployed against an empty database serves an error, correctly.

## 5. Deploy

```bash
gcloud run deploy goldenrod \
    --source . \
    --region europe-west1 \
    --allow-unauthenticated \
    --min-instances 1 \
    --concurrency 8 \
    --timeout 3600 \
    --memory 1Gi \
    --service-account goldenrod-run@PROJECT_ID.iam.gserviceaccount.com \
    --set-env-vars "PRODUCTION_ID=fayoum,GOLDENROD_IDENTITY_CHOOSER=1,GOOGLE_CLOUD_PROJECT=PROJECT_ID,GOOGLE_CLOUD_LOCATION=europe-west1,CLICKHOUSE_HOST=xxx.clickhouse.cloud,CLICKHOUSE_PORT=8443,CLICKHOUSE_USER=default,CLICKHOUSE_SECURE=true,CLICKHOUSE_VERIFY=true,CLICKHOUSE_DATABASE=goldenrod" \
    --set-secrets "CLICKHOUSE_PASSWORD=clickhouse-password:latest"
```

Why those flags, since none of them are defaults worth guessing at later:

- `--min-instances 1` — a cold start is a container start *plus* an MCP subprocess spawn.
  Nobody watching a three-minute demo should pay for both. It bills continuously; set a
  budget alert.
- `--concurrency 8` — `ClickHouseMCP` serialises one stdio pipe, so stacking requests onto
  one instance queues them rather than parallelising them. Let Cloud Run add instances.
- `--timeout 3600` — `/api/run/stream` is server-sent events and must not be cut off.
- `--allow-unauthenticated` — see below. The application does its own authorisation.

## Access on the hosted URL

`GOLDENROD_IDENTITY_CHOOSER=1`, deliberately, so a judge opening the link lands signed in as
the 1st AD and can switch to the property master to watch the scoping happen. Locking the
URL would hide the thing it is meant to show.

This is not the same as the app being open. Every route resolves an identity and refuses
without one — `curl` the deployed `/api/run` with no cookie and it is a 401, on the public
internet as much as on a laptop.

Behind Cloud IAP, exactly one thing changes:

```bash
gcloud run services update goldenrod --update-env-vars GOLDENROD_IDENTITY_CHOOSER=0
```

The chooser disappears, the local header and cookie stop being honoured, and identity comes
from `X-Goog-Authenticated-User-Email` — the header [`services/api/auth.py`](../services/api/auth.py)
already reads. No code change; that is the point of reading the header IAP writes.

## Verifying a deploy

```bash
URL=$(gcloud run services describe goldenrod --region europe-west1 --format='value(status.url)')

curl -s "$URL/api/health" | python -m json.tool   # ok:true, ClickHouse Cloud version, access.grants
curl -s -o /dev/null -w '%{http_code}\n' "$URL/api/run"   # 401 — access fails closed here too
```

Then open `$URL` and watch the six pipeline steps complete, the ClickHouse query render
beside the rows it returned, and the findings come back ranked with scene 22 on top.

## Logs

```bash
gcloud run services logs tail goldenrod --region europe-west1
```

The MCP server's own stderr is drained by the client and surfaces in these logs, so a
ClickHouse connection failure shows up here rather than vanishing.
