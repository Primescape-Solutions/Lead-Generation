# Hospitality Lead Generator

Finds hotel and restaurant owners/managers within 50 miles of Elkin, NC (28621) using the Google Places API. Collects 100 qualified leads that have an email address, scores them, and exports to CSV.

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

- Business Name
- Email
- **Email Source** — `Google Places` or `Website Scraped`
- Phone
- Address
- Business Type
- Website
- Google Rating
- Number of Reviews
- Lead Score
- Notes

Leads are sorted by score (highest first). Only leads with a confirmed email address are included.

## How It Works

1. Searches Google Places for hotels, motels, inns, B&Bs, resorts, restaurants, cafes, and similar businesses near Elkin, NC
2. For each result, fetches detailed place information
3. **Enhanced email finding** (multi-strategy, see below)
4. Skips any business without an email
5. Scores each lead based on the rubric above
6. Stops after collecting 100 qualified leads
7. Exports to CSV sorted by score

### Enhanced Email Finding

The script uses a two-stage approach to maximize email discovery:

**Stage 1 — Google Places data:** Checks if Google already provides an email for the business (rare but it happens).

**Stage 2 — Website scraping:** For any business that has a website but no email from Google:

- Crawls up to 16 pages per site: homepage, `/contact`, `/contact-us`, `/about`, `/about-us`, `/info`, `/reservations`, `/book`, `/location`, `/locations`, `/our-story`, `/connect`, `/feedback`, `/reach-us`, and any additional contact-like links discovered in the HTML
- Extracts emails from `mailto:` links first (highest signal), then from free-text regex
- Filters out false positives (image files, social media domains, tracking services, noreply addresses, etc.)
- Ranks candidates: emails matching the site's own domain and common business prefixes (`info@`, `contact@`, `reservations@`, `frontdesk@`, etc.) are preferred
- Tracks the source of every email so you can see which came from Google vs. scraping

Each CSV row includes an **Email Source** column showing where the email was found.

## API Usage Notes

- The script makes one Text Search call per query/page and one Place Details call per business
- Email extraction may visit up to ~16 pages per business website (homepage + contact/about pages + discovered links)
- Expect roughly 300-600 Google API calls for a full run depending on how many businesses lack emails
- Website scraping adds network time but does not consume Google API quota
- Google Places API pricing: see [Google Maps Platform pricing](https://developers.google.com/maps/billing-and-pricing/pricing)
