# Phoenix PM Dashboard

An interactive intelligence dashboard for AI Product Managers to monitor GenAI product usage via Phoenix Arize trace logs. Connect to any Phoenix instance, configure your product's cohorts/locations/meeting schedules, and get actionable insights.

## What It Does

- **Executive Summary** — KPIs for leadership: request volume, success rates, latency, token usage, model distribution
- **Usage Analytics** — Organic vs planned usage, per-location breakdowns, cohort adoption tracking, workflow completion funnels
- **Usage Report** — Per-cohort adoption rates, active/inactive user lists, resource category demand, geographic coverage
- **Performance Metrics** — Latency percentiles over time, tail latency analysis, per-trace bottleneck breakdown, slowest users
- **Log Explorer** — Downloadable trace list with latency/token correlation, span-level step breakdown
- **Advanced Analytics** — Query pattern intelligence, resource effectiveness scoring, user journey analysis, drop-off detection

## Quick Start

```bash
# 1. Clone
git clone <repo-url> && cd phoenix-pm-dashboard

# 2. Install
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 3. Configure
cp .env.example .env        # Add your Phoenix URL + API key
cp config.example.yaml config.yaml  # Customize for your product

# 4. Run
streamlit run dashboard.py
```

The dashboard opens at `http://localhost:8501`.

## Configuration

All product-specific settings live in `config.yaml`. See `config.example.yaml` for the full schema with comments.

### Minimal config

```yaml
product:
  name: "My AI Product"

excluded_users:
  emails:
    - "admin@mycompany.com"
  domain_patterns:
    - "@internal\\.[a-z0-9.-]+$"
```

### Full config sections

| Section | Purpose |
|---------|---------|
| `product` | Product name, optional secondary Phoenix source |
| `excluded_users` | Emails and domain patterns to filter out of all analytics |
| `priority_email_domains` | Preferred domains when extracting user identity from traces |
| `trace_types` | Regex patterns to classify root span names into trace types |
| `cohorts` | Named user groups to track adoption (e.g., pilot waves) |
| `locations` | Domain-based user grouping for location breakdowns |
| `meeting_windows` | Scheduled sessions to exclude from organic usage counts |
| `geographic` | Service area zip codes and city name mappings |
| `categories` | Keyword-based query classification |

### Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `PHOENIX_API_URL` | Yes | Your Phoenix instance URL |
| `PHOENIX_API_KEY` | No | API key (if auth required) |
| `PHOENIX_PROJECT_ID` | No | Default project ID |
| `DASHBOARD_CONFIG` | No | Path to config file (default: `config.yaml`) |

## How It Works

1. **Connects** to your Phoenix Arize instance via GraphQL
2. **Fetches** spans with pagination and client-side time filtering
3. **Groups** spans into traces and extracts user identity, query, category, location
4. **Filters** out internal/test users based on your config
5. **Classifies** traces by type using your configured patterns
6. **Renders** interactive Plotly charts in a multi-tab Streamlit interface

## Adapting for Your Product

The dashboard was designed to be product-agnostic. Here's how to customize it:

**If your product has user cohorts** (pilot waves, beta groups):
- Add them to `cohorts` in config.yaml with email lists
- The Usage Report tab will show per-cohort adoption rates

**If you have scheduled demo/training sessions**:
- Add meeting windows to `meeting_windows` in config.yaml
- The Usage Analytics tab will separate organic from planned usage

**If your product serves multiple sites/locations**:
- Define locations with domain patterns in config.yaml
- The dashboard will break down usage by location

**If your product has distinct workflow steps** (search → generate → email):
- Define trace types in config.yaml matching your root span names
- The dashboard will track workflow completion funnels

**If you need query categorization**:
- Define categories with keywords in config.yaml
- Queries will be auto-classified for demand analysis

## Project Structure

```
phoenix-pm-dashboard/
├── dashboard.py          # Streamlit UI (main entry point)
├── data_analyzer.py      # Trace analysis engine
├── phoenix_client.py     # Phoenix GraphQL API client
├── config.py             # Config loader and helpers
├── config.example.yaml   # Example configuration (copy to config.yaml)
├── requirements.txt      # Python dependencies
├── .env.example          # Environment variable template
└── README.md             # This file
```

## Requirements

- Python 3.9+
- Access to a Phoenix Arize instance
- Optional: API key for authenticated Phoenix instances
