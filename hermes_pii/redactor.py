"""Local Presidio detection and replacement; no model downloads at runtime."""
from threading import RLock

import spacy
import tldextract
from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
from presidio_analyzer.nlp_engine import NerModelConfiguration, SpacyNlpEngine
from presidio_analyzer.predefined_recognizers import (
    CreditCardRecognizer,
    EmailRecognizer,
    IbanRecognizer,
    IpRecognizer,
    PhoneRecognizer,
    SpacyRecognizer,
)
from .masking import replace_detected_spans


class OfflineEmailRecognizer(EmailRecognizer):
    """Use the packaged suffix snapshot, not tldextract's remote refresh."""

    _extract = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)

    def validate_result(self, pattern_text):
        return self._extract(pattern_text).fqdn != ""


class PresidioRedactor:
    """Italian text by default; model must already be installed locally."""

    def __init__(self, language="it", model_name="it_core_news_sm", score_threshold=0.4):
        self.language = language
        self.score_threshold = score_threshold
        self._lock = RLock()
        # Do NOT call SpacyNlpEngine.load(): it downloads missing models.
        nlp = spacy.load(model_name)
        engine = SpacyNlpEngine(
            models=[{"lang_code": language, "model_name": model_name}],
            ner_model_configuration=NerModelConfiguration(
                model_to_presidio_entity_mapping={
                    "PER": "PERSON", "PERSON": "PERSON",
                    "LOC": "LOCATION", "GPE": "LOCATION", "LOCATION": "LOCATION",
                    "ORG": "ORGANIZATION", "ORGANIZATION": "ORGANIZATION",
                },
                low_score_entity_names=[],
                labels_to_ignore=["MISC"],
            ),
        )
        engine.nlp = {language: nlp}
        registry = RecognizerRegistry(supported_languages=[language])
        for recognizer in (
            OfflineEmailRecognizer(supported_language=language),
            PhoneRecognizer(supported_language=language, supported_regions=("IT", "CH", "US", "GB", "DE", "FR")),
            IbanRecognizer(supported_language=language),
            CreditCardRecognizer(supported_language=language),
            IpRecognizer(supported_language=language),
            SpacyRecognizer(supported_language=language, supported_entities=["PERSON", "LOCATION", "ORGANIZATION"]),
        ):
            registry.add_recognizer(recognizer)
        self._analyzer = AnalyzerEngine(
            registry=registry, nlp_engine=engine, supported_languages=[language]
        )

    def redact(self, text):
        """Replace detections with type placeholders, without a recovery mapping."""
        if not text:
            return text
        with self._lock:
            results = self._analyzer.analyze(
                text=text, language=self.language, score_threshold=self.score_threshold
            )
            return replace_detected_spans(text, results)
