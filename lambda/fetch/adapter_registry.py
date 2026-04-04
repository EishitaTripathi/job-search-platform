"""Adapter registry — maps source names to adapter classes.

TOS Compliance Policy:
  Only adapters that use official, documented, free public APIs are enabled.
  Adapters requiring API keys are kept but only activate when keys are provided.
  Blacklisted adapters are removed from the registry and must not be re-added
  without verifying their TOS explicitly permits automated programmatic access.

Blacklist (DO NOT re-enable without TOS review):
  - remoteok: Actively blocks automated access (HTTP 403). No documented API TOS.
  - jsearch: Third-party RapidAPI wrapper around Google Jobs. Google does not
    officially license this aggregation. Extremely limited free tier (100/mo).
"""

from adapters.simplify import SimplifyAdapter
from adapters.the_muse import TheMuseAdapter
from adapters.greenhouse import GreenhouseAdapter
from adapters.lever import LeverAdapter
from adapters.ashby import AshbyAdapter
from adapters.hn_hiring import HNHiringAdapter
from adapters.base import SourceAdapter

# Only TOS-compliant adapters are registered.
# GREEN: Official public APIs, no auth required.
# YELLOW: Official APIs, require free API keys (gracefully return [] if keys missing).
ADAPTERS: dict[str, type[SourceAdapter]] = {
    # All adapters below are free, public, and TOS-compliant (no API key required)
    "the_muse": TheMuseAdapter,
    "greenhouse": GreenhouseAdapter,
    "lever": LeverAdapter,
    "ashby": AshbyAdapter,
    "simplify": SimplifyAdapter,  # Published JSON from SimplifyJobs GitHub repo
    "hn_hiring": HNHiringAdapter,  # Algolia public HN Search API
    # Re-enable with API keys if needed:
    # "adzuna": AdzunaAdapter,         # Free tier: 250 calls/day, needs ADZUNA_APP_ID + ADZUNA_APP_KEY
    # "usajobs": USAJobsAdapter,       # US Government API, needs USAJOBS_API_KEY + USAJOBS_EMAIL
}


def get_adapter(source: str) -> SourceAdapter:
    """Get adapter instance by source name."""
    adapter_cls = ADAPTERS.get(source)
    if adapter_cls is None:
        raise ValueError(
            f"Unknown source: {source}. Available: {list(ADAPTERS.keys())}"
        )
    return adapter_cls()
