"""
Canonical definition references.

Studyfyr identifies a definition the way FHIR does, by packing the business version into the url:
``https://ai4hf.eu/cohorts/maggic|1.3``. The Passport keeps the two apart, in a ``url`` column and a
``version`` column, because ``(url, version)`` is the identity every sync is keyed on.

A definition that declares no version yields a bare url, which is a legitimate state the Passport
records as a null version rather than inventing one.
"""

from typing import Optional, Tuple

CANONICAL_SEPARATOR = "|"


def split_canonical(canonical: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    Split a canonical reference into its url and version.

    >>> split_canonical("https://ai4hf.eu/cohorts/maggic|1.3")
    ('https://ai4hf.eu/cohorts/maggic', '1.3')
    >>> split_canonical("https://ai4hf.eu/cohorts/maggic")
    ('https://ai4hf.eu/cohorts/maggic', None)
    >>> split_canonical(None)
    (None, None)

    :param canonical: the canonical reference as Studyfyr writes it
    :return: (url, version), with version None when the definition declares none
    """
    if canonical is None:
        return None, None

    url, separator, version = canonical.partition(CANONICAL_SEPARATOR)
    url = url.strip() or None
    if not separator:
        return url, None

    version = version.strip()
    return url, version or None


def join_canonical(url: Optional[str], version: Optional[str]) -> Optional[str]:
    """
    Rebuild a canonical reference from its parts, for logging and for reporting coverage back.

    :param url: the definition url
    :param version: the business version, or None
    :return: the canonical reference, or None when there is no url
    """
    if not url:
        return None
    return f"{url}{CANONICAL_SEPARATOR}{version}" if version else url
