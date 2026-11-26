"""Choose the classifier: LLM when an API key is available, otherwise rules."""

from __future__ import annotations

import logging
import os

from rendiciones.recognition.base import Classifier
from rendiciones.recognition.rules import RuleBasedClassifier
from rendiciones.recognition.secrets import anthropic_api_key

logger = logging.getLogger(__name__)


def build_classifier(mode: str | None = None) -> Classifier:
    """Build a classifier.

    ``mode`` (or ``RENDICIONES_CLASSIFIER``) may be ``rules``, ``llm`` or ``auto`` (default).
    """
    mode = (mode or os.environ.get("RENDICIONES_CLASSIFIER", "auto")).lower()
    rules = RuleBasedClassifier()
    if mode == "rules":
        return rules
    key = anthropic_api_key()
    if key is None:
        if mode == "llm":
            logger.warning("LLM classifier requested but no API key found; using rules")
        return rules
    from rendiciones.recognition.llm import AnthropicClassifier

    logger.info("using LLM classifier")
    return AnthropicClassifier(api_key=key, fallback=rules)
