"""Greenhouse Board API — per-company, public REST."""

import json
import urllib.request
from datetime import date

from .base import NormalizedJob, SourceAdapter


class GreenhouseAdapter(SourceAdapter):
    source_name = "greenhouse"
    tier = 2

    def fetch(self, params: dict, since: date | None = None) -> list[NormalizedJob]:
        company_slug = params.get("company", "")
        if not company_slug:
            return []

        url = f"https://boards-api.greenhouse.io/v1/boards/{company_slug}/jobs"
        req = urllib.request.Request(
            url, headers={"User-Agent": "JobSearchPlatform/1.0"}
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError:
            return []

        results = []
        for item in data.get("jobs", []):
            loc = (
                item.get("location", {}).get("name", "Unknown")
                if isinstance(item.get("location"), dict)
                else "Unknown"
            )
            date_str = (
                item.get("updated_at", "")[:10] if item.get("updated_at") else None
            )

            if since and date_str:
                try:
                    if date.fromisoformat(date_str) <= since:
                        continue
                except ValueError:
                    pass

            results.append(
                NormalizedJob(
                    company=company_slug.replace("-", " ").title(),
                    role=item.get("title", "Unknown"),
                    location=loc,
                    ats_url=item.get("absolute_url", ""),
                    date_posted=date_str,
                    source=self.source_name,
                    source_id=str(item.get("id", "")),
                    raw_json=item,
                )
            )
        return results
