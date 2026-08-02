"""
upload_catalog_images.py
-------------------------
One-off script: reads a product catalog CSV (SKU, Item Name, Price (KES),
Category, Barcode, Image Link), downloads each row's external "Image Link",
re-uploads it to Supabase Storage, and writes a new CSV with an added
"Supabase Image Link" column.

Run:
  cd fastAPI&backend
  python scripts/upload_catalog_images.py path/to/catalog.csv

Optional:
  --output path/to/output.csv     (default: <input>_with_supabase.csv)
  --bucket product-catalog-images (default bucket name; created if missing)
  --dry-run                       (don't upload, just report what would happen)

Env vars required (same as the API — read from fastAPI&backend/.env):
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY

Re-running is safe: rows that already have a value in "Supabase Image Link"
in the input CSV are skipped, and storage uploads use upsert=true so the
same SKU re-uploads cleanly instead of erroring on a duplicate path.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from pathlib import Path

import httpx
from supabase import Client, create_client

OUTPUT_COLUMN = "Supabase Image Link"
CONTENT_TYPE_COLUMN = "Content Type"
IMAGE_LINK_COLUMN = "Image Link"
SKU_COLUMN = "SKU"
DEFAULT_BUCKET = "product-catalog-images"
REQUEST_TIMEOUT = 20.0
MAX_RETRIES = 2
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Load .env from parent dir if env vars not already set (same pattern as
# backfill_weekly_snapshots.py)
# ---------------------------------------------------------------------------
def _load_env():
    for candidate in [Path(__file__).parent.parent / ".env"]:
        if not candidate.exists():
            continue
        with candidate.open() as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
        break


_load_env()


def _sanitize(name: str) -> str:
    keep = "-_."
    return "".join(c if c.isalnum() or c in keep else "_" for c in name).strip("_") or "file"


def _download_image(url: str) -> tuple[bytes, str]:
    """Returns (bytes, content_type). Raises on failure."""
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            with httpx.Client(follow_redirects=True, timeout=REQUEST_TIMEOUT) as client:
                resp = client.get(url, headers={"User-Agent": USER_AGENT})
                resp.raise_for_status()
                if not resp.content:
                    raise RuntimeError("empty response body")
                return resp.content, resp.headers.get("content-type", "")
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt <= MAX_RETRIES:
                time.sleep(1.5 * attempt)
    raise RuntimeError(f"download failed after {MAX_RETRIES + 1} attempts: {last_exc}")


def _ensure_bucket(sb: Client, bucket: str) -> None:
    try:
        existing = {b.name if hasattr(b, "name") else b["name"] for b in sb.storage.list_buckets()}
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] could not list buckets ({exc}); assuming '{bucket}' exists", flush=True)
        return
    if bucket not in existing:
        print(f"Creating public bucket '{bucket}'...", flush=True)
        sb.storage.create_bucket(bucket, options={"public": True})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_csv", type=Path)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.input_csv.exists():
        sys.exit(f"Input CSV not found: {args.input_csv}")

    output_path = args.output or args.input_csv.with_name(args.input_csv.stem + "_with_supabase.csv")

    sb: Client | None = None
    if not args.dry_run:
        supabase_url = os.environ["SUPABASE_URL"]
        supabase_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
        sb = create_client(supabase_url, supabase_key)
        _ensure_bucket(sb, args.bucket)

    with args.input_csv.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])

    if IMAGE_LINK_COLUMN not in fieldnames:
        sys.exit(f"Expected a '{IMAGE_LINK_COLUMN}' column — found: {fieldnames}")
    if OUTPUT_COLUMN not in fieldnames:
        fieldnames.append(OUTPUT_COLUMN)
    if CONTENT_TYPE_COLUMN not in fieldnames:
        fieldnames.append(CONTENT_TYPE_COLUMN)

    total = len(rows)
    uploaded = 0
    skipped = 0
    failed = 0

    for i, row in enumerate(rows, start=1):
        sku = (row.get(SKU_COLUMN) or "").strip()
        image_url = (row.get(IMAGE_LINK_COLUMN) or "").strip()
        already = (row.get(OUTPUT_COLUMN) or "").strip()
        label = f"[{i}/{total}] SKU={sku or '?'}"

        if already:
            print(f"{label} — already has a Supabase link, skipping")
            skipped += 1
            continue

        if not image_url:
            print(f"{label} — no Image Link, skipping")
            row[OUTPUT_COLUMN] = ""
            skipped += 1
            continue

        if args.dry_run:
            print(f"{label} — would download {image_url}")
            row[OUTPUT_COLUMN] = ""
            continue

        try:
            content, content_type = _download_image(image_url)
            content_type = content_type or "image/jpeg"
            # No extension in the storage key — rendering relies purely on the
            # Content-Type header, so a SKU's path (and public URL) never
            # changes even if a later replacement image is a different format.
            storage_path = _sanitize(sku or str(i))

            sb.storage.from_(args.bucket).upload(
                storage_path,
                content,
                file_options={"content-type": content_type, "upsert": "true"},
            )
            public_url = sb.storage.from_(args.bucket).get_public_url(storage_path)
            row[OUTPUT_COLUMN] = public_url
            row[CONTENT_TYPE_COLUMN] = content_type
            uploaded += 1
            print(f"{label} — uploaded → {public_url}")
        except Exception as exc:  # noqa: BLE001
            row[OUTPUT_COLUMN] = ""
            row[CONTENT_TYPE_COLUMN] = ""
            failed += 1
            print(f"{label} — FAILED: {exc}")

        # Write progress incrementally so a crash partway through doesn't lose work
        if i % 20 == 0 or i == total:
            with output_path.open("w", newline="", encoding="utf-8") as out:
                writer = csv.DictWriter(out, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)

    with output_path.open("w", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(
        f"\nDone — {uploaded} uploaded, {skipped} skipped, {failed} failed, "
        f"{total} total. Output: {output_path}"
    )


if __name__ == "__main__":
    main()
