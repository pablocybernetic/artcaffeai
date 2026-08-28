"""
import_existing_place_ids.py
------------------------------
One-time import of the 9 Google Place IDs currently hardcoded in the
Artcaffe Market Shopify theme (sections/store-finder-section.liquid:254-264)
into the new `locations` table — per the migration strategy, Shopify's
hardcoded array only gets removed once this data has been verified.

All 9 came from the Artcaffe Market theme, so they import as
brand_type='artcaffe_market' — reassign any individual location's brand
afterward in the /locations admin UI if one turns out to actually be an
Artcaffe restaurant branch.

Run once, locally, against production:
    cd fastAPI&backend
    python3 scripts/import_existing_place_ids.py

If a Google Places API key has already been saved in Admin Settings
(Settings → Integrations → Locations & Google Integration), each newly
inserted row is synced immediately so it's not sitting empty. If no key
is configured yet, rows are still created (place_id + name + slug only)
and the script says so — sync them from the /locations admin UI (or
wait for the next scheduled run) once a key is added.

Mirrors this session's backfill_social_posts.py precedent: a throwaway
script, not a permanent code path, run once against the live database
using the service-role key from .env (never printed).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

from supabase import create_client  # noqa: E402

# (place_id, name, slug, city) — city is a best-effort guess from the
# branch name; correct it in the admin UI if wrong.
KNOWN_PLACE_IDS = [
    ("ChIJexgfp4EXLxgRCliN79GHSfg", "Artcaffe Market Rhapta Road", "rhapta-road", "Nairobi"),
    ("ChIJgQRVeuURLxgR2Qm_d3nJwpE", "Artcaffe Market Dennis Pritt Road", "dennis-pritt-road", "Nairobi"),
    ("ChIJ3dW8DIcbLxgRT7QIb61k0Gg", "Artcaffe Market Lavington", "lavington", "Nairobi"),
    ("ChIJuTzPxkMXLxgRksjtHNvA3jM", "Artcaffe Market Village Market", "village-market", "Nairobi"),
    ("ChIJqwgcTOcRgBcRP1IqwEW6ngg", "Artcaffe Market Kisumu", "kisumu", "Kisumu"),
    ("ChIJq4LB0z33hxcRuKq-JpHomVM", "Artcaffe Market Nanyuki", "nanyuki", "Nanyuki"),
    ("ChIJgwZDvXwXLxgRPVBc08eC7l0", "Artcaffe Market Westview", "westview", "Nairobi"),
    ("ChIJOwE-vLkLKRgRak2Xp178jGU", "Artcaffe Market Naivasha", "naivasha", "Naivasha"),
    ("ChIJwbzguh4RLxgRXqvMd_KlrwI", "Artcaffe Market Kileleshwa", "kileleshwa", "Nairobi"),
]


def _load_env() -> dict:
    env_path = Path(__file__).parent.parent / ".env"
    env: dict[str, str] = {}
    with env_path.open() as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main() -> None:
    env = _load_env()
    sb = create_client(env["SUPABASE_URL"], env["SUPABASE_SERVICE_ROLE_KEY"])

    inserted = []
    for place_id, name, slug, city in KNOWN_PLACE_IDS:
        existing = sb.table("locations").select("id").eq("google_place_id", place_id).limit(1).execute()
        if existing.data:
            print(f"  skip (already exists): {name}")
            continue
        row = {
            "brand_type": "artcaffe_market",
            "name": name,
            "slug": slug,
            "google_place_id": place_id,
            "status": "active",
            "country": "Kenya",
            "city": city,
            "schema_type": "GroceryStore",
            "sync_from_google": True,
        }
        res = sb.table("locations").insert(row).execute()
        loc = res.data[0]
        inserted.append(loc)
        print(f"  inserted: {name} ({loc['id']})")

    print(f"\n{len(inserted)} new location(s) inserted (of {len(KNOWN_PLACE_IDS)} known Place IDs).")

    if not inserted:
        return

    from app_settings import get_setting  # noqa: E402
    saved = get_setting(sb, "locations_google_integration", {}) or {}
    if not saved.get("places_api_key_enc"):
        print(
            "\nNo Google Places API key configured yet — these rows only "
            "have place_id/name/slug for now. Add a key under Settings → "
            "Integrations → Locations & Google Integration, then click "
            "'Sync All Locations' (or wait for the next scheduled run)."
        )
        return

    from secrets_crypto import decrypt_value  # noqa: E402
    from google_location_sync_service import sync_location  # noqa: E402

    api_key = decrypt_value(saved["places_api_key_enc"])
    print("\nGoogle Places API key found — running an initial sync for each new location:")
    for loc in inserted:
        result = sync_location(sb, loc, api_key, sync_fields=saved.get("sync_fields"))
        status = "ok" if result["status"] != "failed" else f"FAILED: {result['error']}"
        print(f"  {loc['name']}: {status}")


if __name__ == "__main__":
    main()
