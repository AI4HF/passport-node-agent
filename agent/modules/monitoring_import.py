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
import re
from datetime import datetime, time, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from ..config import AgentConfig
from ..passport import PassportClient

logger = logging.getLogger(__name__)

# Distinct measure names read per import: one terms bucket each. A few dozen are typical.
MAX_MEASURES = 500

# The two forms a period bound may take: a plain date, or a timestamp with at least hours and minutes
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}(:?\d{2})?)?")


def period_bound(value: Optional[str], end: bool) -> Optional[str]:
    """
    Normalise a period bound to a full UTC timestamp.

    A date alone means the whole day: the start of it for the period start, the end of it for the period
    end (Elasticsearch would otherwise read ``lte: 2026-03-31`` as midnight and drop that day, and the
    Passport's ``executedAt`` is an Instant, which rejects a date). A timestamp without a zone is UTC.
    Anything but a ``YYYY-MM-DD`` date or a full timestamp (with a time) is rejected: Python would read a
    compact ``20260331`` as midnight, not as the whole day.

    >>> period_bound("2026-03-31", end=True)
    '2026-03-31T23:59:59.999Z'
    >>> period_bound("20260331", end=True)
    Traceback (most recent call last):
    ...
    ValueError: Not a date or timestamp: '20260331'; write the date as YYYY-MM-DD (or a full ISO-8601 timestamp, e.g. 2026-03-31T23:59:59Z)

    :param value: a YYYY-MM-DD date or an ISO-8601 timestamp, or None
    :param end: whether this is the end of the period
    :return: the ISO-8601 UTC timestamp, or None
    :raises ValueError: when the value is neither
    """
    if not value:
        return None
    try:
        if _DATE.fullmatch(value):
            moment = datetime.combine(
                datetime.fromisoformat(value).date(), time.max if end else time.min, tzinfo=timezone.utc
            )
        elif _TIMESTAMP.fullmatch(value):
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
        else:
            raise ValueError(value)
    except ValueError as error:
        raise ValueError(
            f"Not a date or timestamp: {value!r}; write the date as YYYY-MM-DD "
            "(or a full ISO-8601 timestamp, e.g. 2026-03-31T23:59:59Z)"
        ) from error
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _bound_label(bound: str, end: bool) -> str:
    """
    How a normalised bound is named in the run's description: a plain date when it is the start (or, for
    the period end, the last second) of a UTC day, the UTC timestamp otherwise.
    """
    moment = datetime.fromisoformat(bound.replace("Z", "+00:00"))
    if not end and (moment.hour, moment.minute, moment.second, moment.microsecond) == (0, 0, 0, 0):
        return moment.date().isoformat()
    if end and (moment.hour, moment.minute, moment.second) == (23, 59, 59):
        return moment.date().isoformat()
    spec = "seconds" if moment.microsecond == 0 else "milliseconds"
    return moment.isoformat(timespec=spec).replace("+00:00", "Z")


def normalise_period(
    period_start: Optional[str], period_end: Optional[str]
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    The period as queried and as named, the same however it was typed.

    ``--period-end 2026-03-31`` and ``--period-end 2026-03-31T23:59:59Z`` are the same period: both are
    named ``2026-03-31`` (the form records written before this normalisation carry) and queried up to
    the end of that day.

    >>> normalise_period("2026-01-01T00:00:00Z", "2026-03-31T23:59:59Z")
    ('2026-01-01T00:00:00.000Z', '2026-03-31T23:59:59.999Z', '2026-01-01', '2026-03-31')
    >>> normalise_period(None, "2026-03-31T12:00:00+02:00")[1:]
    ('2026-03-31T10:00:00.000Z', None, '2026-03-31T10:00:00Z')

    :return: (start bound, end bound, start label, end label), each None when the bound is open
    """
    labels = []
    bounds = []
    for value, end in ((period_start, False), (period_end, True)):
        bound = period_bound(value, end)
        label = _bound_label(bound, end) if bound else None
        labels.append(label)
        bounds.append(period_bound(label, end) if label else None)
    return bounds[0], bounds[1], labels[0], labels[1]


def _statistics(start: Optional[str], end: Optional[str]) -> str:
    """The sentence naming the period, which ends the run's description and identifies the run."""
    period = f" between {start} and {end}" if start and end else (
        f" from {start}" if start else (f" until {end}" if end else "")
    )
    return f"Deployment-period statistics observed at this site{period}."


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
        # model_id is dynamically mapped as text (a UUID is split into tokens), so match its keyword field
        filters: List[Dict[str, Any]] = [{"term": {"model_id.keyword": model_id}}]
        if period_start or period_end:
            date_range: Dict[str, str] = {}
            if period_start:
                date_range["gte"] = period_start
            if period_end:
                date_range["lte"] = period_end
            filters.append({"range": {"timestamp": date_range}})

        query = {
            "size": 0,
            # The passport-monitoring-platform-connector also writes measures here: the model's training
            # and per-site evaluations, copied from the Passport, each carrying the id of its Passport run.
            # Those are not deployment-period observations; importing them would record the Passport's
            # own numbers back into it as a monitoring run.
            "query": {
                "bool": {
                    "filter": filters,
                    "must_not": [{"exists": {"field": "model_evaluation_id"}}],
                }
            },
            "aggs": {
                "by_measure": {
                    "terms": {"field": "name.keyword", "size": MAX_MEASURES},
                    "aggs": {"mean": {"avg": {"field": "value"}}},
                }
            },
        }

        url = f"{self._base_url}/{self._index}/_search"
        response = requests.post(url, json=query, auth=self._auth, timeout=self._timeout)
        if not response.ok:
            raise MonitoringError(f"POST {url} -> {response.status_code} {response.text[:300]}")

        by_measure = response.json().get("aggregations", {}).get("by_measure", {})
        if by_measure.get("sum_other_doc_count"):
            # more distinct measure names than buckets: say so rather than drop them silently
            raise MonitoringError(
                f"Model {model_id} has more than {MAX_MEASURES} distinct measures in that period; "
                "raise MAX_MEASURES in monitoring_import.py"
            )
        buckets = by_measure.get("buckets", [])
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

    start, end, start_label, end_label = normalise_period(period_start, period_end)
    measures = monitoring.measures_for_model(model_id, start, end)
    if not measures:
        logger.warning("The monitoring store holds no measures for model %s in that period", model_id)
        return {"modelEvaluationId": None, "measures": 0}

    # The period is always part of the description: it is what tells two imports for the same model apart,
    # and what a reader of the passport needs next to the numbers. It is named from the normalised bounds,
    # so the same period typed differently is the same run.
    statistics = _statistics(start_label, end_label)
    description = f"{reason.rstrip('.')}. {statistics}" if reason else statistics
    # Runs recorded before the normalisation name the period as it was typed
    legacy = _statistics(period_start, period_end)

    # One run per (model, organization, period): re-running the import after a partial failure must
    # not leave the passport with the same period recorded twice. The run has no period fields, so the
    # period is read from the end of its description; the reason before it may be worded differently
    # on each run and does not make it another period.
    existing = [
        e
        for e in passport.model_evaluations(model_id)
        if e.get("trigger") == "monitoring"
        and e.get("organizationId") == config.organization_id
        and (e.get("description") or "").endswith((statistics, legacy))
    ]
    if existing:
        evaluation_id = existing[0].get("modelEvaluationId")
        # An import interrupted after the run was created is completed: add the measures it lacks
        recorded = {m.get("name") for m in passport.evaluation_measures(evaluation_id)}
        missing = [m for m in measures if m["name"] not in recorded]
        if not missing:
            logger.info("= This monitoring period is already recorded for model %s", model_id)
        else:
            logger.info("~ Completing the monitoring run %s with %s measure(s)", evaluation_id, len(missing))
        _record_measures(passport, evaluation_id, missing)
        return {"modelEvaluationId": evaluation_id, "measures": len(missing)}

    evaluation = passport.post(
        "/model-evaluation",
        {
            "modelId": model_id,
            "organizationId": config.organization_id,
            "trigger": "monitoring",
            "aggregationMethod": "site-aggregate",
            # the end of the period it summarises, or now for an open-ended period
            "executedAt": end or period_bound(datetime.now(timezone.utc).isoformat(), end=True),
            "executedBy": "monitoring-import",
            "description": description,
        },
    )
    evaluation_id = evaluation.get("modelEvaluationId")
    logger.info("+ ModelEvaluation (monitoring) created for model %s", model_id)
    _record_measures(passport, evaluation_id, measures)
    return {"modelEvaluationId": evaluation_id, "measures": len(measures)}


def _record_measures(passport: PassportClient, evaluation_id: str, measures: List[Dict[str, Any]]) -> None:
    """Record the period means as the run's measures."""
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
