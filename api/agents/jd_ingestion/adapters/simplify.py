"""Simplify GitHub — curated new-grad/intern tech listings.

Filtering mirrors Simplify's own README generation logic
(see .github/scripts/update_readmes.py + util.py in the SimplifyJobs repo):
  1. active=true, is_visible=true
  2. date_posted > cycle start (June 2025 for 2026 new grad cycle)
  3. Title must match role terms AND new-grad terms (for Simplify-sourced)
  4. Community-sourced listings pass title filter automatically
  5. Sponsorship eligible only
"""

import json
import urllib.request
from datetime import date, datetime, timezone

from .base import NormalizedJob, SourceAdapter

SIMPLIFY_URL = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json"

# Sponsorship values that indicate visa sponsorship is possible
ELIGIBLE_SPONSORSHIP = {"Offers Sponsorship", "Other"}

# Earliest date for the current new-grad cycle (matches Simplify's own filter)
# 1748761200 = 2025-06-01 UTC — start of 2026 new grad hiring season
CYCLE_START_TIMESTAMP = 1748761200

# Title matching terms from Simplify's util.py filterListings()
ROLE_TERMS = [
    "software eng",
    "software dev",
    "product engineer",
    "fullstack engineer",
    "frontend",
    "front end",
    "front-end",
    "backend",
    "back end",
    "full-stack",
    "full stack",
    "founding engineer",
    "mobile dev",
    "mobile engineer",
    "data scientist",
    "data engineer",
    "research eng",
    "product manag",
    "apm",
    "product",
    "devops",
    "android",
    "ios",
    "sre",
    "site reliability eng",
    "quantitative trad",
    "quantitative research",
    "quantitative dev",
    "security eng",
    "compiler eng",
    "machine learning eng",
    "hardware eng",
    "firmware eng",
    "infrastructure eng",
    "embedded",
    "fpga",
    "circuit",
    "chip",
    "silicon",
    "asic",
    "quant",
    "quantitative",
    "trading",
    "finance",
    "investment",
    "ai &",
    "machine learning",
    "ml",
    "analytics",
    "analyst",
    "research sci",
]

NEW_GRAD_TERMS = [
    "new grad",
    "early career",
    "college grad",
    "entry level",
    "founding",
    "early in career",
    "university grad",
    "fresh grad",
    "2024 grad",
    "2025 grad",
    "engineer 0",
    "engineer 1",
    "engineer i ",
    "junior",
    "sde 1",
    "sde i",
]


class SimplifyAdapter(SourceAdapter):
    source_name = "simplify"
    tier = 1

    def fetch(self, params: dict, since: date | None = None) -> list[NormalizedJob]:
        req = urllib.request.Request(
            SIMPLIFY_URL, headers={"User-Agent": "JobSearchPlatform/1.0"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            listings = json.loads(resp.read().decode())

        results = []
        for item in listings:
            if not item.get("company_name") or not item.get("title"):
                continue
            # Only open positions
            if not item.get("active"):
                continue
            # Only visible listings (matches Simplify's README filter)
            if not item.get("is_visible"):
                continue
            # Skip roles that don't offer sponsorship
            if item.get("sponsorship") not in ELIGIBLE_SPONSORSHIP:
                continue

            # Date filter: must be within current new-grad cycle
            raw_date = item.get("date_posted")
            if (
                not isinstance(raw_date, (int, float))
                or raw_date <= CYCLE_START_TIMESTAMP
            ):
                continue

            post_date = datetime.fromtimestamp(raw_date, tz=timezone.utc).date()
            date_str = post_date.strftime("%Y-%m-%d")

            # Title relevance filter (matches Simplify's README logic):
            # Community-sourced listings pass automatically;
            # Simplify-sourced must match role terms AND new-grad terms
            item_source = item.get("source", "")
            if item_source == "Simplify":
                title_lower = item["title"].lower()
                has_role = any(t in title_lower for t in ROLE_TERMS)
                has_level = any(
                    t in title_lower for t in NEW_GRAD_TERMS
                ) or title_lower.endswith("engineer i")
                if not (has_role and has_level):
                    continue

            # Watermark filter: skip jobs older than last fetch
            if since and post_date <= since:
                continue

            results.append(
                NormalizedJob(
                    company=item["company_name"],
                    role=item["title"],
                    location=", ".join(item.get("locations", [])) or "Unknown",
                    ats_url=item.get("url", ""),
                    date_posted=date_str,
                    source=self.source_name,
                    source_id=str(item.get("id", "")),
                    raw_json=item,
                )
            )
        return results
