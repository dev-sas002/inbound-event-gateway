"""
Normalisation errors.

Kept in their own module because both normalisers raise them and the
rule-based one must stay importable without the `openai` package — which it
would not be if these lived alongside the OpenAI client.
"""


class NormalizationError(Exception):
    """The payload could not be normalised, and retrying will not help."""


class RetryableNormalizationError(Exception):
    """A transient failure. The task should be retried with backoff."""
