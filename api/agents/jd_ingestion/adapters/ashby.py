"""Ashby Job Board API — public REST."""

import json
import urllib.request
from datetime import date

from .base import NormalizedJob, SourceAdapter


class AshbyAdapter(SourceAdapter):
    source_name = "ashby"
    tier = 2

    def fetch(self, params: dict, since: date | None = None) -> list[NormalizedJob]:
        company_slug = params.get("company", "")
        if not company_slug:
            return []

        url = f"https://api.ashbyhq.com/posting-api/job-board/{company_slug}"
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
            date_str = (
                item.get("publishedAt", "")[:10] if item.get("publishedAt") else None
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
                    location=item.get("location", "Unknown"),
                    ats_url=item.get("jobUrl", ""),
                    date_posted=date_str,
                    source=self.source_name,
                    source_id=str(item.get("id", "")),
                    raw_json=item,
                )
            )
        return results
