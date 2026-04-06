"""Adzuna API — 250 calls/day free tier, 10K+ US tech jobs."""

import json
import os
import urllib.parse
import urllib.request
from datetime import date

from .base import NormalizedJob, SourceAdapter


class AdzunaAdapter(SourceAdapter):
    source_name = "adzuna"
    tier = 1

    def fetch(self, params: dict, since: date | None = None) -> list[NormalizedJob]:
        app_id = os.environ.get("ADZUNA_APP_ID", "")
        app_key = os.environ.get("ADZUNA_APP_KEY", "")
        if not app_id or not app_key:
            return []

        page = params.get("page", 1)
        query = params.get("query", "software engineer")
        qs_params: dict = {
            "app_id": app_id,
            "app_key": app_key,
            "results_per_page": 50,
            "what": query,
            "where": "United States",
            "content-type": "application/json",
            "category": "it-jobs",
        }
        # Adzuna supports server-side date filtering
        if since:
            qs_params["max_days_old"] = (date.today() - since).days

        qs = urllib.parse.urlencode(qs_params)
        url = f"https://api.adzuna.com/v1/api/jobs/us/search/{page}?{qs}"

        req = urllib.request.Request(
            url, headers={"User-Agent": "JobSearchPlatform/1.0"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        results = []
        for item in data.get("results", []):
            results.append(
                NormalizedJob(
                    company=item.get("company", {}).get("display_name", "Unknown"),
                    role=item.get("title", "Unknown"),
                    location=item.get("location", {}).get("display_name", "Unknown"),
                    ats_url=item.get("redirect_url", ""),
                    date_posted=item.get("created", "")[:10]
                    if item.get("created")
                    else None,
                    source=self.source_name,
                    source_id=str(item.get("id", "")),
                    raw_json=item,
                )
            )
        return results
