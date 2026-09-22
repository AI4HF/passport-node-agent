"""
Monitoring import (integration #4).

Per-site monitoring results stay inside the hospital: the Monitoring Platform holds them and the
dashboards read them there. The passport is not a monitoring store, and nothing streams into it.

The one moment that changes is a **retraining**. The new model's passport has to document why it was
retrained and from what state, so at that point the deployment-period statistics of the model being
replaced are read from the local Elasticsearch and recorded centrally as a ModelEvaluation with
``trigger = 'monitoring'``, attributed to this organization (D10). There are no dataset links: these
numbers come from live inference, not from an extracted dataset.

*The retraining-event contract is still open* - what tells a node that a retraining has begun. Until
it is settled the module is invoked explicitly, with the model being replaced and the period to
summarise, which is also what a human would do when kicking off a retraining by hand.
"""

import logging
from typing import Any, Dict, List, Optional

import requests

from ..config import AgentConfig
from ..passport import PassportClient

logger = logging.getLogger(__name__)


class MonitoringError(RuntimeError):
    """Raised when the local monitoring store cannot be read."""


class MonitoringPlatformClient:
    """
    Reads aggregated measures for a model from the site's own Elasticsearch.

    :param base_url: the Elasticsearch base url
    :param index: the index or index pattern holding evaluation measures
    :param basic_auth: ``user:password`` when the store requires it
    :param timeout: per-request timeout in seconds
    """

    def __init__(
        self,
        base_url: str,
        index: str,
        basic_auth: Optional[str] = None,
        timeout: int = 60,
    ):
        self._base_url = base_url.rstrip("/")
        self._index = index
        self._auth = tuple(basic_auth.split(":", 1)) if basic_auth else None
        self._timeout = timeout

    def measures_for_model(
        self, model_id: str, period_start: Optional[str], period_end: Optional[str]
    ) -> List[Dict[str, Any]]:
        """
        Average each measure over the deployment period, which is what the passport records.

        Per-observation values stay at the node; only the aggregate crosses to the centre, which is
        the whole point of keeping monitoring local.

        :param model_id: the model whose deployment period is being summarised
        :param period_start: ISO-8601 start of the period, or None for open-ended
        :param period_end: ISO-8601 end of the period, or None for open-ended
        :return: one entry per measure name, with its mean value and observation count
        """
        filters: List[Dict[str, Any]] = [{"term": {"model_id": model_id}}]
        if period_start or period_end:
            date_range: Dict[str, str] = {}
            if period_start:
                date_range["gte"] = period_start
            if period_end:
                date_range["lte"] = period_end
            filters.append({"range": {"timestamp": date_range}})

        query = {
            "size": 0,
            "query": {"bool": {"filter": filters}},
            "aggs": {
                "by_measure": {
                    "terms": {"field": "name.keyword", "size": 100},
                    "aggs": {"mean": {"avg": {"field": "value"}}},
                }
            },
        }

        url = f"{self._base_url}/{self._index}/_search"
        response = requests.post(url, json=query, auth=self._auth, timeout=self._timeout)
        if not response.ok:
            raise MonitoringError(f"POST {url} -> {response.status_code} {response.text[:300]}")

        buckets = (
            response.json().get("aggregations", {}).get("by_measure", {}).get("buckets", [])
        )
        return [
            {
                "name": bucket.get("key"),
                "value": (bucket.get("mean") or {}).get("value"),
                "observations": bucket.get("doc_count"),
            }
            for bucket in buckets
            if (bucket.get("mean") or {}).get("value") is not None
        ]


def run(
    config: AgentConfig,
    passport: PassportClient,
    model_id: str,
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Import the deployment-period statistics of the model being replaced.

    :param config: the module's configuration
    :param passport: the Passport client, authenticated as the monitoring-import service account
    :param model_id: the model being replaced - the run is recorded against it, not its successor
    :param period_start: ISO-8601 start of the deployment period
    :param period_end: ISO-8601 end of the deployment period
    :param reason: what prompted the retraining, recorded on the evaluation
    :return: a summary of what was recorded
    """
    monitoring = MonitoringPlatformClient(
        config.elasticsearch_url,
        config.elasticsearch_index,
        config.elasticsearch_basic_auth,
        config.request_timeout,
    )

    measures = monitoring.measures_for_model(model_id, period_start, period_end)
    if not measures:
        logger.warning("The monitoring store holds no measures for model %s in that period", model_id)
        return {"modelEvaluationId": None, "measures": 0}

    description = reason or (
        f"Deployment-period statistics observed at this site"
        f"{f' between {period_start} and {period_end}' if period_start and period_end else ''}."
    )

    # One run per (model, organization, period): re-running the import after a partial failure must
    # not leave the passport with the same period recorded twice.
    existing = [
        e
        for e in passport.model_evaluations(model_id)
        if e.get("trigger") == "monitoring"
        and e.get("organizationId") == config.organization_id
        and e.get("description") == description
    ]
    if existing:
        logger.info("= This monitoring period is already recorded for model %s", model_id)
        return {"modelEvaluationId": existing[0].get("modelEvaluationId"), "measures": 0}

    evaluation = passport.post(
        "/model-evaluation",
        {
            "modelId": model_id,
            "organizationId": config.organization_id,
            "trigger": "monitoring",
            "aggregationMethod": "site-aggregate",
            "executedAt": period_end,
            "executedBy": "monitoring-import",
            "description": description,
        },
    )
    evaluation_id = evaluation.get("modelEvaluationId")
    logger.info("+ ModelEvaluation (monitoring) created for model %s", model_id)

    for measure in measures:
        passport.post(
            "/evaluation-measure",
            {
                "modelEvaluationId": evaluation_id,
                "name": measure["name"],
                "value": str(measure["value"]),
                "dataType": "float",
                "description": (
                    f"Mean {measure['name']} over {measure['observations']} observations "
                    f"during the deployment period at this site."
                ),
            },
        )
        logger.info("  + %s = %s", measure["name"], measure["value"])

    return {"modelEvaluationId": evaluation_id, "measures": len(measures)}
