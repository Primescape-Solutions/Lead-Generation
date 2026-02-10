#!/usr/bin/env python3
"""
Hospitality Lead Generator
===========================
Finds hotel and restaurant owners/managers near Elkin, Wilkesboro,
and Statesville, NC using the Google Places API.

Searches three overlapping 25-mile-radius zones, de-duplicates results,
collects 100 qualified leads (must have email), scores them, determines
the county and tax-assessor lookup link for each, and exports to CSV.

Usage:
    python lead_generator.py

Requires:
    - Google Places API key (set in .env file or GOOGLE_PLACES_API_KEY env var)
    - See requirements.txt for Python dependencies
"""

import csv
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from urllib.parse import quote_plus

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")

# Three search areas — each searched with a 25-mile radius
SEARCH_AREAS = [
    {"name": "Elkin, NC (28621)",       "lat": 36.2440, "lng": -80.8487},
    {"name": "Wilkesboro, NC (28697)",  "lat": 36.1460, "lng": -81.1604},
    {"name": "Statesville, NC (28677)", "lat": 35.7826, "lng": -80.8873},
]

# 25 miles in meters
RADIUS_METERS = 40234

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
    email_source: str = ""  # "Google Places" or "Website Scraped"
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
    search_area: str = ""          # which of the 3 zones found it
    county: str = ""               # NC county name
    tax_assessor_link: str = ""    # clickable URL for property lookup


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


def search_nearby(
    query: str,
    lat: float,
    lng: float,
    page_token: str | None = None,
) -> dict:
    """Search for places using the Text Search endpoint."""
    url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    params = {
        "query": query,
        "location": f"{lat},{lng}",
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
# Email extraction — multi-strategy approach
# ---------------------------------------------------------------------------

# Matches standard email addresses in free text
EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", re.IGNORECASE
)

# Matches mailto: links in HTML (more reliable than free-text regex)
MAILTO_RE = re.compile(r'mailto:([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})', re.IGNORECASE)

# Common hospitality email prefixes to try when constructing guesses
COMMON_PREFIXES = [
    "info",
    "contact",
    "reservations",
    "frontdesk",
    "front.desk",
    "stay",
    "hello",
    "gm",
    "manager",
    "sales",
    "events",
    "booking",
    "inquiries",
    "mail",
    "admin",
]

# Domains / patterns that are false-positive noise, not real business emails
_JUNK_DOMAINS = {
    "example.com",
    "sentry.io",
    "wixpress.com",
    "schema.org",
    "w3.org",
    "googleapis.com",
    "gstatic.com",
    "facebook.com",
    "twitter.com",
    "instagram.com",
    "youtube.com",
    "google.com",
    "cloudflare.com",
    "gravatar.com",
}

_JUNK_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".css", ".js", ".webp")

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# Pages to crawl on a business website, in priority order
_CONTACT_PATHS = [
    "",                # homepage
    "/contact",
    "/contact-us",
    "/contactus",
    "/about",
    "/about-us",
    "/aboutus",
    "/info",
    "/reservations",
    "/book",
    "/location",
    "/locations",
    "/our-story",
    "/connect",
    "/feedback",
    "/reach-us",
]


def _is_junk_email(email: str) -> bool:
    """Return True if the email looks like a false positive."""
    email_lower = email.lower()
    if email_lower.endswith(_JUNK_EXTENSIONS):
        return True
    domain = email_lower.split("@", 1)[-1]
    if domain in _JUNK_DOMAINS:
        return True
    if "wordpress" in email_lower or "noreply" in email_lower or "no-reply" in email_lower:
        return True
    return False


def _domain_from_url(url: str) -> str:
    """Extract the root domain from a URL (e.g. 'example.com')."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        # Strip www.
        if host.startswith("www."):
            host = host[4:]
        return host.lower()
    except Exception:
        return ""


def _rank_emails(emails: list[str], site_domain: str) -> list[str]:
    """
    Sort candidate emails so the best ones come first:
      1. Emails whose domain matches the website domain
      2. Emails with preferred prefixes (info@, contact@, reservations@, …)
      3. Everything else
    """
    preferred_prefix_set = set(COMMON_PREFIXES)

    def _sort_key(email: str) -> tuple[int, int, str]:
        local, _, domain = email.lower().partition("@")
        # Priority 1: domain matches the business website
        domain_match = 0 if (site_domain and domain == site_domain) else 1
        # Priority 2: common business prefix
        prefix_match = 0 if local in preferred_prefix_set else 1
        return (domain_match, prefix_match, email)

    return sorted(emails, key=_sort_key)


def _fetch_page(url: str) -> str:
    """Fetch a single page and return its text, or '' on any error."""
    try:
        resp = requests.get(
            url, timeout=10, headers=HTTP_HEADERS, allow_redirects=True,
        )
        if resp.status_code == 200:
            return resp.text
    except requests.ConnectionError:
        log.debug("  Connection failed: %s", url)
    except requests.Timeout:
        log.debug("  Timeout: %s", url)
    except requests.TooManyRedirects:
        log.debug("  Too many redirects: %s", url)
    except Exception as exc:
        log.debug("  Fetch error for %s: %s", url, exc)
    return ""


def _extract_emails_from_html(html: str) -> list[str]:
    """Pull all candidate emails from a page using both mailto: and free-text regex."""
    found: list[str] = []
    # mailto: links are the highest signal
    found.extend(MAILTO_RE.findall(html))
    # Free-text regex
    found.extend(EMAIL_RE.findall(html))
    # Deduplicate preserving order, filter junk
    seen: set[str] = set()
    clean: list[str] = []
    for e in found:
        e_lower = e.lower()
        if e_lower not in seen and not _is_junk_email(e_lower):
            seen.add(e_lower)
            clean.append(e_lower)
    return clean


def scrape_website_for_email(website_url: str) -> str:
    """
    Crawl a business website to find an email address.

    Tries the homepage first, then common contact/about pages.
    Extracts emails from mailto: links and free-text patterns,
    then ranks them so domain-matching & business-prefix emails
    sort to the top.

    Returns the best email found, or ''.
    """
    if not website_url:
        return ""

    # Normalize
    if not website_url.startswith(("http://", "https://")):
        website_url = "https://" + website_url

    base = website_url.rstrip("/")
    site_domain = _domain_from_url(base)
    all_candidates: list[str] = []

    for path in _CONTACT_PATHS:
        page_url = base + path
        html = _fetch_page(page_url)
        if not html:
            continue
        emails = _extract_emails_from_html(html)
        all_candidates.extend(emails)

        # Also look for links to other contact-like pages embedded in the HTML
        # (e.g. <a href="/contact-us">) — we already cover common paths above,
        # but some sites use non-standard paths like /get-in-touch
        for match in re.finditer(r'href=["\']/?([^"\']*(?:contact|email|reach|connect)[^"\']*)["\']', html, re.IGNORECASE):
            discovered = match.group(1)
            if not discovered.startswith(("http://", "https://")):
                discovered = base + "/" + discovered.lstrip("/")
            # Avoid re-fetching pages we already tried
            if discovered not in (base + p for p in _CONTACT_PATHS):
                extra_html = _fetch_page(discovered)
                if extra_html:
                    all_candidates.extend(_extract_emails_from_html(extra_html))

    # Deduplicate
    seen: set[str] = set()
    unique: list[str] = []
    for e in all_candidates:
        if e not in seen:
            seen.add(e)
            unique.append(e)

    if not unique:
        return ""

    ranked = _rank_emails(unique, site_domain)
    return ranked[0]


def find_email(details: dict) -> tuple[str, str]:
    """
    Multi-strategy email finder.  Returns (email, source) where source is
    one of 'Google Places' or 'Website Scraped'.

    Strategy order:
      1. Check if Google Places details already contain an email (rare but possible
         in the formatted fields or website URL itself being a mailto:).
      2. Scrape the business website for email addresses.
    """
    # --- Strategy 1: Google Places data ---
    # Google sometimes embeds an email in the website field or additional fields
    website = details.get("website", "")
    if website.startswith("mailto:"):
        email = website.replace("mailto:", "").strip()
        if email and not _is_junk_email(email):
            return email.lower(), "Google Places"

    # The formatted_phone_number / formatted_address won't have email,
    # but some custom fields might — check the raw detail blob
    for field_name in ("email", "email_address"):
        val = details.get(field_name, "")
        if val and "@" in val and not _is_junk_email(val):
            return val.lower(), "Google Places"

    # --- Strategy 2: Scrape the website ---
    if website:
        log.debug("  Scraping website for email: %s", website)
        email = scrape_website_for_email(website)
        if email:
            return email, "Website Scraped"

    return "", ""


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
# Address parsing, county lookup, and tax assessor links
# ---------------------------------------------------------------------------

# City/town → county mapping for the NC foothills / piedmont region.
# Covers the 25-mile radius around Elkin, Wilkesboro, and Statesville.
_CITY_TO_COUNTY: dict[str, str] = {
    # Wilkes County
    "wilkesboro": "Wilkes",
    "north wilkesboro": "Wilkes",
    "n wilkesboro": "Wilkes",
    "ronda": "Wilkes",
    "moravian falls": "Wilkes",
    "hays": "Wilkes",
    "purlear": "Wilkes",
    "millers creek": "Wilkes",
    "champion": "Wilkes",
    "traphill": "Wilkes",
    "boomer": "Wilkes",
    "cricket": "Wilkes",
    "mcgrady": "Wilkes",
    "ferguson": "Wilkes",
    "darby": "Wilkes",
    # Surry County
    "elkin": "Surry",
    "mount airy": "Surry",
    "mt airy": "Surry",
    "pilot mountain": "Surry",
    "dobson": "Surry",
    "lowgap": "Surry",
    "state road": "Surry",
    "ararat": "Surry",
    "toast": "Surry",
    "white plains": "Surry",
    "westfield": "Surry",
    "siloam": "Surry",
    # Yadkin County
    "yadkinville": "Yadkin",
    "jonesville": "Yadkin",
    "boonville": "Yadkin",
    "east bend": "Yadkin",
    "hamptonville": "Yadkin",
    "courtney": "Yadkin",
    # Iredell County
    "statesville": "Iredell",
    "mooresville": "Iredell",
    "troutman": "Iredell",
    "harmony": "Iredell",
    "love valley": "Iredell",
    "union grove": "Iredell",
    "cool springs": "Iredell",
    "olin": "Iredell",
    "turnersburg": "Iredell",
    "stony point": "Iredell",
    "hiddenite": "Iredell",
    # Alleghany County
    "sparta": "Alleghany",
    "piney creek": "Alleghany",
    "laurel springs": "Alleghany",
    "ennice": "Alleghany",
    "roaring gap": "Alleghany",
    # Ashe County
    "west jefferson": "Ashe",
    "jefferson": "Ashe",
    "todd": "Ashe",
    "lansing": "Ashe",
    "grassy creek": "Ashe",
    "warrensville": "Ashe",
    "crumpler": "Ashe",
    # Watauga County
    "boone": "Watauga",
    "blowing rock": "Watauga",
    "banner elk": "Watauga",
    "sugar grove": "Watauga",
    "valle crucis": "Watauga",
    "vilas": "Watauga",
    "deep gap": "Watauga",
    # Alexander County
    "taylorsville": "Alexander",
    "bethlehem": "Alexander",
    "stony point": "Alexander",
    "hiddenite": "Alexander",
    # Caldwell County
    "lenoir": "Caldwell",
    "hudson": "Caldwell",
    "granite falls": "Caldwell",
    "sawmills": "Caldwell",
    "gamewell": "Caldwell",
    "patterson": "Caldwell",
    # Catawba County
    "hickory": "Catawba",
    "newton": "Catawba",
    "conover": "Catawba",
    "maiden": "Catawba",
    "claremont": "Catawba",
    "catawba": "Catawba",
    "long view": "Catawba",
    "sherrills ford": "Catawba",
    # Davie County
    "mocksville": "Davie",
    "advance": "Davie",
    "cooleemee": "Davie",
    "bermuda run": "Davie",
    # Stokes County
    "danbury": "Stokes",
    "king": "Stokes",
    "walnut cove": "Stokes",
    "pine hall": "Stokes",
    "germanton": "Stokes",
    # Forsyth County
    "winston-salem": "Forsyth",
    "winston salem": "Forsyth",
    "kernersville": "Forsyth",
    "clemmons": "Forsyth",
    "lewisville": "Forsyth",
    "rural hall": "Forsyth",
    "tobaccoville": "Forsyth",
    "pfafftown": "Forsyth",
    "bethania": "Forsyth",
    # Rowan County
    "salisbury": "Rowan",
    "china grove": "Rowan",
    "spencer": "Rowan",
    "landis": "Rowan",
    "east spencer": "Rowan",
    "rockwell": "Rowan",
    "granite quarry": "Rowan",
    "faith": "Rowan",
    "cleveland": "Rowan",
    # Lincoln County
    "lincolnton": "Lincoln",
    "denver": "Lincoln",
    "iron station": "Lincoln",
    "vale": "Lincoln",
    # Avery County
    "banner elk": "Avery",
    "newland": "Avery",
    "linville": "Avery",
    "elk park": "Avery",
    "crossnore": "Avery",
    # Burke County
    "morganton": "Burke",
    "valdese": "Burke",
    "drexel": "Burke",
    "connelly springs": "Burke",
    "glen alpine": "Burke",
}

# County → online property-search / GIS portal URL.
# Most NC counties use a <county>.webgis.net portal or their own tax site.
_COUNTY_TAX_URLS: dict[str, str] = {
    "Wilkes":    "https://wilkes.webgis.net/",
    "Surry":     "https://surry.webgis.net/",
    "Yadkin":    "https://yadkin.webgis.net/",
    "Iredell":   "https://iredell.webgis.net/",
    "Alleghany": "https://alleghany.webgis.net/",
    "Ashe":      "https://ashe.webgis.net/",
    "Watauga":   "https://watauga.webgis.net/",
    "Alexander": "https://alexander.webgis.net/",
    "Caldwell":  "https://caldwell.webgis.net/",
    "Catawba":   "https://catawba.webgis.net/",
    "Davie":     "https://davie.webgis.net/",
    "Stokes":    "https://stokes.webgis.net/",
    "Forsyth":   "https://forsyth.webgis.net/",
    "Rowan":     "https://rowan.webgis.net/",
    "Lincoln":   "https://lincoln.webgis.net/",
    "Avery":     "https://avery.webgis.net/",
    "Burke":     "https://burke.webgis.net/",
}

# Fallback: NC statewide GIS search
_NC_FALLBACK_URL = "https://www.nconemap.gov/"


def parse_address_parts(formatted_address: str) -> dict[str, str]:
    """
    Parse a Google-formatted address into components.

    Google Places returns addresses like:
        '123 Main St, Elkin, NC 28621, USA'
        '456 Broad St, Statesville, NC 28677, United States'

    Returns dict with keys: street, city, state, zip, full.
    """
    parts = {
        "street": "",
        "city": "",
        "state": "",
        "zip": "",
        "full": formatted_address,
    }
    if not formatted_address:
        return parts

    # Remove trailing country
    addr = re.sub(r",?\s*(USA|United States|US)\s*$", "", formatted_address, flags=re.IGNORECASE).strip()

    # Split on commas
    segments = [s.strip() for s in addr.split(",")]

    if len(segments) >= 3:
        # "123 Main St", "Elkin", "NC 28621"
        parts["street"] = segments[0]
        parts["city"] = segments[-2]
        state_zip = segments[-1]
    elif len(segments) == 2:
        parts["street"] = segments[0]
        state_zip = segments[1]
    else:
        state_zip = segments[0]

    # Parse "NC 28621" from the last segment
    m = re.match(r"([A-Z]{2})\s+(\d{5}(?:-\d{4})?)", state_zip)
    if m:
        parts["state"] = m.group(1)
        parts["zip"] = m.group(2)
    else:
        parts["state"] = state_zip.strip()

    # Rebuild a clean full address: street, city, state zip
    full_parts = []
    if parts["street"]:
        full_parts.append(parts["street"])
    if parts["city"]:
        full_parts.append(parts["city"])
    sz = parts["state"]
    if parts["zip"]:
        sz += " " + parts["zip"]
    if sz.strip():
        full_parts.append(sz.strip())
    parts["full"] = ", ".join(full_parts) if full_parts else formatted_address

    return parts


def determine_county(city: str) -> str:
    """Look up the NC county for a city name. Returns '' if unknown."""
    if not city:
        return ""
    return _CITY_TO_COUNTY.get(city.lower().strip(), "")


def get_tax_assessor_link(county: str, address: str) -> str:
    """
    Return a clickable tax-assessor URL for the given county.

    If the county has a known portal, returns that URL.
    Otherwise returns the NC statewide GIS fallback.
    """
    if county and county in _COUNTY_TAX_URLS:
        return _COUNTY_TAX_URLS[county]
    return _NC_FALLBACK_URL


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
    """
    Run the full lead collection pipeline.

    Iterates over all three search areas, runs every query in each area,
    de-duplicates by place_id across areas, enriches with county and
    tax-assessor data, and stops once TARGET_LEAD_COUNT qualified leads
    are collected.
    """
    seen_place_ids: set[str] = set()
    qualified_leads: list[Lead] = []
    duplicates_skipped = 0
    total_scanned = 0
    skipped_no_email = 0

    area_names = [a["name"] for a in SEARCH_AREAS]
    log.info("Starting lead collection for hospitality businesses")
    log.info("Search areas: %s", " | ".join(area_names))
    log.info("Target: %d qualified leads (must have email)", TARGET_LEAD_COUNT)
    log.info("Search radius: 25 miles per area")
    log.info("-" * 60)

    for area in SEARCH_AREAS:
        if len(qualified_leads) >= TARGET_LEAD_COUNT:
            break

        area_name = area["name"]
        area_lat = area["lat"]
        area_lng = area["lng"]
        # Short city name for building the search query
        area_city = area_name.split(",")[0]  # e.g. "Elkin"

        log.info("=== Searching area: %s ===", area_name)

        for query_text, query_type in SEARCH_QUERIES:
            if len(qualified_leads) >= TARGET_LEAD_COUNT:
                break

            full_query = f"{query_text} near {area_city} NC"
            log.info("  Query: '%s'", full_query)

            page_token = None
            pages_fetched = 0

            while True:
                if len(qualified_leads) >= TARGET_LEAD_COUNT:
                    break

                result = search_nearby(full_query, area_lat, area_lng, page_token)
                places = result.get("results", [])

                if not places:
                    log.info("    No results for this query/page.")
                    break

                pages_fetched += 1
                log.info(
                    "    Page %d: %d places (qualified so far: %d/%d)",
                    pages_fetched,
                    len(places),
                    len(qualified_leads),
                    TARGET_LEAD_COUNT,
                )

                for place in places:
                    if len(qualified_leads) >= TARGET_LEAD_COUNT:
                        break

                    place_id = place.get("place_id", "")
                    if not place_id:
                        continue
                    # Cross-area dedup: skip if already seen from another area
                    if place_id in seen_place_ids:
                        duplicates_skipped += 1
                        continue
                    seen_place_ids.add(place_id)
                    total_scanned += 1

                    name = place.get("name", "Unknown")

                    # Skip permanently closed businesses
                    if place.get("business_status") == "CLOSED_PERMANENTLY":
                        log.debug("    Skipping closed business: %s", name)
                        continue

                    # Fetch full details
                    details = get_place_details(place_id)
                    if not details:
                        continue

                    # Find email via multi-strategy approach
                    email, email_source = find_email(details)
                    website = details.get("website", "")

                    # REQUIRED: skip leads without email
                    if not email:
                        skipped_no_email += 1
                        if skipped_no_email % 20 == 0:
                            log.info(
                                "    [%d leads skipped so far -- no email found]",
                                skipped_no_email,
                            )
                        continue

                    # Parse address and determine county / tax link
                    raw_address = details.get("formatted_address", "")
                    addr = parse_address_parts(raw_address)
                    county = determine_county(addr["city"])
                    tax_link = get_tax_assessor_link(county, addr["full"])

                    # Build the lead
                    has_park, large_park = check_parking(details)
                    recent = has_recent_reviews(details)
                    types = details.get("types", []) or place.get("types", [])

                    lead = Lead(
                        business_name=details.get("name", name),
                        email=email,
                        email_source=email_source,
                        phone=details.get("formatted_phone_number", ""),
                        address=addr["full"],
                        business_type=classify_business_type(types, query_type),
                        website=website,
                        google_rating=details.get("rating", 0.0),
                        num_reviews=details.get("user_ratings_total", 0),
                        has_parking=has_park,
                        large_parking=large_park,
                        recent_reviews=recent,
                        place_id=place_id,
                        search_area=area_name,
                        county=county if county else "Unknown",
                        tax_assessor_link=tax_link,
                    )
                    lead.lead_score = score_lead(lead)
                    lead.notes = build_notes(lead)

                    qualified_leads.append(lead)
                    log.info(
                        "    + Lead #%d: %s | %s | score:%d | %s [%s]",
                        len(qualified_leads),
                        lead.business_name,
                        lead.county,
                        lead.lead_score,
                        lead.email,
                        lead.email_source,
                    )

                # Check for next page
                page_token = result.get("next_page_token")
                if not page_token:
                    break

    log.info("-" * 60)
    log.info("Collection complete.")
    log.info("  Total places scanned : %d", total_scanned)
    log.info("  Cross-area duplicates: %d", duplicates_skipped)
    log.info("  Skipped (no email)   : %d", skipped_no_email)
    log.info("  Qualified leads      : %d", len(qualified_leads))

    return qualified_leads


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

CSV_COLUMNS = [
    "Business Name",
    "Email",
    "Email Source",
    "Phone",
    "Full Property Address",
    "County",
    "Business Type",
    "Website",
    "Google Rating",
    "Number of Reviews",
    "Lead Score",
    "Search Area",
    "Tax Assessor Link",
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
                    lead.email_source,
                    lead.phone,
                    lead.address,
                    lead.county,
                    lead.business_type,
                    lead.website,
                    lead.google_rating,
                    lead.num_reviews,
                    lead.lead_score,
                    lead.search_area,
                    lead.tax_assessor_link,
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

        # Per-area breakdown
        print()
        print("  Leads by search area:")
        for area in SEARCH_AREAS:
            count = sum(1 for l in leads if l.search_area == area["name"])
            print(f"    {area['name']}: {count}")

        # County breakdown
        print()
        print("  Leads by county:")
        county_counts: dict[str, int] = {}
        for l in leads:
            county_counts[l.county] = county_counts.get(l.county, 0) + 1
        for county, count in sorted(county_counts.items(), key=lambda x: -x[1]):
            print(f"    {county}: {count}")

        # Email source breakdown
        from_google = sum(1 for l in leads if l.email_source == "Google Places")
        from_website = sum(1 for l in leads if l.email_source == "Website Scraped")
        print()
        print("  Email sources:")
        print(f"    Google Places  : {from_google}")
        print(f"    Website Scraped: {from_website}")
    print("=" * 60)


if __name__ == "__main__":
    main()
