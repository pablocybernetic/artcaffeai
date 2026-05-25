# ArtCaffe — Brand Pipeline

A FastAPI service that processes brand guideline documents (PDF/DOCX) into structured JSON using Claude, stores them in Supabase, and serves them for downstream ideation workflows.

---

## Project Structure

```
artcaffe/
├── app/
│   ├── app.py                  # FastAPI entry point
│   ├── brand_context.py        # Supabase brand_contexts table helpers
│   ├── brand_pipeline.py       # Core pipeline: extract → AI structure → persist
│   ├── job_runner.py           # Job table bridge + standalone poller
│   └── ideation_example.py     # Example: generate on-brand campaign ideas
├── scripts/
│   ├── 002_brand_pipeline_support.sql  # Supabase SQL migration
│   └── deploy.sh               # One-shot deployment script for Google VM
├── systemd/
│   ├── artcaffe-api.service    # systemd unit for the API server
│   └── artcaffe-worker.service # systemd unit for the standalone poller
├── requirements.txt
├── .env.example
└── README.md
```

---

## Prerequisites

- Google Cloud VM with **Python 3.11+** installed
- A **Supabase** project with the main schema already migrated
- An **Anthropic API key**
- Firewall rule allowing **TCP port 8000** (or your chosen port)

---

## Step 1 — Supabase Setup

### Run the SQL migration
In the Supabase SQL Editor, run `scripts/002_brand_pipeline_support.sql`.  
This creates the `claim_next_job()` function and enables Realtime on `brand_contexts`.

### Create the storage bucket
In the Supabase Dashboard → Storage, create a bucket named `brand-guidelines`:
- **Public:** No
- **File size limit:** 20 MB
- **Allowed MIME types:** `application/pdf`

Then add the two RLS policies from the comments at the bottom of the SQL file.

---

## Step 2 — Deploy to Google VM

### Option A: Automated (recommended)

```bash
# SSH into your VM
gcloud compute ssh your-vm-name

# Clone the repo
git clone https://github.com/your-org/artcaffe.git /tmp/artcaffe
cd /tmp/artcaffe

# Run the deploy script
sudo bash scripts/deploy.sh
```

The script will:
1. Create a system user `artcaffe`
2. Copy files to `/opt/artcaffe`
3. Create a Python virtual environment and install dependencies
4. Create `/opt/artcaffe/.env` from the example (you fill in credentials)
5. Install and start the `artcaffe-api` systemd service

### Option B: Manual

```bash
# Install dependencies
python3 -m venv /opt/artcaffe/venv
/opt/artcaffe/venv/bin/pip install -r requirements.txt

# Configure environment
cp .env.example /opt/artcaffe/.env
nano /opt/artcaffe/.env   # fill in your values

# Run directly (for testing)
cd app
/opt/artcaffe/venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
```

---

## Step 3 — Configure Environment

Edit `/opt/artcaffe/.env`:

```env
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your-service-role-key
ANTHROPIC_API_KEY=sk-ant-...
BRAND_BUCKET=brand-guidelines
BRAND_MODEL=claude-sonnet-4-20250514
API_HOST=0.0.0.0
API_PORT=8000
API_WORKERS=2
CORS_ORIGINS=https://your-frontend.com
```

Then restart the service:
```bash
sudo systemctl restart artcaffe-api
```

---

## API Usage

### Health check
```bash
curl http://YOUR_VM_IP:8000/health
# {"status":"ok"}
```

### Enqueue a brand-guidelines processing job
```bash
curl -X POST http://YOUR_VM_IP:8000/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "concept_id": "uuid-of-your-concept",
    "file_path": "concepts/my-brand/guidelines-v1.pdf",
    "file_name": "guidelines-v1.pdf",
    "mime_type": "application/pdf"
  }'
```

Response:
```json
{
  "job_id": "abc123...",
  "status": "pending",
  "task": "brand_guidelines.process",
  ...
}
```

### Check job status
```bash
curl http://YOUR_VM_IP:8000/jobs/abc123...
```

---

## Running the Standalone Job Poller (Optional)

The API already processes jobs in the background. Only enable the poller if you want to decouple processing from the API process:

```bash
sudo systemctl enable --now artcaffe-worker
sudo journalctl -u artcaffe-worker -f
```

---

## Testing Ideation Locally

```bash
cd app
export SUPABASE_URL=...
export SUPABASE_SERVICE_ROLE_KEY=...
export ANTHROPIC_API_KEY=...

python ideation_example.py <concept_id> "Launch our new oat milk latte"
```

---

## Logs & Monitoring

```bash
# API logs
sudo journalctl -u artcaffe-api -f

# Worker logs (if enabled)
sudo journalctl -u artcaffe-worker -f

# Check service status
sudo systemctl status artcaffe-api
```

---

## Firewall (Google Cloud)

Open port 8000 in the GCP Console:
- **VPC Network → Firewall → Create Rule**
- Targets: all instances (or specific tags)
- Source IP ranges: `0.0.0.0/0` (or restrict to your IP)
- Protocols/ports: `tcp:8000`

Or via gcloud CLI:
```bash
gcloud compute firewall-rules create artcaffe-api \
  --allow tcp:8000 \
  --description "ArtCaffe Brand Pipeline API"
```

> For production, put the API behind a load balancer or nginx with HTTPS instead of exposing port 8000 directly.
