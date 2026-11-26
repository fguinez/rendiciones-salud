"""Classifier protocol."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from rendiciones.domain import Extraction


@dataclass(frozen=True)
class RecognitionInput:
    """What a classifier receives for one document."""

    path: Path
    mime: str
    text: str
    folder: str = ""


class Classifier(Protocol):
    """Turns a document into an Extraction. Implementations must not log content."""

    name: str

    def classify(self, doc: RecognitionInput) -> Extraction:
        """Classify the document and extract its fields."""
        ...
