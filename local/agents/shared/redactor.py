"""Presidio PII redactor for resume upload and agent PII boundary enforcement.

Usage:
    redactor = PiiRedactor()
    clean_text = redactor.redact("John Doe lives at 123 Main St")
    # Returns: "<PERSON> lives at <LOCATION>"

    enforce_pii_boundary(data_dict)
    # Raises ValueError if PII detected in any string field

Security rule: enforce_pii_boundary() before every RDS write in agents.
"""

from presidio_analyzer import AnalyzerEngine, PatternRecognizer, Pattern
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig


def _build_url_recognizer() -> PatternRecognizer:
    """Recognizer for URLs including LinkedIn, GitHub, and personal websites."""
    return PatternRecognizer(
        supported_entity="URL",
        name="url_recognizer",
        patterns=[
            Pattern(
                "linkedin", r"(?:https?://)?(?:www\.)?linkedin\.com/in/[\w\-]+/?", 0.9
            ),
            Pattern("github", r"(?:https?://)?(?:www\.)?github\.com/[\w\-]+/?", 0.8),
            Pattern("url", r"https?://[^\s<>\"']+", 0.6),
        ],
    )


class PiiRedactor:
    """Strips PII using Microsoft Presidio + spaCy en_core_web_lg."""

    # Entity types to detect and redact
    ENTITIES = [
        "PERSON",
        "EMAIL_ADDRESS",
        "PHONE_NUMBER",
        "US_SSN",
        "CREDIT_CARD",
        "LOCATION",
        "US_DRIVER_LICENSE",
        "US_PASSPORT",
        "IP_ADDRESS",
        "URL",
    ]

    def __init__(self):
        self._analyzer = AnalyzerEngine()
        self._analyzer.registry.add_recognizer(_build_url_recognizer())
        self._anonymizer = AnonymizerEngine()

    def redact(self, text: str, language: str = "en") -> str:
        """Replace PII entities with type labels (e.g. <PERSON>)."""
        results = self._analyzer.analyze(
            text=text,
            entities=self.ENTITIES,
            language=language,
        )
        operators = {
            entity: OperatorConfig("replace", {"new_value": f"<{entity}>"})
            for entity in self.ENTITIES
        }
        anonymized = self._anonymizer.anonymize(
            text=text,
            analyzer_results=results,
            operators=operators,
        )
        return anonymized.text

    def contains_pii(self, text: str, language: str = "en") -> bool:
        """Check if text contains any PII without modifying it."""
        results = self._analyzer.analyze(
            text=text,
            entities=self.ENTITIES,
            language=language,
            score_threshold=0.7,
        )
        return len(results) > 0


_pii_redactor: PiiRedactor | None = None


def _get_pii_redactor() -> PiiRedactor:
    """Module-level singleton to avoid reloading spaCy on every call."""
    global _pii_redactor
    if _pii_redactor is None:
        _pii_redactor = PiiRedactor()
    return _pii_redactor


def enforce_pii_boundary(data: dict) -> None:
    """Raise ValueError if any string field in data contains PII.

    Call this before every RDS write in agents to prevent PII leaking to cloud DB.
    """
    redactor = _get_pii_redactor()
    for key, value in data.items():
        if isinstance(value, str) and redactor.contains_pii(value):
            raise ValueError(
                f"PII detected in field '{key}' — cannot write to cloud RDS. "
                "Strip PII with PiiRedactor.redact() first."
            )
