"""
import_restaurant_locations.py
--------------------------------
One-time import of Artcaffe's restaurant-family physical locations
(Artcaffe Restaurant, Artcaffe Gastro Bar, Artcaffe To Go, Urban Burgers)
into the `locations` table.

Unlike Artcaffe Market, artcaffe.co.ke's live store locator doesn't call
Google Places directly or embed Place IDs in the theme — it calls a
third-party locator backend (rightchoice.ai). That backend was queried
directly (POST https://prod-backend.rightchoice.ai/sl-api/stores-list-2)
on 2026-08-28 and returned 31 stores with real name/address/phone/
coordinates, but identified each only by a Google CID (via a
maps.google.com?cid=... link), not a Place ID. The 3 Artcaffe Market
branches in that response (AM006/AM007/AM008) are skipped here — they
were already imported with real Place IDs by import_existing_place_ids.py
from the Shopify Market theme directly.

brand_type assignment (per user decision):
  - "Artcaffé Restaurant", "Artcaffé To Go", "Urban Burgers" -> artcaffe_restaurant
  - "Artcaffé Gastro Bar"                                    -> artcaffe_gastro_bar
    (requires migration 025_add_gastro_bar_brand.sql to have been applied)

For each store, this script resolves a real google_place_id via Google's
Places API (New) Text Search (name + address, biased to the store's own
coordinates) — requires a Places API key to already be configured in
Admin Settings (Settings -> Integrations -> Locations & Google
Integration). If no key is configured, rows are still inserted with the
real address/phone/coordinates from rightchoice.ai (much richer than a
bare placeholder) but without a place_id or a Google sync — add a key and
run "Sync All Locations" (or rerun this script) once one is set.

Run once, locally, against production:
    cd "fastAPI&backend"
    python3 scripts/import_restaurant_locations.py

Mirrors import_existing_place_ids.py: a throwaway script, not a permanent
code path, run once against the live database using the service-role key
from .env (never printed).
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

from supabase import create_client  # noqa: E402

# (store_code, title, address_lines, locality, admin_area, phone, lat, lng)
# Sourced verbatim from rightchoice.ai's stores-list-2 response for
# domain=artcaffe-sl.rightchoice.ai on 2026-08-28. AM006/AM007/AM008
# (Artcaffe Market) intentionally excluded — already imported separately.
STORES = [
    ("007", "Artcaffé Restaurant Britam Towers", "Britam Towers, Hospital Road", "Nairobi", "Nairobi", "0709 828282", -1.3000986, 36.8128159),
    ("03033129144370980276", "Artcaffé Restaurant Riverside Drive", "Riverside Drive, The Cube, Ground floor", "Nairobi", "Nairobi", "0709 828282", -1.2700853, 36.7963263),
    ("036", "Artcaffé Restaurant Aga Khan Sports Center", "Aga Khan Sports Center, Mtama Rd", "Nairobi", "Nairobi", "0709 828282", -1.2599029, 36.8235142),
    ("01548763225411923922", "Artcaffé Gastro Bar Westlands Square", "Westlands Square, Waiyaki Way", "Nairobi", "Nairobi", "0709 202022", -1.263561, 36.8018815),
    ("04349770160826118990", "Artcaffé Restaurant Eastleigh Business Bay Mall", "Eastleigh Business Bay Mall, General Waruinge Street", "Nairobi", "Nairobi", "0709 828282", -1.2798073, 36.846583),
    ("015", "Artcaffé Restaurant Yaya Centre", "Yaya Centre, Ring Road - Argwings Kodhek Junction, Kilimani", "Nairobi", "Nairobi", "0709 828282", -1.2926653, 36.7874837),
    ("017", "Artcaffé Restaurant Mwanzi Road", "Westgate Mall, Mwanzi Road, Westlands", "Nairobi", "Nairobi", "0709 828282", -1.2576135, 36.8035936),
    ("001", "Artcaffé Restaurant Capital Centre Mall", "Capital Centre Mall, Mombasa Road", "Nairobi", "Nairobi", "0709 828282", -1.3163637, 36.8345577),
    ("016", "Artcaffé Restaurant Rhapta Road Westlands", "Rhapta Square, Rhapta Road, Westlands", "Nairobi", "Nairobi", "0709 828282", -1.2646085, 36.7891449),
    ("014", "Artcaffé Restaurant Valley Arcade", "Gitanga Road, Valley Arcade", "Nairobi", "Nairobi", "0709 828282", -1.2905137, 36.7703005),
    ("021", "Artcaffé Restaurant Lavington", "Lavington Mall, James Gichuru Road, Lavington", "Nairobi", "Nairobi", "0709 828282", -1.2802215, 36.7700661),
    ("17596615850721542974", "Artcaffé Restaurant St Austin Shell Lavington", "Cnr James Gichuru & Ndoto Road", "Nairobi", "Nairobi", "0709 828282", -1.2808489, 36.7694126),
    ("012a", "Artcaffé Restaurant The Junction Mall", "The Junction Mall, Junction Of Ngong Road & Kingara Rd, Dagoretti", "Nairobi", "Nairobi", "0709 828282", -1.2984917, 36.7616911),
    ("08042557414941159413", "Artcaffé Restaurant Thika Road", "At Shell Petrol Station, Exit 5, Thika Road", "Nairobi", "Nairobi", "0709 828282", -1.2497069, 36.8612303),
    ("029", "Artcaffé Restaurant Gigiri", "The Village Market, Limuru Road, Gigiri", "Nairobi", "Nairobi", "0709 828282", -1.2291457, 36.8048151),
    ("AGB002", "Artcaffé Gastro Bar Imaara Mall", "Imaara Mall, Mombasa Rd., Imara Daima", "Nairobi", "Nairobi", "0709 828282", -1.3276848, 36.8815644),
    ("00398302338496447743", "Urban Burgers Galleria Mall", "First Floor, Galleria Mall, Langata Road", "Nairobi", "Nairobi", "0709 828282", -1.3435209, 36.7659534),
    ("009", "Artcaffé Restaurant Galleria Mall", "Galleria Mall, Junction of Magadi & Langata Rd", "Nairobi", "Nairobi", "0709 828282", -1.343568, 36.7659115),
    ("025", "Artcaffé Restaurant Runda", "Two Rivers, Limuru Rd, Runda", "Nairobi", "Nairobi", "0709 828282", -1.2103777, 36.7959836),
    ("005", "Artcaffé Restaurant Garden City Mall", "Garden City Mall, Thika Road", "Nairobi", "Nairobi", "0709 828282", -1.232259, 36.8785603),
    ("004", "Artcaffé Restaurant TRM Mall", "Thika Road Mall, 2nd Floor, Off Thika Road, Kasarani", "Nairobi", "Nairobi", "0709 828282", -1.2195872, 36.8884263),
    ("030", "Artcaffé Restaurant Karen", "The Hub Karen, Dagoretti Road, Karen", "Nairobi", "Nairobi", "0709 828282", -1.3193904, 36.7033429),
    ("027", "Artcaffé Restaurant Eastern Bypass", "Eastern Bypass", "Nairobi", "Nairobi", "0709 828282", -1.170376, 36.9702557),
    ("024", "Artcaffé Restaurant Kitengela Mall", "Kitengela Mall, Nairobi-Namanga Road", "Kitengela", "Kitengela", "0709 828282", -1.4791057, 36.9583922),
    ("012b", "Artcaffé Restaurant Greenpark Estate", "Greenpark Estate, Mombasa Road, Athi River", "Nairobi", "Nairobi", "0709 828282", -1.4612047, 37.0105077),
    ("061", "Artcaffé Restaurant Narok Suswa", "Shell Suswa Service Station", "Narok", "Narok", "0709 828282", -0.994872, 36.5461101),
    ("A003", "Artcaffé To Go Karagita", "Shell Petrol Station-Karai, Karagita", "Karagita", "Karagita", "0709 828282", -0.769562, 36.5013332),
    ("060", "Artcaffé Restaurant Narok Mara Junction", "Rubis Mara Junction Service Station, B3", "Narok", "Narok", "0709 828282", -1.0934634, 35.8697552),
]


def _brand_type(title: str) -> str:
    return "artcaffe_gastro_bar" if "gastro bar" in title.lower() else "artcaffe_restaurant"


def _slugify(text: str) -> str:
    text = text.lower().replace("é", "e")
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text


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

    from app_settings import get_setting  # noqa: E402

    saved = get_setting(sb, "locations_google_integration", {}) or {}
    api_key = None
    if saved.get("places_api_key_enc"):
        from secrets_crypto import decrypt_value  # noqa: E402
        api_key = decrypt_value(saved["places_api_key_enc"])

    if api_key:
        print("Places API key found — will resolve a real google_place_id per location.\n")
    else:
        print(
            "No Places API key configured yet — inserting rows with real "
            "address/phone/coordinates but no google_place_id. Add a key "
            "under Settings -> Integrations -> Locations & Google "
            "Integration and rerun this script to backfill place_ids and "
            "sync full details.\n"
        )

    from google_location_sync_service import sync_location  # noqa: E402
    from connectors.google_places_connector import find_place_id  # noqa: E402

    inserted = []
    skipped = []
    for store_code, title, address_line, locality, admin_area, phone, lat, lng in STORES:
        existing = sb.table("locations").select("id").eq("name", title).limit(1).execute()
        if existing.data:
            skipped.append(title)
            print(f"  skip (already exists): {title}")
            continue

        row = {
            "brand_type": _brand_type(title),
            "name": title,
            "slug": f"{_slugify(title)}-{store_code.lower()}",
            "store_code": store_code,
            "status": "active",
            "country": "Kenya",
            "county": admin_area,
            "city": locality,
            "address": address_line,
            "phone": phone,
            "latitude": lat,
            "longitude": lng,
            "schema_type": "Restaurant",
            "sync_from_google": bool(api_key),
        }

        if api_key:
            try:
                place_id = find_place_id(f"{title}, {address_line}, Kenya", api_key, lat=lat, lng=lng)
                if place_id:
                    row["google_place_id"] = place_id
                else:
                    print(f"    no Google match found for: {title}")
            except RuntimeError as exc:
                print(f"    Text Search failed for {title}: {exc}")

        res = sb.table("locations").insert(row).execute()
        loc = res.data[0]
        inserted.append(loc)
        print(f"  inserted: {title} ({loc['id']}) place_id={row.get('google_place_id')}")
        time.sleep(0.3)  # be polite to the Text Search API between calls

    print(f"\n{len(inserted)} new location(s) inserted, {len(skipped)} already existed (of {len(STORES)} total).")

    if not api_key or not inserted:
        return

    print("\nRunning an initial full sync for each newly resolved location:")
    for loc in inserted:
        if not loc.get("google_place_id"):
            continue
        result = sync_location(sb, loc, api_key, sync_fields=saved.get("sync_fields"))
        status = "ok" if result["status"] != "failed" else f"FAILED: {result['error']}"
        print(f"  {loc['name']}: {status}")


if __name__ == "__main__":
    main()
