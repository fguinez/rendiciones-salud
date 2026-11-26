"""Document recognition: text extraction and pluggable classifiers."""

from rendiciones.recognition.base import Classifier, RecognitionInput
from rendiciones.recognition.factory import build_classifier

__all__ = ["Classifier", "RecognitionInput", "build_classifier"]
