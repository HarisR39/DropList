# InternRadar
### Real-Time AI-Powered Internship Aggregator

> Find internships the moment they drop — before everyone else.

---

## Overview

**InternRadar** is a full-stack web application that continuously monitors company career pages and job boards to surface internship postings in real time. It uses an AI-powered scraping pipeline to extract structured data, identify resume keywords, and present opportunities through a clean, filterable dashboard — with direct application links.

Built for students who are tired of refreshing LinkedIn and missing roles the day they close.

---

## Key Features

| Feature | Description |
|---|---|
| Real-Time Posting Feed | Internships appear as soon as they're detected on company career pages |
| AI Keyword Extractor | Automatically surfaces skills, tools, and qualifications companies are looking for |
| Direct Apply Links | One-click links to the original application page — no middleman |
| Multi-Source Aggregation | Scrapes company career portals, Greenhouse, Lever, Workday, and more |
| Alert System | Email/push notifications when a role matching your profile is posted |
| Smart Filtering | Filter by role, location, stack, deadline, season, and company size |
| Keyword Analytics | See what skills appear most across all postings in a given field |

---

## Tech Stack

### Frontend
- **Framework**: React (Vite) or Next.js
- **Styling**: Tailwind CSS
- **State**: Zustand or React Query
- **Charts**: Recharts (for keyword frequency analytics)

### Backend
- **Runtime**: Node.js (Express) or Python (FastAPI)
- **Scraping Engine**: Playwright or Puppeteer (headless browser for JS-heavy career pages)
- **AI Layer**: Anthropic Claude API — keyword extraction, role classification, and summary generation
- **Job Queue**: BullMQ + Redis (rate-limited scraping scheduler)
- **Database**: PostgreSQL (postings, companies, keywords) + Redis (caching + dedup)

### Infrastructure
- **Hosting**: Fly.io / Railway / Render
- **Scraper Workers**: Docker containers, horizontally scalable
- **Scheduler**: Cron jobs or event-driven triggers per source
- **Notifications**: Resend (email) + Web Push API

---

## Project Structure

```
internradar/
├── apps/
│   ├── web/                  # React/Next.js frontend
│   │   ├── components/
│   │   │   ├── PostingCard.tsx
│   │   │   ├── KeywordBadge.tsx
│   │   │   ├── FilterSidebar.tsx
│   │   │   └── AlertModal.tsx
│   │   └── pages/
│   │       ├── index.tsx     # Main feed
│   │       ├── posting/[id]  # Individual posting view
│   │       └── analytics.tsx # Keyword trends
│   └── api/                  # Backend server
│       ├── routes/
│       │   ├── postings.ts
│       │   ├── alerts.ts
│       │   └── keywords.ts
│       └── services/
│           ├── scraper.ts
│           ├── aiExtractor.ts
│           └── deduplicator.ts
├── scrapers/
│   ├── sources/
│   │   ├── greenhouse.ts
│   │   ├── lever.ts
│   │   ├── workday.ts
│   │   └── custom/          # Per-company scrapers
│   ├── scheduler.ts
│   └── queue.ts
├── workers/
│   └── scrapeWorker.ts
├── db/
│   ├── schema.sql
│   └── migrations/
├── docker-compose.yml
└── README.md
```

---

## AI Pipeline

Each scraped posting goes through a multi-step AI processing pipeline:

```
Raw HTML / Job Description
        |
  Text Extraction (Playwright)
        |
  Claude API -- aiExtractor
        | outputs:
  +-----------------------------------------+
  |  - Role title (normalized)              |
  |  - Required skills (ranked)             |
  |  - Preferred skills                     |
  |  - Resume keywords to match             |
  |  - Season / term (Summer 2026)          |
  |  - Remote / hybrid / onsite             |
  |  - Application deadline (if any)        |
  |  - 2-sentence TL;DR summary             |
  +-----------------------------------------+
        |
  Stored in PostgreSQL + served to frontend
```

**Prompt strategy**: Claude is given the raw job description and asked to return structured JSON with keyword categories (hard skills, soft skills, domain knowledge, tools/frameworks). This output powers both the keyword badge UI and the analytics dashboard.

---

## Database Schema (Simplified)

```sql
-- Companies being tracked
CREATE TABLE companies (
  id UUID PRIMARY KEY,
  name TEXT NOT NULL,
  career_url TEXT NOT NULL,
  ats_platform TEXT,           -- greenhouse | lever | workday | custom
  scrape_interval_minutes INT DEFAULT 60,
  last_scraped_at TIMESTAMPTZ
);

-- Internship postings
CREATE TABLE postings (
  id UUID PRIMARY KEY,
  company_id UUID REFERENCES companies(id),
  title TEXT NOT NULL,
  location TEXT,
  remote BOOLEAN,
  apply_url TEXT NOT NULL,
  description_raw TEXT,
  summary TEXT,
  season TEXT,                 -- "Summer 2026"
  deadline DATE,
  first_seen_at TIMESTAMPTZ DEFAULT NOW(),
  is_active BOOLEAN DEFAULT TRUE
);

-- Extracted keywords per posting
CREATE TABLE keywords (
  id UUID PRIMARY KEY,
  posting_id UUID REFERENCES postings(id),
  keyword TEXT NOT NULL,
  category TEXT,               -- hard_skill | soft_skill | tool | domain
  importance TEXT              -- required | preferred
);

-- User alert subscriptions
CREATE TABLE alerts (
  id UUID PRIMARY KEY,
  email TEXT NOT NULL,
  keywords TEXT[],
  roles TEXT[],
  locations TEXT[],
  created_at TIMESTAMPTZ DEFAULT NOW()
);
```

---

## Scraping Architecture

- **Polling**: Each source is scraped on a configurable interval (default: 60 min, configurable down to 15 min for high-signal companies)
- **Deduplication**: Posting fingerprinted by `(company_id + title + apply_url hash)` — no duplicates in the feed
- **Rate limiting**: BullMQ enforces per-domain concurrency limits to avoid getting blocked
- **Headless rendering**: Playwright handles SPAs and JS-rendered career pages (Workday, Greenhouse iframes, etc.)
- **Robots.txt compliance**: Scraper checks and respects crawl rules per domain

---

## Alert System

Users can subscribe with:
- Target keywords (e.g., `Python`, `React`, `ML`)
- Role types (e.g., `SWE`, `Data Science`, `PM`)
- Location preference
- Specific companies to watch

When a new posting is inserted that matches a user's alert profile, an email is dispatched via Resend within minutes of detection.

---

## Roadmap

- [x] Core scraper scaffold
- [x] AI keyword extraction pipeline
- [x] Posting feed UI with filters
- [ ] User accounts + alert subscriptions
- [ ] Keyword analytics dashboard
- [ ] Browser extension for one-click "track this company"
- [ ] Resume match score (upload resume, see how well you match each posting)
- [ ] ATS coverage expanded (iCIMS, SmartRecruiters, Ashby)
- [ ] Public API for third-party integrations

---

## Getting Started (Dev)

```bash
# Clone the repo
git clone https://github.com/yourusername/internradar.git
cd internradar

# Install dependencies
npm install

# Set environment variables
cp .env.example .env
# Add: ANTHROPIC_API_KEY, DATABASE_URL, REDIS_URL, RESEND_API_KEY

# Start local services
docker-compose up -d   # PostgreSQL + Redis

# Run migrations
npm run db:migrate

# Start the dev server
npm run dev
```

---

## Environment Variables

```env
ANTHROPIC_API_KEY=       # Claude API key (keyword extraction)
DATABASE_URL=            # PostgreSQL connection string
REDIS_URL=               # Redis connection string
RESEND_API_KEY=          # Email notification service
NEXT_PUBLIC_API_URL=     # Frontend -> API base URL
SCRAPE_INTERVAL_MINUTES= # Default: 60
```

---

## Legal & Ethical Notes

- This project respects `robots.txt` directives on all scraped domains
- Postings are not reproduced in full — only metadata and extracted keywords are stored
- Direct apply links point back to the original company page
- No login walls are bypassed; only publicly accessible career pages are scraped

---

## Contributing

PRs welcome. If you want to add a new ATS platform scraper or a company to the default watchlist, open an issue first to discuss the approach.

---

## License

MIT
