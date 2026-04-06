"""HN Who's Hiring — monthly thread parser via Algolia API."""

import json
import re
import urllib.request
from datetime import date

from .base import NormalizedJob, SourceAdapter


class HNHiringAdapter(SourceAdapter):
    source_name = "hn_hiring"
    tier = 4

    def fetch(self, params: dict, since: date | None = None) -> list[NormalizedJob]:
        # Find the latest "Who is hiring?" thread
        search_url = "https://hn.algolia.com/api/v1/search?query=%22Who%20is%20hiring%22&tags=ask_hn&hitsPerPage=1"
        req = urllib.request.Request(
            search_url, headers={"User-Agent": "JobSearchPlatform/1.0"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            search_data = json.loads(resp.read().decode())

        hits = search_data.get("hits", [])
        if not hits:
            return []

        thread_id = hits[0]["objectID"]

        # Fetch all comments
        comments_url = f"https://hn.algolia.com/api/v1/items/{thread_id}"
        req = urllib.request.Request(
            comments_url, headers={"User-Agent": "JobSearchPlatform/1.0"}
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            thread_data = json.loads(resp.read().decode())

        results = []
        for child in thread_data.get("children", []):
            text = child.get("text", "")
            if not text or len(text) < 50:
                continue

            # Basic extraction: first line is usually "Company | Role | Location | ..."
            first_line = re.split(r"<[^>]+>", text)[0].strip()
            parts = [p.strip() for p in first_line.split("|")]

            if len(parts) >= 2:
                company = parts[0][:100]
                role = parts[1][:200] if len(parts) > 1 else "Unknown"
                location = parts[2][:200] if len(parts) > 2 else "Unknown"
            else:
                company = first_line[:100]
                role = "Unknown"
                location = "Unknown"

            date_str = (
                child.get("created_at", "")[:10] if child.get("created_at") else None
            )

            # Watermark filter
            if since and date_str:
                try:
                    if date.fromisoformat(date_str) <= since:
                        continue
                except ValueError:
                    pass

            results.append(
                NormalizedJob(
                    company=company,
                    role=role,
                    location=location,
                    ats_url="",  # HN comments don't always have URLs
                    date_posted=date_str,
                    source=self.source_name,
                    source_id=str(child.get("id", "")),
                    raw_json={"text": text[:2000], "author": child.get("author", "")},
                )
            )
        return results
