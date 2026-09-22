"""
The Passport REST client.

Two things distinguish this from the prototype connectors it replaces. It authenticates as a service
account rather than with a stored offline token, and its writes are *lookup-then-create*: re-running a
module converges on the same records instead of creating a second set. The prototypes duplicated
everything on every run, which made them unusable on a schedule.

Idempotency is keyed on whatever identifies the record in the domain - ``(url, version)`` for a
definition, ``(featuresetId, populationId, organizationId, version)`` for a dataset - not on a
synthetic key, because the node does not control the Passport's ids.
"""

import logging
from typing import Any, Callable, Dict, List, Optional

import requests

from .auth import ServiceAccountTokenProvider

logger = logging.getLogger(__name__)


class PassportError(RuntimeError):
    """Raised when the Passport rejects a call the agent cannot recover from."""


class PassportClient:
    """
    A thin REST client scoped to one study.

    :param base_url: the Passport server's base url
    :param study_id: the study every call is scoped to - the Passport's authorization scope
    :param tokens: the service account whose identity the writes carry
    :param timeout: per-request timeout in seconds
    """

    def __init__(
        self,
        base_url: str,
        study_id: str,
        tokens: ServiceAccountTokenProvider,
        timeout: int = 60,
    ):
        self._base_url = base_url.rstrip("/")
        self._study_id = study_id
        self._tokens = tokens
        self._timeout = timeout

    # ------------------------------------------------------------------ transport

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        """
        Issue a request, appending the study scope and retrying once with a fresh token on 401.

        :param method: HTTP method
        :param path: path below the base url, e.g. "/population"
        :return: the response
        """
        url = f"{self._base_url}{path}"
        params = dict(kwargs.pop("params", None) or {})
        params.setdefault("studyId", self._study_id)

        def send() -> requests.Response:
            return requests.request(
                method,
                url,
                params=params,
                headers={
                    "Authorization": f"Bearer {self._tokens.token()}",
                    "Content-Type": "application/json",
                },
                timeout=self._timeout,
                **kwargs,
            )

        response = send()
        if response.status_code == 401:
            logger.debug("Passport answered 401; refreshing the token and retrying once")
            self._tokens.refresh()
            response = send()
        return response

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """
        Read a resource or collection.

        :param path: path below the base url
        :param params: extra query parameters
        :return: the decoded body, or None on 404
        """
        response = self._request("GET", path, params=params)
        if response.status_code == 404:
            return None
        if not response.ok:
            raise PassportError(f"GET {path} -> {response.status_code} {response.text[:300]}")
        return response.json() if response.content else None

    def post(self, path: str, body: Dict[str, Any], params: Optional[Dict[str, Any]] = None) -> Any:
        """
        Create a resource.

        :param path: path below the base url
        :param body: the record to create
        :param params: extra query parameters
        :return: the created record
        """
        response = self._request("POST", path, json=body, params=params)
        if not response.ok:
            raise PassportError(f"POST {path} -> {response.status_code} {response.text[:300]}")
        return response.json() if response.content else None

    def put(self, path: str, body: Dict[str, Any], params: Optional[Dict[str, Any]] = None) -> Any:
        """
        Update a resource.

        :param path: path below the base url
        :param body: the updated record
        :param params: extra query parameters
        :return: the updated record
        """
        response = self._request("PUT", path, json=body, params=params)
        if not response.ok:
            raise PassportError(f"PUT {path} -> {response.status_code} {response.text[:300]}")
        return response.json() if response.content else None

    # ------------------------------------------------------------------ idempotent writes

    def find_or_create(
        self,
        path: str,
        listing: List[Dict[str, Any]],
        matches: Callable[[Dict[str, Any]], bool],
        body: Dict[str, Any],
        describe: str,
    ) -> Dict[str, Any]:
        """
        Return the existing record that matches, or create one.

        This is the whole of the agent's idempotency: the node re-reads what the Passport already
        holds for the study and only writes what is genuinely new, so a re-run after a partial
        failure completes the work rather than duplicating it.

        :param path: the collection path, e.g. "/population"
        :param listing: the records already in the study
        :param matches: predicate identifying the record in the domain
        :param body: the record to create when nothing matches
        :param describe: a human-readable name for the log line
        :return: the existing or newly created record
        """
        for existing in listing:
            if matches(existing):
                logger.info("  = %s already recorded", describe)
                return existing

        created = self.post(path, body)
        logger.info("  + %s created", describe)
        return created

    # ------------------------------------------------------------------ collections

    def populations(self) -> List[Dict[str, Any]]:
        """All populations of the study."""
        return self.get("/population") or []

    def feature_sets(self) -> List[Dict[str, Any]]:
        """All feature sets of the study."""
        return self.get("/featureset") or []

    def features(self, featureset_id: str) -> List[Dict[str, Any]]:
        """The features of one feature set."""
        return self.get("/feature", {"featuresetId": featureset_id}) or []

    def datasets(self) -> List[Dict[str, Any]]:
        """All datasets of the study."""
        return self.get("/dataset") or []

    def dataset_characteristics(self, dataset_id: str) -> List[Dict[str, Any]]:
        """The feature characteristics recorded against one dataset."""
        return self.get("/feature-dataset-characteristic", {"datasetId": dataset_id}) or []

    def quality_criteria(self, experiment_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """The quality criteria of the study, optionally narrowed to one experiment."""
        params = {"experimentId": experiment_id} if experiment_id else None
        return self.get("/quality-criteria", params) or []

    def quality_criterion(self, quality_criteria_id: str) -> List[Dict[str, Any]]:
        """The criteria within one quality criteria group."""
        return self.get("/quality-criterion", {"qualityCriteriaId": quality_criteria_id}) or []

    def quality_assessments(self, dataset_id: str) -> List[Dict[str, Any]]:
        """The quality assessments recorded against one dataset."""
        return self.get("/quality-assessment", {"datasetId": dataset_id}) or []

    def catalogue_datasets(self, dataset_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """The publication records of the study, optionally for one dataset."""
        params = {"datasetId": dataset_id} if dataset_id else None
        return self.get("/catalogue-dataset", params) or []

    def dataset_concepts(self, dataset_id: str) -> List[Dict[str, Any]]:
        """The controlled-vocabulary values recorded against one dataset."""
        return self.get("/dataset-concept", {"datasetId": dataset_id}) or []

    def catalogue_registrations(self, catalogue_dataset_id: str) -> List[Dict[str, Any]]:
        """Where a published dataset has been listed."""
        return self.get("/catalogue-registration", {"catalogueDatasetId": catalogue_dataset_id}) or []

    def model_evaluations(self, model_id: str) -> List[Dict[str, Any]]:
        """The evaluation runs of one model."""
        return self.get("/model-evaluation", {"modelId": model_id}) or []

    def models(self) -> List[Dict[str, Any]]:
        """All models of the study."""
        return self.get("/model") or []
