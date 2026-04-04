"""Tech company allowlist for recommendation validation.

Seed: ~100 known tech companies loaded at startup.
Auto-expand: companies seen in our source adapters are trusted.
Unknown: queued for user review in local labeling dashboard.
"""

import logging

from local.agents.shared.db import acquire

logger = logging.getLogger(__name__)

# Static seed of well-known tech companies (loaded once, expanded from DB)
_SEED_COMPANIES = {
    "google",
    "meta",
    "apple",
    "amazon",
    "microsoft",
    "netflix",
    "nvidia",
    "anthropic",
    "openai",
    "stripe",
    "airbnb",
    "uber",
    "lyft",
    "doordash",
    "coinbase",
    "databricks",
    "snowflake",
    "palantir",
    "datadog",
    "mongodb",
    "elastic",
    "cloudflare",
    "twilio",
    "okta",
    "crowdstrike",
    "palo alto networks",
    "splunk",
    "servicenow",
    "workday",
    "salesforce",
    "adobe",
    "intuit",
    "vmware",
    "broadcom",
    "qualcomm",
    "intel",
    "amd",
    "arm",
    "tesla",
    "spacex",
    "rivian",
    "waymo",
    "cruise",
    "aurora",
    "figma",
    "notion",
    "vercel",
    "supabase",
    "hashicorp",
    "confluent",
    "cockroach labs",
    "timescale",
    "planetscale",
    "neon",
    "anduril",
    "shield ai",
    "scale ai",
    "hugging face",
    "cohere",
    "mistral",
    "inflection",
    "character ai",
    "stability ai",
    "runway",
    "plaid",
    "marqeta",
    "brex",
    "ramp",
    "mercury",
    "rippling",
    "gusto",
    "lattice",
    "carta",
    "deel",
    "remote",
    "bytedance",
    "tiktok",
    "snap",
    "pinterest",
    "reddit",
    "discord",
    "slack",
    "zoom",
    "dropbox",
    "box",
    "asana",
    "monday",
    "atlassian",
    "jira",
    "github",
    "gitlab",
    "bitbucket",
    "linear",
    "vercel",
    "robinhood",
    "sofi",
    "chime",
    "affirm",
    "klarna",
    "square",
    "block",
    "oracle",
    "ibm",
    "sap",
    "cisco",
    "dell",
    "hp",
    "lenovo",
    "samsung",
    "sony",
    "lg",
    "lockheed martin",
    "raytheon",
    "northrop grumman",
    "boeing",
    "general dynamics",
    "jpmorgan",
    "goldman sachs",
    "morgan stanley",
    "citadel",
    "two sigma",
    "jane street",
    "de shaw",
    "bridgewater",
    "point72",
    "millennium",
    "mckinsey",
    "bain",
    "bcg",
    "deloitte",
    "accenture",
    "pwc",
    "ey",
    "kpmg",
}

_allowed_cache: set[str] | None = None


async def _load_allowlist() -> set[str]:
    """Load allowlist from seed + DB config."""
    global _allowed_cache
    if _allowed_cache is not None:
        return _allowed_cache

    allowed = {c.lower() for c in _SEED_COMPANIES}

    try:
        async with acquire() as conn:
            # Load any additions from config table
            row = await conn.fetchval(
                "SELECT value FROM config WHERE key = 'tech_company_allowlist'"
            )
            if row:
                for company in row.split(","):
                    allowed.add(company.strip().lower())

            # Auto-expand from known jobs (companies we've already fetched JDs for)
            known = await conn.fetch(
                "SELECT DISTINCT LOWER(company) as company FROM jobs WHERE company != 'Unknown'"
            )
            for r in known:
                allowed.add(r["company"])
    except Exception as e:
        logger.warning("Failed to load allowlist from DB: %s", e)

    _allowed_cache = allowed
    return allowed


async def is_company_allowed(company: str) -> bool:
    """Check if a company is in the allowlist."""
    allowed = await _load_allowlist()
    return company.strip().lower() in allowed


async def queue_unknown_company(company: str, role: str):
    """Queue an unknown company for user review in labeling dashboard."""
    try:
        async with acquire() as conn:
            await conn.execute(
                """
                INSERT INTO labeling_queue (email_id, subject, snippet, body, guessed_company, guessed_role, resolved)
                VALUES ($1, $2, $3, $4, $5, $6, FALSE)
                ON CONFLICT (email_id) DO NOTHING
                """,
                f"rec-{company}-{role}",  # synthetic email_id for recommendations
                f"Job recommendation: {role} at {company}",
                f"Recommended: {role} at {company}",
                "",
                company,
                role,
            )
    except Exception as e:
        logger.warning("Failed to queue unknown company: %s", e)


def invalidate_cache():
    """Clear the allowlist cache (call after user adds companies)."""
    global _allowed_cache
    _allowed_cache = None
