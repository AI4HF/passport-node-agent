"""
Reading Studyfyr (onfhir-feast).

Extraction stays a node-local operation: the site runs ``$extract`` on its own schedule and the agent
reads the descriptor afterwards (D2). Nothing here triggers an extraction, and the central Passport
never calls in (D4).

Quality is different in shape. Studyfyr has no endpoint that returns a *stored* quality report -
``$quality`` is a POST that evaluates a dataset against a criteria definition and returns the report
it just produced. So the agent invokes it, exactly as the data-quality prototype does, and records the
result. The report references the definition canonically as ``url|version``.

The criteria *definition* itself is not served over REST (``/definition`` covers Population,
FeatureGroup and FeatureSet only), so its rules are read from the definitions checkout the node
already has - the same tagged release Studyfyr loaded (integration #8).
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


class FeastError(RuntimeError):
    """Raised when Studyfyr cannot be read or a quality evaluation fails."""


class FeastClient:
    """
    Reads dataset descriptors from a local Studyfyr instance and runs quality evaluations.

    :param base_url: the Studyfyr API base url, e.g. http://localhost:8086/onfhir-feast/api
    :param timeout: per-request timeout in seconds
    :param quality_timeout: quality evaluation runs over the dataset, so it gets its own, longer timeout
    """

    def __init__(self, base_url: str, timeout: int = 60, quality_timeout: int = 900):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._quality_timeout = quality_timeout

    def dataset(self, dataset_id: str) -> Dict[str, Any]:
        """
        Read one dataset descriptor.

        :param dataset_id: the extraction job id Studyfyr uses as the dataset id
        :return: the descriptor
        :raises FeastError: when the dataset does not exist
        """
        url = f"{self._base_url}/Dataset/{dataset_id}"
        response = requests.get(url, timeout=self._timeout)
        if response.status_code == 404:
            raise FeastError(f"Studyfyr holds no dataset {dataset_id!r}")
        if not response.ok:
            raise FeastError(f"GET {url} -> {response.status_code} {response.text[:300]}")
        return response.json()

    def evaluate_quality(self, dataset_id: str, quality_criteria_id: str) -> Dict[str, Any]:
        """
        Run a quality evaluation and return the report it produces.

        :param dataset_id: the dataset to evaluate
        :param quality_criteria_id: the criteria definition to evaluate it against
        :return: the DatasetQualityReport
        :raises FeastError: when the evaluation fails
        """
        url = f"{self._base_url}/Dataset/{dataset_id}/DatasetQualityCriteria/{quality_criteria_id}/$quality"
        logger.info("Evaluating quality of %s against %s", dataset_id, quality_criteria_id)
        response = requests.post(url, timeout=self._quality_timeout)
        if not response.ok:
            raise FeastError(f"POST {url} -> {response.status_code} {response.text[:300]}")
        return response.json()


class DefinitionsDirectory:
    """
    The definitions checkout the node runs, read straight off disk.

    Studyfyr does not serve quality criteria definitions over REST, and the node already holds the
    tagged release it loaded them from, so the criteria rules are read from there.

    :param path: directory holding the definitions, e.g. a checkout of the feature-extraction-suite
    """

    def __init__(self, path: Optional[str]):
        self._path = path

    def quality_criteria(self, canonical_url: Optional[str], definition_id: str) -> Optional[Dict[str, Any]]:
        """
        Find a quality criteria definition by the canonical url the report cites, falling back to its id.

        :param canonical_url: the ``url`` the report's criteria reference carries, without the version
        :param definition_id: the definition's id, used when the url does not match anything
        :return: the definition, or None when the checkout does not hold it
        """
        if not self._path or not os.path.isdir(self._path):
            logger.info("No definitions directory configured; quality criteria rules will not be synced")
            return None

        for root, _dirs, files in os.walk(self._path):
            for name in files:
                if not name.endswith(".json"):
                    continue
                full = os.path.join(root, name)
                try:
                    with open(full, encoding="utf8") as handle:
                        doc = json.load(handle)
                except (ValueError, OSError):
                    continue
                if doc.get("resourceType") != "DatasetQualityCriteria":
                    continue
                if (canonical_url and doc.get("url") == canonical_url) or doc.get("id") == definition_id:
                    logger.debug("Matched quality criteria definition in %s", full)
                    return doc

        logger.warning(
            "No DatasetQualityCriteria definition found for url=%s id=%s under %s",
            canonical_url,
            definition_id,
            self._path,
        )
        return None
