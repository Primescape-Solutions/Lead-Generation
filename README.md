# Hospitality Lead Generator

Finds hotel and restaurant owners/managers in three search zones across the NC foothills using the Google Places API. Searches within 25 miles of Elkin, Wilkesboro, and Statesville, NC, de-duplicates across zones, collects 100 qualified leads with email addresses, scores them, determines the county and tax-assessor lookup link, and exports to CSV.

## Search Areas

| Area | Center | Radius |
|---|---|---|
| Elkin, NC (28621) | 36.2440, -80.8487 | 25 miles |
| Wilkesboro, NC (28697) | 36.1460, -81.1604 | 25 miles |
| Statesville, NC (28677) | 35.7826, -80.8873 | 25 miles |

Businesses that appear in multiple overlapping zones are counted only once.

## Lead Scoring System

| Criteria | Points |
|---|---|
| Has email address | +10 (required) |
| Has phone number | +5 |
| Has website | +5 |
| Parking lot visible | +15 |
| Large parking lot | +10 |
| Google rating 4+ stars | +5 |
| 50+ reviews | +5 |
| Recent reviews (last 3 months) | +5 |

**Maximum possible score: 60 points**

## Setup

### 1. Get a Google Places API Key

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or select an existing one)
3. Navigate to **APIs & Services > Library**
4. Search for **Places API** and enable it
5. Go to **APIs & Services > Credentials**
6. Click **Create Credentials > API Key**
7. Copy the API key

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure API Key

Copy the example env file and add your key:

```bash
cp .env.example .env
```

Edit `.env` and replace `your_api_key_here` with your actual API key:

```
GOOGLE_PLACES_API_KEY=AIzaSy...your_key_here
```

### 4. Run

```bash
python lead_generator.py
```

## Output

The script produces `hospitality_leads.csv` with these columns:

| Column | Description |
|---|---|
| Business Name | Name of the hotel/restaurant |
| Email | Contact email address |
| Email Source | `Google Places` or `Website Scraped` |
| Phone | Phone number |
| Full Property Address | Street, city, state, zip |
| County | NC county (Wilkes, Surry, Iredell, etc.) |
| Business Type | Hotel / Lodging or Restaurant / Dining |
| Website | Business website URL |
| Google Rating | Star rating (0-5) |
| Number of Reviews | Total Google review count |
| Lead Score | Calculated score (0-60) |
| Search Area | Which of the 3 zones found this lead |
| Tax Assessor Link | Clickable URL to the county's property search portal |
| Notes | Auto-generated observations (parking, ratings, etc.) |

Leads are sorted by score (highest first). Only leads with a confirmed email address are included.

### Property Owner Lookup

Each lead includes:

- **County** — auto-determined from the city in the business address using a built-in mapping of 100+ cities/towns across 17 NC counties in the region
- **Tax Assessor Link** — a direct URL to the county's online GIS/property-search portal where you can look up the property owner by address

Most counties in the region use `<county>.webgis.net` portals. Open the link, search by the business address, and you'll find the property owner name, parcel ID, and assessed value.

## How It Works

1. Iterates over three search areas (Elkin, Wilkesboro, Statesville)
2. For each area, queries Google Places for 10 hospitality business types
3. De-duplicates results across overlapping search zones by place ID
4. Fetches detailed place information for each unique result
5. **Enhanced email finding** (multi-strategy, see below)
6. Skips any business without an email
7. Parses the address, determines the county, and generates a tax-assessor link
8. Scores each lead based on the rubric above
9. Stops after collecting 100 qualified leads
10. Exports to CSV sorted by score

### Enhanced Email Finding

The script uses a two-stage approach to maximize email discovery:

**Stage 1 — Google Places data:** Checks if Google already provides an email for the business (rare but it happens).

**Stage 2 — Website scraping:** For any business that has a website but no email from Google:

- Crawls up to 16 pages per site: homepage, `/contact`, `/contact-us`, `/about`, `/about-us`, `/info`, `/reservations`, `/book`, `/location`, `/locations`, `/our-story`, `/connect`, `/feedback`, `/reach-us`, and any additional contact-like links discovered in the HTML
- Extracts emails from `mailto:` links first (highest signal), then from free-text regex
- Filters out false positives (image files, social media domains, tracking services, noreply addresses, etc.)
- Ranks candidates: emails matching the site's own domain and common business prefixes (`info@`, `contact@`, `reservations@`, `frontdesk@`, etc.) are preferred
- Tracks the source of every email so you can see which came from Google vs. scraping

### County Coverage

The built-in city-to-county mapping covers these NC counties:

Alleghany, Alexander, Ashe, Avery, Burke, Caldwell, Catawba, Davie, Forsyth, Iredell, Lincoln, Rowan, Stokes, Surry, Watauga, Wilkes, Yadkin

If a business is in a city not in the mapping, the county shows as "Unknown" and the tax link falls back to the NC statewide GIS portal.

## API Usage Notes

- The script makes one Text Search call per query/page and one Place Details call per business
- With 3 search areas x 10 queries, there are up to 30 initial search batches (plus pagination)
- Email extraction may visit up to ~16 pages per business website
- Expect roughly 500-900 Google API calls for a full run depending on overlap and email hit rates
- Website scraping adds network time but does not consume Google API quota
- Google Places API pricing: see [Google Maps Platform pricing](https://developers.google.com/maps/billing-and-pricing/pricing)
