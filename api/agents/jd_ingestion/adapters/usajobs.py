"""USAJobs API — free, open, government/contractor tech roles."""

import json
import os
import urllib.parse
import urllib.request
from datetime import date

from .base import NormalizedJob, SourceAdapter


class USAJobsAdapter(SourceAdapter):
    source_name = "usajobs"
    tier = 1

    def fetch(self, params: dict, since: date | None = None) -> list[NormalizedJob]:
        api_key = os.environ.get("USAJOBS_API_KEY", "")
        email = os.environ.get("USAJOBS_EMAIL", "")
        if not api_key or not email:
            return []

        keyword = params.get("keyword", "software engineer")
        qs_params: dict = {
            "Keyword": keyword,
            "ResultsPerPage": 50,
            "Fields": "min",
        }
        # USAJobs supports server-side date filtering
        if since:
            qs_params["DatePosted"] = (date.today() - since).days

        qs = urllib.parse.urlencode(qs_params)
        url = f"https://data.usajobs.gov/api/search?{qs}"

        req = urllib.request.Request(
            url,
            headers={
                "Authorization-Key": api_key,
                "User-Agent": email,
                "Host": "data.usajobs.gov",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        results = []
        for item in data.get("SearchResult", {}).get("SearchResultItems", []):
            match = item.get("MatchedObjectDescriptor", {})
            results.append(
                NormalizedJob(
                    company=match.get("OrganizationName", "US Government"),
                    role=match.get("PositionTitle", "Unknown"),
                    location=", ".join(
                        loc.get("CityName", "")
                        + ", "
                        + loc.get("CountrySubDivisionCode", "")
                        for loc in match.get("PositionLocation", [])
                    )
                    or "Unknown",
                    ats_url=match.get("PositionURI", ""),
                    date_posted=match.get("PublicationStartDate", "")[:10]
                    if match.get("PublicationStartDate")
                    else None,
                    source=self.source_name,
                    source_id=match.get("PositionID", ""),
                    raw_json=item,
                )
            )
        return results
