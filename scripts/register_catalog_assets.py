"""
register_catalog_assets.py
----------------------------
Follow-up to upload_catalog_images.py: reads the CSV it produced (which has
a populated "Supabase Image Link" column) and registers each uploaded image
as a row in the `assets` table, so it shows up on the Assets Library page.

Tags every asset visible to all three brands (Market, Restaurant, Gastro Bar)
via the asset_concept_tags table (see scripts/012_asset_concept_tags.sql) —
assets.concept_id itself is set to Market since that's the "primary" brand
these catalog items were sourced for.

Run:
  cd fastAPI&backend
  python3 scripts/register_catalog_assets.py path/to/sasaa_with_supabase.csv

Optional:
  --dry-run   don't write anything, just report what would happen

Re-running is safe and edit-in-place: a row whose storage_path already has an
assets row gets its filename/mime_type/metadata *updated* (same id, same
public_url) instead of creating a duplicate — so fixing a catalog row's Item
Name/Category/Price and re-running actually applies the correction.

Env vars required (read from fastAPI&backend/.env):
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY
"""
from __future__ import annotations

import argparse
import csv
import mimetypes
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from supabase import Client, create_client

SKU_COLUMN = "SKU"
ITEM_NAME_COLUMN = "Item Name"
PRICE_COLUMN = "Price (KES)"
CATEGORY_COLUMN = "Category"
BARCODE_COLUMN = "Barcode"
SUPABASE_LINK_COLUMN = "Supabase Image Link"
CONTENT_TYPE_COLUMN = "Content Type"

PRIMARY_CONCEPT_KEY = "market"
ALL_CONCEPT_KEYS = ["market", "restaurant", "gastro_bar"]


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


def _storage_path_from_url(url: str) -> str:
    """Extract the bucket-relative storage path from a Supabase public URL
    (…/storage/v1/object/public/<bucket>/<path>)."""
    marker = "/object/public/"
    idx = url.find(marker)
    if idx < 0:
        return Path(urlparse(url).path).name
    rest = url[idx + len(marker):]
    return rest.split("/", 1)[1] if "/" in rest else rest


def _parse_price(raw: str) -> float | None:
    try:
        return float((raw or "").replace(",", "").strip())
    except ValueError:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_csv", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.input_csv.exists():
        sys.exit(f"Input CSV not found: {args.input_csv}")

    with args.input_csv.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if SUPABASE_LINK_COLUMN not in (reader.fieldnames or []):
        sys.exit(f"Expected a '{SUPABASE_LINK_COLUMN}' column — run upload_catalog_images.py first")

    sb: Client | None = None
    concept_ids: dict[str, str] = {}
    if not args.dry_run:
        sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
        concepts_res = sb.table("concepts").select("id,key").in_("key", ALL_CONCEPT_KEYS).execute()
        concept_ids = {c["key"]: c["id"] for c in (concepts_res.data or [])}
        missing = [k for k in ALL_CONCEPT_KEYS if k not in concept_ids]
        if missing:
            sys.exit(f"Could not resolve concept id(s) for: {missing}")

    total = len(rows)
    inserted = 0
    updated = 0
    skipped = 0
    failed = 0

    for i, row in enumerate(rows, start=1):
        sku = (row.get(SKU_COLUMN) or "").strip()
        public_url = (row.get(SUPABASE_LINK_COLUMN) or "").strip()
        label = f"[{i}/{total}] SKU={sku or '?'}"

        if not public_url:
            print(f"{label} — no Supabase Image Link, skipping")
            skipped += 1
            continue

        storage_path = _storage_path_from_url(public_url)
        filename = Path(storage_path).name
        content_type = (row.get(CONTENT_TYPE_COLUMN) or "").strip()
        mime_type = content_type or mimetypes.guess_type(filename)[0] or "image/jpeg"
        item_name = (row.get(ITEM_NAME_COLUMN) or "").strip()
        category = (row.get(CATEGORY_COLUMN) or "").strip()
        price = _parse_price(row.get(PRICE_COLUMN) or "")
        barcode = (row.get(BARCODE_COLUMN) or "").strip()

        if args.dry_run:
            print(f"{label} — would register/update '{item_name}' ({storage_path}) tagged to {ALL_CONCEPT_KEYS}")
            continue

        try:
            existing = (
                sb.table("assets")
                .select("id")
                .eq("storage_path", storage_path)
                .limit(1)
                .execute()
            )

            metadata = {
                "description": item_name or None,
                "tags": [category] if category else [],
                "sku": sku or None,
                "barcode": barcode or None,
                "price_kes": price,
            }
            metadata = {k: v for k, v in metadata.items() if v not in (None, [], "")}

            if existing.data:
                asset_id = existing.data[0]["id"]
                sb.table("assets").update({
                    "filename": filename,
                    "mime_type": mime_type,
                    "metadata": metadata,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", asset_id).execute()
                updated += 1
                print(f"{label} — updated '{item_name}' (same id/URL)")
            else:
                insert_res = (
                    sb.table("assets")
                    .insert({
                        "concept_id": concept_ids[PRIMARY_CONCEPT_KEY],
                        "filename": filename,
                        "storage_path": storage_path,
                        "public_url": public_url,
                        "mime_type": mime_type,
                        "asset_type": "image",
                        "generator": "catalog-import",
                        "metadata": metadata,
                    })
                    .execute()
                )
                asset_id = insert_res.data[0]["id"]
                inserted += 1
                print(f"{label} — registered '{item_name}'")

            # Trigger auto-tags the primary concept on insert; upsert the rest
            # of the brands here so re-runs always converge to full coverage.
            extra_tags = [
                {"asset_id": asset_id, "concept_id": concept_ids[key]}
                for key in ALL_CONCEPT_KEYS
                if key != PRIMARY_CONCEPT_KEY
            ]
            if extra_tags:
                sb.table("asset_concept_tags").upsert(extra_tags, on_conflict="asset_id,concept_id").execute()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"{label} — FAILED: {exc}")

    print(
        f"\nDone — {inserted} registered, {updated} updated, {skipped} skipped, "
        f"{failed} failed, {total} total."
    )


if __name__ == "__main__":
    main()
