"""Simplify GitHub — curated new-grad/intern tech listings."""

import json
import urllib.request
from datetime import datetime, timezone
from .base import SourceAdapter, NormalizedJob

SIMPLIFY_URL = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json"

# Sponsorship values that indicate visa sponsorship is possible
ELIGIBLE_SPONSORSHIP = {"Offers Sponsorship", "Other"}


class SimplifyAdapter(SourceAdapter):
    source_name = "simplify"
    tier = 1

    def fetch(self, params: dict) -> list[NormalizedJob]:
        req = urllib.request.Request(
            SIMPLIFY_URL, headers={"User-Agent": "JobSearchPlatform/1.0"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            listings = json.loads(resp.read().decode())

        results = []
        for item in listings:
            if not item.get("company_name") or not item.get("title"):
                continue
            # Skip inactive listings
            if not item.get("active"):
                continue
            # Skip roles that don't offer sponsorship
            if item.get("sponsorship") not in ELIGIBLE_SPONSORSHIP:
                continue

            # Convert Unix timestamp to YYYY-MM-DD
            raw_date = item.get("date_posted")
            if isinstance(raw_date, (int, float)) and raw_date > 1_000_000_000:
                date_str = datetime.fromtimestamp(raw_date, tz=timezone.utc).strftime(
                    "%Y-%m-%d"
                )
            else:
                date_str = str(raw_date) if raw_date else None

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
