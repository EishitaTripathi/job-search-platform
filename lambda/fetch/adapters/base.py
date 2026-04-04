"""Base class for all source adapters."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
import logging

logger = logging.getLogger(__name__)


@dataclass
class NormalizedJob:
    company: str
    role: str
    location: str
    ats_url: str
    date_posted: Optional[str]  # YYYY-MM-DD or None
    source: str
    source_id: str
    raw_json: dict


class SourceAdapter(ABC):
    """Abstract base for job source adapters."""

    source_name: str = ""
    tier: int = 0

    @abstractmethod
    def fetch(self, params: dict) -> list[NormalizedJob]:
        """Fetch and normalize job listings from this source."""
        ...

    def _validate_url(self, url: str) -> bool:
        """SSRF protection — reject private/loopback IPs."""
        from urllib.parse import urlparse
        import ipaddress

        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        try:
            ip = ipaddress.ip_address(parsed.hostname)
            if ip.is_private or ip.is_loopback or ip.is_reserved:
                return False
        except ValueError:
            pass  # hostname, not IP — OK
        return True
