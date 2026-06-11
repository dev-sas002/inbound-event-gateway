from __future__ import annotations


def is_low_confidence(confidence_score: float, threshold: float) -> bool:
    return confidence_score < threshold
