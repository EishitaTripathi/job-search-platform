"""The Muse API — entry-level tech focus, free."""

import json
import urllib.request
import urllib.parse
from .base import SourceAdapter, NormalizedJob


class TheMuseAdapter(SourceAdapter):
    source_name = "the_muse"
    tier = 1

    def fetch(self, params: dict) -> list[NormalizedJob]:
        page = params.get("page", 0)
        qs = urllib.parse.urlencode(
            {
                "category": "Software Engineering",
                "level": "Entry Level",
                "location": "United States",
                "page": page,
            }
        )
        url = f"https://www.themuse.com/api/public/jobs?{qs}"

        req = urllib.request.Request(
            url, headers={"User-Agent": "JobSearchPlatform/1.0"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        results = []
        for item in data.get("results", []):
            company = item.get("company", {}).get("name", "Unknown")
            results.append(
                NormalizedJob(
                    company=company,
                    role=item.get("name", "Unknown"),
                    location=", ".join(
                        loc.get("name", "") for loc in item.get("locations", [])
                    )
                    or "Unknown",
                    ats_url=item.get("refs", {}).get("landing_page", ""),
                    date_posted=item.get("publication_date", "")[:10]
                    if item.get("publication_date")
                    else None,
                    source=self.source_name,
                    source_id=str(item.get("id", "")),
                    raw_json=item,
                )
            )
        return results
