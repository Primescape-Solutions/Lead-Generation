#!/usr/bin/env python3
"""
Hospitality Lead Generator
===========================
Finds hotel and restaurant owners/managers near Elkin, NC (28621)
within a 50-mile radius using the Google Places API.

Collects exactly 100 qualified leads (must have email), scores them,
and exports to CSV sorted by lead score.

Usage:
    python lead_generator.py

Requires:
    - Google Places API key (set in .env file or GOOGLE_PLACES_API_KEY env var)
    - See requirements.txt for Python dependencies
"""

import csv
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")

# Elkin, NC coordinates
CENTER_LAT = 36.2440
CENTER_LNG = -80.8487

# 50 miles in meters
RADIUS_METERS = 80467

# Business types to search for
SEARCH_QUERIES = [
    ("hotel", "lodging"),
    ("motel", "lodging"),
    ("inn", "lodging"),
    ("bed and breakfast", "lodging"),
    ("resort", "lodging"),
    ("restaurant", "restaurant"),
    ("dining", "restaurant"),
    ("cafe", "restaurant"),
    ("bistro", "restaurant"),
    ("bar and grill", "restaurant"),
]

TARGET_LEAD_COUNT = 100
OUTPUT_FILE = "hospitality_leads.csv"

# Rate limiting: Google Places API has per-second limits
API_DELAY = 0.15  # seconds between requests

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Lead:
    business_name: str = ""
    email: str = ""
    phone: str = ""
    address: str = ""
    business_type: str = ""
    website: str = ""
    google_rating: float = 0.0
    num_reviews: int = 0
    has_parking: bool = False
    large_parking: bool = False
    recent_reviews: bool = False
    lead_score: int = 0
    notes: str = ""
    place_id: str = ""


# ---------------------------------------------------------------------------
# Google Places API helpers
# ---------------------------------------------------------------------------


def _api_get(url: str, params: dict, retries: int = 3) -> dict:
    """Make a GET request to the Google API with retry logic."""
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            status = data.get("status", "OK")
            if status == "REQUEST_DENIED":
                log.error("API request denied: %s", data.get("error_message", ""))
                sys.exit(1)
            if status == "OVER_QUERY_LIMIT":
                wait = 2 ** (attempt + 1)
                log.warning("Rate limited. Waiting %ds before retry...", wait)
                time.sleep(wait)
                continue
            return data
        except requests.RequestException as exc:
            if attempt < retries - 1:
                wait = 2 ** (attempt + 1)
                log.warning("Request failed (%s). Retrying in %ds...", exc, wait)
                time.sleep(wait)
            else:
                log.error("Request failed after %d retries: %s", retries, exc)
                raise
    return {}


def search_nearby(query: str, page_token: str | None = None) -> dict:
    """Search for places using the Text Search endpoint."""
    url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    params = {
        "query": query,
        "location": f"{CENTER_LAT},{CENTER_LNG}",
        "radius": RADIUS_METERS,
        "key": API_KEY,
    }
    if page_token:
        params["pagetoken"] = page_token
        # Google requires a short delay before using next_page_token
        time.sleep(2)
    time.sleep(API_DELAY)
    return _api_get(url, params)


def get_place_details(place_id: str) -> dict:
    """Fetch full details for a single place."""
    url = "https://maps.googleapis.com/maps/api/place/details/json"
    params = {
        "place_id": place_id,
        "fields": (
            "name,formatted_address,formatted_phone_number,website,"
            "rating,user_ratings_total,reviews,photos,types,url,"
            "business_status"
        ),
        "key": API_KEY,
    }
    time.sleep(API_DELAY)
    data = _api_get(url, params)
    return data.get("result", {})


# ---------------------------------------------------------------------------
# Email extraction
# ---------------------------------------------------------------------------


EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", re.IGNORECASE
)


def extract_email_from_website(website_url: str) -> str:
    """Try to scrape an email address from the business website."""
    if not website_url:
        return ""

    # Normalize URL
    if not website_url.startswith(("http://", "https://")):
        website_url = "https://" + website_url

    pages_to_try = [website_url]
    # Common contact page paths
    for suffix in ["/contact", "/contact-us", "/about", "/about-us"]:
        base = website_url.rstrip("/")
        pages_to_try.append(base + suffix)

    for page_url in pages_to_try:
        try:
            resp = requests.get(
                page_url,
                timeout=10,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    )
                },
                allow_redirects=True,
            )
            if resp.status_code != 200:
                continue
            emails = EMAIL_RE.findall(resp.text)
            # Filter out common false positives
            filtered = [
                e
                for e in emails
                if not e.endswith((".png", ".jpg", ".gif", ".svg", ".css", ".js"))
                and "example.com" not in e
                and "sentry.io" not in e
                and "wixpress.com" not in e
                and "wordpress" not in e.lower()
                and "schema.org" not in e
            ]
            if filtered:
                return filtered[0].lower()
        except Exception:
            continue
    return ""


# ---------------------------------------------------------------------------
# Parking lot detection (heuristic from photos metadata)
# ---------------------------------------------------------------------------


def check_parking(details: dict) -> tuple[bool, bool]:
    """
    Estimate whether the business has a parking lot and its size.

    Uses the number and types of photos as a proxy -- businesses with
    more photos tagged by Google tend to be larger establishments
    with parking.  Also checks the place types for "parking" hints.

    Returns (has_parking, large_parking).
    """
    has_parking = False
    large_parking = False

    photos = details.get("photos", [])
    types = details.get("types", [])

    # If Google marks it as lodging/hotel it almost certainly has parking
    if any(t in types for t in ("lodging", "hotel", "resort_hotel")):
        has_parking = True
        # Hotels with many photos tend to be larger
        if len(photos) >= 8:
            large_parking = True

    # Restaurants with many photos are likely larger establishments
    if any(t in types for t in ("restaurant", "bar", "cafe")):
        if len(photos) >= 5:
            has_parking = True
        if len(photos) >= 10:
            large_parking = True

    # High review counts suggest a well-visited place with parking
    num_reviews = details.get("user_ratings_total", 0)
    if num_reviews >= 200:
        has_parking = True
    if num_reviews >= 500:
        large_parking = True

    return has_parking, large_parking


# ---------------------------------------------------------------------------
# Recent reviews check
# ---------------------------------------------------------------------------


def has_recent_reviews(details: dict, months: int = 3) -> bool:
    """Check if any reviews were posted within the last N months."""
    reviews = details.get("reviews", [])
    if not reviews:
        return False
    cutoff = time.time() - (months * 30 * 24 * 3600)
    for review in reviews:
        review_time = review.get("time", 0)
        if review_time >= cutoff:
            return True
    return False


# ---------------------------------------------------------------------------
# Lead scoring
# ---------------------------------------------------------------------------


def score_lead(lead: Lead) -> int:
    """Calculate lead score based on the defined rubric."""
    score = 0

    # Email is required -- caller should have already filtered, but guard
    if lead.email:
        score += 10
    else:
        return 0

    if lead.phone:
        score += 5
    if lead.website:
        score += 5
    if lead.has_parking:
        score += 15
    if lead.large_parking:
        score += 10
    if lead.google_rating >= 4.0:
        score += 5
    if lead.num_reviews >= 50:
        score += 5
    if lead.recent_reviews:
        score += 5

    return score


# ---------------------------------------------------------------------------
# Main collection logic
# ---------------------------------------------------------------------------


def classify_business_type(types: list[str], query_type: str) -> str:
    """Return a human-readable business type string."""
    lodging_types = {"lodging", "hotel", "resort_hotel", "motel"}
    food_types = {"restaurant", "bar", "cafe", "bakery", "meal_delivery", "meal_takeaway"}

    type_set = set(types)
    if type_set & lodging_types:
        return "Hotel / Lodging"
    if type_set & food_types:
        return "Restaurant / Dining"
    # Fall back to the query category
    if query_type == "lodging":
        return "Hotel / Lodging"
    return "Restaurant / Dining"


def build_notes(lead: Lead) -> str:
    """Build a human-readable notes string."""
    parts = []
    if lead.has_parking:
        parts.append("Parking lot detected")
    if lead.large_parking:
        parts.append("Large parking area")
    if lead.recent_reviews:
        parts.append("Recent review activity")
    if lead.google_rating >= 4.5:
        parts.append("Highly rated")
    if lead.num_reviews >= 100:
        parts.append("Popular venue")
    return "; ".join(parts)


def collect_leads() -> list[Lead]:
    """Run the full lead collection pipeline."""
    seen_place_ids: set[str] = set()
    qualified_leads: list[Lead] = []
    total_scanned = 0
    skipped_no_email = 0

    log.info("Starting lead collection for hospitality businesses near Elkin, NC")
    log.info("Target: %d qualified leads (must have email)", TARGET_LEAD_COUNT)
    log.info("Search radius: 50 miles from Elkin, NC (28621)")
    log.info("-" * 60)

    for query_text, query_type in SEARCH_QUERIES:
        if len(qualified_leads) >= TARGET_LEAD_COUNT:
            break

        full_query = f"{query_text} near Elkin NC"
        log.info("Searching: '%s'", full_query)

        page_token = None
        pages_fetched = 0

        while True:
            if len(qualified_leads) >= TARGET_LEAD_COUNT:
                break

            result = search_nearby(full_query, page_token)
            places = result.get("results", [])

            if not places:
                log.info("  No results for this query/page.")
                break

            pages_fetched += 1
            log.info(
                "  Page %d: %d places found (qualified so far: %d/%d)",
                pages_fetched,
                len(places),
                len(qualified_leads),
                TARGET_LEAD_COUNT,
            )

            for place in places:
                if len(qualified_leads) >= TARGET_LEAD_COUNT:
                    break

                place_id = place.get("place_id", "")
                if not place_id or place_id in seen_place_ids:
                    continue
                seen_place_ids.add(place_id)
                total_scanned += 1

                name = place.get("name", "Unknown")

                # Skip permanently closed businesses
                if place.get("business_status") == "CLOSED_PERMANENTLY":
                    log.debug("  Skipping closed business: %s", name)
                    continue

                # Fetch full details
                details = get_place_details(place_id)
                if not details:
                    continue

                # Extract website and try to find email
                website = details.get("website", "")
                email = extract_email_from_website(website)

                # REQUIRED: skip leads without email
                if not email:
                    skipped_no_email += 1
                    if skipped_no_email % 20 == 0:
                        log.info(
                            "  [%d leads skipped so far -- no email found]",
                            skipped_no_email,
                        )
                    continue

                # Build the lead
                has_park, large_park = check_parking(details)
                recent = has_recent_reviews(details)
                types = details.get("types", []) or place.get("types", [])

                lead = Lead(
                    business_name=details.get("name", name),
                    email=email,
                    phone=details.get("formatted_phone_number", ""),
                    address=details.get("formatted_address", ""),
                    business_type=classify_business_type(types, query_type),
                    website=website,
                    google_rating=details.get("rating", 0.0),
                    num_reviews=details.get("user_ratings_total", 0),
                    has_parking=has_park,
                    large_parking=large_park,
                    recent_reviews=recent,
                    place_id=place_id,
                )
                lead.lead_score = score_lead(lead)
                lead.notes = build_notes(lead)

                qualified_leads.append(lead)
                log.info(
                    "  + Lead #%d: %s (score: %d, email: %s)",
                    len(qualified_leads),
                    lead.business_name,
                    lead.lead_score,
                    lead.email,
                )

            # Check for next page
            page_token = result.get("next_page_token")
            if not page_token:
                break

    log.info("-" * 60)
    log.info("Collection complete.")
    log.info("  Total places scanned: %d", total_scanned)
    log.info("  Skipped (no email):   %d", skipped_no_email)
    log.info("  Qualified leads:      %d", len(qualified_leads))

    return qualified_leads


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

CSV_COLUMNS = [
    "Business Name",
    "Email",
    "Phone",
    "Address",
    "Business Type",
    "Website",
    "Google Rating",
    "Number of Reviews",
    "Lead Score",
    "Notes",
]


def export_to_csv(leads: list[Lead], filepath: str) -> None:
    """Export leads to CSV, sorted by lead score descending."""
    # Sort highest score first
    leads.sort(key=lambda x: x.lead_score, reverse=True)

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_COLUMNS)
        for lead in leads:
            writer.writerow(
                [
                    lead.business_name,
                    lead.email,
                    lead.phone,
                    lead.address,
                    lead.business_type,
                    lead.website,
                    lead.google_rating,
                    lead.num_reviews,
                    lead.lead_score,
                    lead.notes,
                ]
            )

    log.info("Leads exported to %s (%d rows)", filepath, len(leads))


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    if not API_KEY:
        log.error(
            "Google Places API key not found.\n"
            "Set GOOGLE_PLACES_API_KEY in a .env file or as an environment variable.\n"
            "See README.md for setup instructions."
        )
        sys.exit(1)

    log.info("Google Places API key loaded (ends with ...%s)", API_KEY[-4:])

    leads = collect_leads()

    if not leads:
        log.warning("No qualified leads found. Check your API key and network.")
        sys.exit(1)

    export_to_csv(leads, OUTPUT_FILE)

    # Print summary
    print("\n" + "=" * 60)
    print("LEAD GENERATION COMPLETE")
    print("=" * 60)
    print(f"  Output file : {OUTPUT_FILE}")
    print(f"  Total leads : {len(leads)}")
    if leads:
        scores = [l.lead_score for l in leads]
        print(f"  Score range : {min(scores)} - {max(scores)}")
        print(f"  Avg score   : {sum(scores) / len(scores):.1f}")

        hotels = sum(1 for l in leads if "Hotel" in l.business_type)
        restaurants = sum(1 for l in leads if "Restaurant" in l.business_type)
        print(f"  Hotels      : {hotels}")
        print(f"  Restaurants : {restaurants}")
    print("=" * 60)


if __name__ == "__main__":
    main()
