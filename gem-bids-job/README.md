# gem-bids-job

One process that lists ongoing GeM bids, reads each PDF text layer, and inserts new rows into the Supabase `gem_bids` table. No model is called. PDFs are kept in memory and never written to disk.

Bids whose `source_pdf` is already in `gem_bids` are skipped, so a scheduled run only fetches new tenders.

## Run locally

```bash
cd gem-bids-job
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill `.env` with `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`, then:

```bash
LIMIT=20 python -m gem_job.main
```

`LIMIT=0` (the default) processes every new ongoing bid.

| Variable | Default | Meaning |
|---|---|---|
| `DOWNLOAD_WORKERS` | 4 | Parallel PDF downloads |
| `LISTING_WORKERS` | 16 | Parallel listing pages |
| `MAX_PDF_PAGES` | 3 | Pages read from each PDF |
| `BATCH_SIZE` | 200 | Rows per Supabase insert |
| `LIMIT` | 0 | Max new bids this run; 0 means all |

The process exits 0 when it finishes, including when some PDFs fail. It exits 1 when listing or Supabase fails.

## Cloud Run Job

The job reads two secrets that are already in Secret Manager:

| Env var | Secret |
|---|---|
| `SUPABASE_URL` | `supabase-url` |
| `SUPABASE_SERVICE_ROLE_KEY` | `supabase-service-key` |

Grant the Cloud Run runtime service account access to those secrets. This example uses the default Compute Engine service account. Replace `PROJECT_ID`.

```bash
PROJECT_NUMBER=$(gcloud projects describe PROJECT_ID --format='value(projectNumber)')
RUNTIME_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

for SECRET in supabase-url supabase-service-key; do
  gcloud secrets add-iam-policy-binding "$SECRET" \
    --member="serviceAccount:${RUNTIME_SA}" \
    --role="roles/secretmanager.secretAccessor"
done
```

From the workspace root, deploy the job. The build context is this folder:

```bash
gcloud run jobs deploy gem-bids-job \
  --source gem-bids-job \
  --region asia-south1 \
  --memory 4Gi \
  --cpu 2 \
  --task-timeout 6h \
  --max-retries 1 \
  --set-secrets SUPABASE_URL=supabase-url:latest,SUPABASE_SERVICE_ROLE_KEY=supabase-service-key:latest
```

Run it once:

```bash
gcloud run jobs execute gem-bids-job --region asia-south1
```

## Schedule

Create a service account that is allowed to start the job, then a daily Cloud Scheduler trigger. Replace `PROJECT_ID`.

```bash
gcloud iam service-accounts create gem-bids-scheduler \
  --display-name="GeM bids job scheduler"

gcloud run jobs add-iam-policy-binding gem-bids-job \
  --region asia-south1 \
  --member="serviceAccount:gem-bids-scheduler@PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/run.invoker"

gcloud scheduler jobs create http gem-bids-job-daily \
  --location asia-south1 \
  --schedule "0 3 * * *" \
  --time-zone "Asia/Kolkata" \
  --uri "https://asia-south1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/PROJECT_ID/jobs/gem-bids-job:run" \
  --http-method POST \
  --oauth-service-account-email "gem-bids-scheduler@PROJECT_ID.iam.gserviceaccount.com"
```
