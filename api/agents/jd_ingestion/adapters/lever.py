"""Lever Postings API — per-company, public REST."""

import json
import urllib.request
from datetime import date

from .base import NormalizedJob, SourceAdapter


class LeverAdapter(SourceAdapter):
    source_name = "lever"
    tier = 2

    def fetch(self, params: dict, since: date | None = None) -> list[NormalizedJob]:
        # Lever doesn't expose post dates, so watermark filtering is not possible
        company_slug = params.get("company", "")
        if not company_slug:
            return []

        url = f"https://api.lever.co/v0/postings/{company_slug}?mode=json"
        req = urllib.request.Request(
            url, headers={"User-Agent": "JobSearchPlatform/1.0"}
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError:
            return []

        results = []
        for item in data if isinstance(data, list) else []:
            categories = item.get("categories", {})
            results.append(
                NormalizedJob(
                    company=company_slug.replace("-", " ").title(),
                    role=item.get("text", "Unknown"),
                    location=categories.get("location", "Unknown"),
                    ats_url=item.get("hostedUrl", ""),
                    date_posted=None,  # Lever doesn't expose post date in public API
                    source=self.source_name,
                    source_id=str(item.get("id", "")),
                    raw_json=item,
                )
            )
        return results
