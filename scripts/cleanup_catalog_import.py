"""
cleanup_catalog_import.py
---------------------------
Removes everything created by upload_catalog_images.py + register_catalog_assets.py:
  1. All `assets` rows with generator='catalog-import' (cascades to asset_concept_tags)
  2. All objects in the product-catalog-images storage bucket

Run:
  cd fastAPI&backend
  python3 scripts/cleanup_catalog_import.py --dry-run   # preview
  python3 scripts/cleanup_catalog_import.py              # actually delete

Env vars required (read from fastAPI&backend/.env):
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from supabase import Client, create_client

BUCKET = "product-catalog-images"
GENERATOR_MARKER = "catalog-import"


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    sb: Client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])

    assets_res = sb.table("assets").select("id,filename").eq("generator", GENERATOR_MARKER).execute()
    asset_rows = assets_res.data or []

    object_names: list[str] = []
    offset = 0
    while True:
        page = sb.storage.from_(BUCKET).list(options={"limit": 1000, "offset": offset})
        if not page:
            break
        object_names.extend(o["name"] for o in page)
        if len(page) < 1000:
            break
        offset += 1000

    print(f"assets rows to delete (generator='{GENERATOR_MARKER}'): {len(asset_rows)}")
    print(f"storage objects to delete (bucket='{BUCKET}'): {len(object_names)}")

    if args.dry_run:
        print("\n--dry-run: nothing deleted.")
        return

    if asset_rows:
        sb.table("assets").delete().eq("generator", GENERATOR_MARKER).execute()
        print(f"Deleted {len(asset_rows)} assets rows (asset_concept_tags cascaded automatically).")

    if object_names:
        # storage remove() accepts up to ~1000 paths per call; chunk to be safe
        for i in range(0, len(object_names), 100):
            chunk = object_names[i:i + 100]
            sb.storage.from_(BUCKET).remove(chunk)
        print(f"Deleted {len(object_names)} storage objects from '{BUCKET}'.")

    print("\nDone.")


if __name__ == "__main__":
    main()
