"""
Dataset sync (integrations #1 and #2).

After the site has run ``$extract``, this module reads the descriptor Studyfyr produced and records
in the central Passport what was extracted, where from, and against which version of which definition.
If quality criteria are configured it then runs ``$quality`` and records the assessment too.

Two things separate it from the prototype connector it replaces:

* **Idempotency.** Every write is lookup-then-create, keyed on ``(url, version)`` for definitions and
  on the Studyfyr extraction job id recorded in ``wasGeneratedBy`` for a dataset. Re-running
  converges; the prototype duplicated everything.
* **Coverage.** The descriptor is richer than the prototype used. The fields it dropped - the
  derivation of each feature, the temporal window, the codings, the cohort size - are the ones v2.7
  added columns for, so they are carried over rather than left to a steward to retype.
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..canonical import join_canonical, split_canonical
from ..config import AgentConfig
from ..feast import DefinitionsDirectory, FeastClient
from ..passport import PassportClient

logger = logging.getLogger(__name__)


def _as_instant(value: Any) -> Optional[str]:
    """
    Normalise a Studyfyr timestamp to an ISO-8601 instant.

    Studyfyr serialises instants as epoch **milliseconds**, so a numeric value is converted rather
    than stringified - slicing the digits of an epoch yields a plausible-looking wrong date.

    :param value: epoch milliseconds, an ISO-8601 string, or None
    :return: an ISO-8601 instant, or None
    """
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        seconds = int(value) / 1000.0
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


def _as_date(value: Any) -> Optional[str]:
    """
    Reduce a Studyfyr timestamp to the date the Passport stores for temporal coverage.

    :param value: epoch milliseconds, an ISO-8601 instant or date, or None
    :return: the date part as yyyy-mm-dd, or None
    """
    instant = _as_instant(value)
    return instant[:10] if instant else None


def _feature_statistics(descriptor: Dict[str, Any], name: str, outcome: bool) -> Dict[str, Any]:
    """Look a variable's statistics up in the descriptor, from the right side of the split."""
    stats = descriptor.get("datasetStats") or {}
    bucket = stats.get("outcomeStats" if outcome else "featureStats") or {}
    return bucket.get(name) or {}


def _concept_of(variable: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """
    Pull the coding out of a variable's valueSet binding.

    HealthDCAT-AP wants to know which coding systems a dataset speaks; Studyfyr already knows, because
    the definition bound the variable to a value set.

    :param variable: a descriptor variable
    :return: uri, system and code, any of which may be None
    """
    value_set = variable.get("valueSet") or {}
    # Studyfyr serialises the binding's concepts as a list of {code, display[, system]}; a single concept
    # names the code, several only the value set they belong to.
    concepts = value_set.get("concept") or []
    if isinstance(concepts, dict):
        concepts = [concepts]
    systems = {c.get("system") for c in concepts if c.get("system")}
    return {
        "conceptUri": value_set.get("url") or (concepts[0].get("uri") if len(concepts) == 1 else None),
        "conceptSystem": value_set.get("system") or (systems.pop() if len(systems) == 1 else None),
        "conceptCode": concepts[0].get("code") if len(concepts) == 1 else None,
    }


def run(config: AgentConfig, passport: PassportClient) -> Dict[str, Any]:
    """
    Sync one extracted dataset, and its quality assessment when criteria are configured.

    :param config: the module's configuration
    :param passport: the Passport client, authenticated as the dataset-sync service account
    :return: a summary of what was recorded
    """
    feast = FeastClient(config.feast_url, timeout=config.request_timeout)
    descriptor = feast.dataset(config.dataset_id)
    logger.info("Read descriptor for dataset %s from Studyfyr", config.dataset_id)

    population = _sync_population(config, passport, descriptor)
    feature_set = _sync_feature_set(config, passport, descriptor)
    dataset = _sync_dataset(config, passport, descriptor, population, feature_set)
    features = _sync_features(config, passport, descriptor, feature_set, dataset)
    _sync_concepts(passport, descriptor, dataset)

    summary = {
        "populationId": population.get("populationId"),
        "featuresetId": feature_set.get("featuresetId"),
        "datasetId": dataset.get("datasetId"),
        "features": len(features),
    }

    if config.quality_criteria_ids:
        summary["qualityAssessments"] = _sync_quality(
            config, passport, feast, descriptor, dataset, feature_set, features
        )

    return summary


# ---------------------------------------------------------------- definitions


def _sync_population(
    config: AgentConfig, passport: PassportClient, descriptor: Dict[str, Any]
) -> Dict[str, Any]:
    """Record the cohort definition the extraction ran, identified by (url, version)."""
    reference = descriptor.get("population") or {}
    url, version = split_canonical(reference.get("url"))
    if not url:
        raise ValueError("The descriptor carries no population url; cannot identify the cohort")

    body = {
        "studyId": config.study_id,
        "populationUrl": url,
        "version": version,
        "description": reference.get("description"),
        "characteristics": reference.get("title"),
    }

    return passport.find_or_create(
        "/population",
        passport.populations(),
        lambda existing: existing.get("populationUrl") == url and existing.get("version") == version,
        body,
        f"Population {join_canonical(url, version)}",
    )


def _sync_feature_set(
    config: AgentConfig, passport: PassportClient, descriptor: Dict[str, Any]
) -> Dict[str, Any]:
    """Record the feature set definition the extraction ran, identified by (url, version)."""
    reference = descriptor.get("featureSet") or {}
    url, version = split_canonical(reference.get("url"))
    if not url:
        raise ValueError("The descriptor carries no featureSet url; cannot identify the feature set")

    body = {
        "experimentId": config.experiment_id,
        "title": reference.get("title"),
        "featuresetURL": url,
        "version": version,
        "description": reference.get("description"),
    }

    return passport.find_or_create(
        "/featureset",
        passport.feature_sets(),
        lambda existing: existing.get("featuresetURL") == url and existing.get("version") == version,
        body,
        f"Featureset {join_canonical(url, version)}",
    )


# ---------------------------------------------------------------- dataset


def _sync_dataset(
    config: AgentConfig,
    passport: PassportClient,
    descriptor: Dict[str, Any],
    population: Dict[str, Any],
    feature_set: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Record the extracted dataset.

    A dataset is identified by the extraction that produced it. Studyfyr's descriptor id *is* the
    extraction job, so it is recorded as ``wasGeneratedBy`` and used as the identity: re-running the
    agent over the same extraction converges, while a fresh extraction over newer data becomes a new
    dataset.

    ``wasGeneratedBy`` names the job by its full URL, but only the job id identifies it: the address
    the agent reaches Studyfyr at (``FEAST_URL``) is deployment detail, and changing it must not turn
    an extraction already recorded into a new dataset.
    """
    data_source = descriptor.get("dataSource") or {}
    population_stats = descriptor.get("populationStats") or {}
    dataset_stats = descriptor.get("datasetStats") or {}
    temporal = descriptor.get("temporal") or {}

    featureset_id = feature_set.get("featuresetId")
    population_id = population.get("populationId")
    version = data_source.get("version")
    # The extraction job, named where it can be looked up again.
    job_path = f"/Dataset/{descriptor.get('id')}" if descriptor.get("id") else None
    generated_by = f"{config.feast_url}{job_path}" if job_path else None

    # Same site, same definitions: the only datasets this extraction can already be recorded as.
    candidates = [
        d
        for d in passport.datasets()
        if d.get("featuresetId") == featureset_id
        and d.get("populationId") == population_id
        and d.get("organizationId") == config.organization_id
    ]
    if generated_by:
        # The study listing omits wasGeneratedBy, so the candidates are read in full.
        full_records = (passport.dataset(d.get("datasetId")) or {} for d in candidates)
        already = next(
            (d for d in full_records if (d.get("wasGeneratedBy") or "").endswith(job_path)), None
        )
    else:
        # Without an extraction id only what produced the dataset can identify it.
        already = next((d for d in candidates if d.get("version") == version), None)
    if already:
        logger.info("  = Dataset %s already recorded", already.get("datasetId"))
        return already

    feature_set_ref = descriptor.get("featureSet") or {}
    population_ref = descriptor.get("population") or {}

    body = {
        "featuresetId": featureset_id,
        "populationId": population_id,
        "organizationId": config.organization_id,
        "title": descriptor.get("name") or feature_set_ref.get("title") or data_source.get("name"),
        "description": descriptor.get("description")
        or f"{feature_set_ref.get('title')} extracted for the cohort: {population_ref.get('title')}",
        "version": version,
        "referenceEntity": (descriptor.get("population") or {}).get("title"),
        "numberOfRecords": dataset_stats.get("numOfEntries"),
        "synthetic": False,
        # HealthDCAT-AP fields Studyfyr already computes, which the prototype dropped and a steward
        # would otherwise have to retype.
        "temporalCoverageStart": _as_date(temporal.get("start")),
        "temporalCoverageEnd": _as_date(temporal.get("end")),
        "numberOfUniqueIndividuals": population_stats.get("numOfEntries"),
        "wasGeneratedBy": generated_by,
        "provenanceStatement": _provenance_statement(descriptor),
    }

    created = passport.post("/dataset", body)
    logger.info("  + Dataset %s version %s created", body["title"], version)
    return created


def _provenance_statement(descriptor: Dict[str, Any]) -> str:
    """
    Describe, in one sentence a reader of the signed passport can follow, how the dataset was produced.

    :param descriptor: the Studyfyr descriptor
    :return: the provenance sentence
    """
    data_source = descriptor.get("dataSource") or {}
    population = join_canonical(*split_canonical((descriptor.get("population") or {}).get("url")))
    feature_set = join_canonical(*split_canonical((descriptor.get("featureSet") or {}).get("url")))
    engine_version = descriptor.get("onFhirFeastVersion")
    engine = f"onfhir-feast {engine_version}" if engine_version else "onfhir-feast"

    return (
        f"Extracted by {engine} from {data_source.get('name')} "
        f"({data_source.get('interface')} {data_source.get('version')}) "
        f"using cohort {population} and feature set {feature_set}."
    )


# ---------------------------------------------------------------- features


def _sync_features(
    config: AgentConfig,
    passport: PassportClient,
    descriptor: Dict[str, Any],
    feature_set: Dict[str, Any],
    dataset: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Record every feature and outcome, then the statistics computed for each over this dataset.

    The Passport has no separate outcome entity, so an outcome is a feature with ``isOutcome`` set.
    """
    featureset_id = feature_set.get("featuresetId")
    dataset_id = dataset.get("datasetId")
    existing_features = passport.features(featureset_id)
    existing_characteristics = passport.dataset_characteristics(dataset_id)

    recorded: List[Dict[str, Any]] = []
    for outcome, variables in ((False, descriptor.get("features") or []), (True, descriptor.get("outcomes") or [])):
        for variable in variables:
            feature = _sync_feature(passport, variable, featureset_id, existing_features, outcome, descriptor)
            recorded.append(feature)
            _sync_characteristics(
                passport, descriptor, variable, feature, dataset_id, outcome, existing_characteristics
            )

    return recorded


def _sync_feature(
    passport: PassportClient,
    variable: Dict[str, Any],
    featureset_id: str,
    existing_features: List[Dict[str, Any]],
    outcome: bool,
    descriptor: Dict[str, Any],
) -> Dict[str, Any]:
    """Record one feature, with the derivation Studyfyr generated for it (D5)."""
    name = variable.get("name")
    statistics = _feature_statistics(descriptor, name, outcome)
    total_entries = (descriptor.get("datasetStats") or {}).get("numOfEntries")
    not_null = statistics.get("numOfNotNull")

    generated = variable.get("generatedDescription") or []
    body = {
        "featuresetId": featureset_id,
        "title": name,
        "description": variable.get("description"),
        "dataType": variable.get("dataType"),
        "isOutcome": outcome,
        # A feature with a value on every row is mandatory in practice, which is what the Passport records.
        "mandatory": bool(not_null is not None and total_entries is not None and not_null == total_entries),
        "isUnique": False,
        "units": "Unknown",
        "equipment": "Unknown",
        "dataCollection": "Unknown",
        # D5: how the value was derived, and which feature group it came from.
        "extractionDefinition": "; ".join(generated) if generated else None,
        "extractionDefinitionUrl": variable.get("featureGroupUrl") or variable.get("basedOn"),
    }
    body.update(_concept_of(variable))

    return passport.find_or_create(
        "/feature",
        existing_features,
        lambda existing: existing.get("title") == name and bool(existing.get("isOutcome")) == outcome,
        body,
        f"Feature {name}",
    )


def _sync_characteristics(
    passport: PassportClient,
    descriptor: Dict[str, Any],
    variable: Dict[str, Any],
    feature: Dict[str, Any],
    dataset_id: str,
    outcome: bool,
    existing: List[Dict[str, Any]],
) -> None:
    """
    Record each statistic Studyfyr computed for a variable over this dataset.

    The statistic names are not a fixed list - whatever the engine reports is carried across as-is,
    so a new statistic needs no change here.
    """
    feature_id = feature.get("featureId")
    statistics = _feature_statistics(descriptor, variable.get("name"), outcome)

    for characteristic_name, value in statistics.items():
        already = any(
            e.get("datasetId") == dataset_id
            and e.get("featureId") == feature_id
            and e.get("characteristicName") == characteristic_name
            for e in existing
        )
        if already or value is None:
            continue

        passport.post(
            "/feature-dataset-characteristic",
            {
                "datasetId": dataset_id,
                "featureId": feature_id,
                "characteristicName": characteristic_name,
                # Structured statistics (histograms, per-item cardinalities) as JSON, not a Python repr.
                "value": json.dumps(value) if isinstance(value, (dict, list)) else str(value),
                "valueDataType": type(value).__name__,
            },
        )


def _sync_concepts(passport: PassportClient, descriptor: Dict[str, Any], dataset: Dict[str, Any]) -> None:
    """
    Record the distinct coding systems the dataset's variables are bound to.

    HealthDCAT-AP asks which vocabularies a dataset speaks; the definitions already say, per variable,
    so the answer is derived rather than typed.
    """
    dataset_id = dataset.get("datasetId")
    existing = passport.dataset_concepts(dataset_id)
    # A concept is identified by its uri; the label is presentation and may be curated by a steward later.
    seen = {c.get("conceptUri") for c in existing}

    for variable in (descriptor.get("features") or []) + (descriptor.get("outcomes") or []):
        value_set = variable.get("valueSet") or {}
        uri = value_set.get("url")
        # Studyfyr's descriptor carries only the value set url; its last path segment is the best label.
        label = value_set.get("title") or value_set.get("name") or (uri.rstrip("/").rsplit("/", 1)[-1] if uri else None)
        if not uri or uri in seen:
            continue
        seen.add(uri)
        passport.post(
            "/dataset-concept",
            {
                "datasetId": dataset_id,
                "propertyUri": "http://www.w3.org/ns/dcat#theme",
                "conceptUri": uri,
                "prefLabel": label,
                "conceptScheme": value_set.get("system"),
            },
        )


# ---------------------------------------------------------------- quality


def _sync_quality(
    config: AgentConfig,
    passport: PassportClient,
    feast: FeastClient,
    descriptor: Dict[str, Any],
    dataset: Dict[str, Any],
    feature_set: Dict[str, Any],
    features: List[Dict[str, Any]],
) -> int:
    """
    Run each configured quality criteria over the dataset and record the assessment.

    The report identifies the criteria definition canonically, so the Passport records which version
    of which rule set produced the result - the same identity the definitions themselves are keyed on.

    One assessment per dataset and criteria definition: every ``$quality`` call is a new execution (new
    "issued"), so a re-run must not evaluate again and record a second one. An assessment that exists
    but lacks some of its results (an interrupted run) is completed instead.

    :return: how many assessments were recorded
    """
    definitions = DefinitionsDirectory(config.definitions_dir)
    features_by_title = {f.get("title"): f.get("featureId") for f in features}
    dataset_id = dataset.get("datasetId")
    recorded = 0

    for criteria_id in config.quality_criteria_ids:
        assessment = _existing_assessment(config, passport, definitions, dataset_id, criteria_id)
        if assessment:
            _complete_assessment(config, passport, feast, definitions, assessment, criteria_id, features_by_title)
            continue

        report = feast.evaluate_quality(config.dataset_id, criteria_id)
        reference = report.get("criteria") or {}
        url, version = split_canonical(reference.get("url"))

        criteria = passport.find_or_create(
            "/quality-criteria",
            passport.quality_criteria(config.experiment_id),
            lambda existing: existing.get("url") == url and existing.get("version") == version,
            {
                "experimentId": config.experiment_id,
                "title": reference.get("title"),
                "url": url,
                "version": version,
                "description": reference.get("description"),
            },
            f"QualityCriteria {join_canonical(url, version)}",
        )
        criteria_id_in_passport = criteria.get("qualityCriteriaId")

        criterion_by_name = _sync_criterion(
            passport, definitions, criteria_id_in_passport, url, criteria_id, report, features_by_title
        )
        if _record_assessment(passport, dataset, criteria_id_in_passport, criteria_id, report, criterion_by_name):
            recorded += 1

    return recorded


def _criteria_marker(criteria_id: str) -> str:
    """
    How an assessment's summary names the Studyfyr criteria definition it ran.

    Studyfyr does not serve the definition, so without the definitions checkout its url and version are
    unknown until ``$quality`` has run; the summary is what lets a re-run recognise the assessment.
    """
    return f"(DatasetQualityCriteria {criteria_id})"


def _existing_assessment(
    config: AgentConfig,
    passport: PassportClient,
    definitions: DefinitionsDirectory,
    dataset_id: str,
    criteria_id: str,
) -> Optional[Dict[str, Any]]:
    """
    The assessment of this dataset against this criteria definition, if one is recorded.

    Matched on the criteria id the summary names, which needs nothing but the Passport; assessments
    recorded before the summary carried it are matched on the definition's (url, version) when the
    definitions checkout holds it.
    """
    assessments = passport.quality_assessments(dataset_id)
    marker = _criteria_marker(criteria_id)
    for assessment in assessments:
        if marker in (assessment.get("summary") or ""):
            return assessment

    definition = definitions.quality_criteria(None, criteria_id)
    if definition and definition.get("url"):
        known = {
            c.get("qualityCriteriaId")
            for c in passport.quality_criteria(config.experiment_id)
            if c.get("url") == definition.get("url") and c.get("version") == definition.get("version")
        }
        for assessment in assessments:
            if assessment.get("qualityCriteriaId") in known:
                return assessment
    return None


def _expected_results(passport: PassportClient, assessment: Dict[str, Any]) -> int:
    """How many results an assessment was recorded with: its summary says ("3 of 4 criteria passed")."""
    counted = re.search(r"(\d+) of (\d+) criteria passed", assessment.get("summary") or "")
    if counted:
        return int(counted.group(2))
    return len(passport.quality_criterion(assessment.get("qualityCriteriaId")))


def _complete_assessment(
    config: AgentConfig,
    passport: PassportClient,
    feast: FeastClient,
    definitions: DefinitionsDirectory,
    assessment: Dict[str, Any],
    criteria_id: str,
    features_by_title: Dict[str, str],
) -> int:
    """
    Write the results an existing assessment lacks.

    Studyfyr keeps no report, so the missing results can only come from evaluating again; that happens
    only when results are missing, and they are added to the existing assessment rather than recorded
    as a second one. The dataset is a finished extraction, so the evaluation yields the same values.

    :return: how many results were added
    """
    assessment_id = assessment.get("qualityAssessmentId")
    results = passport.quality_results(assessment_id)
    if len(results) >= _expected_results(passport, assessment):
        logger.info("  = QualityAssessment against %s already recorded for this dataset", criteria_id)
        return 0

    logger.info("  ~ QualityAssessment %s lacks results: evaluating again to complete it", assessment_id)
    report = feast.evaluate_quality(config.dataset_id, criteria_id)
    url, _version = split_canonical((report.get("criteria") or {}).get("url"))
    quality_criteria_id = assessment.get("qualityCriteriaId")
    criterion_by_name = _sync_criterion(
        passport, definitions, quality_criteria_id, url, criteria_id, report, features_by_title
    )
    return _record_results(passport, assessment_id, report, criterion_by_name, results)


def _sync_criterion(
    passport: PassportClient,
    definitions: DefinitionsDirectory,
    quality_criteria_id: str,
    canonical_url: Optional[str],
    definition_id: str,
    report: Dict[str, Any],
    features_by_title: Dict[str, str],
) -> Dict[str, str]:
    """
    Record the individual rules of a criteria definition.

    The report carries each rule's outcome but not its expression, so the rules themselves come from
    the definitions checkout. When the checkout does not hold them the assessment is still recorded -
    the results simply reference rules described only by name.

    :return: rule name -> the Passport's criterion id
    """
    definition = definitions.quality_criteria(canonical_url, definition_id)
    existing = passport.quality_criterion(quality_criteria_id)
    by_name = {c.get("name"): c.get("qualityCriterionId") for c in existing}

    rules = (definition or {}).get("criteria") or [
        {"name": result.get("name"), "description": result.get("description")}
        for result in (report.get("results") or [])
    ]

    for rule in rules:
        name = rule.get("name")
        if name in by_name:
            continue
        # Studyfyr's rule format nests the Kahn classification ("category": {context, category[,
        # subcategory]}) and the acceptance test ("rule": {expression, low, high}); the Passport stores
        # both flat. Already-flat fields are accepted too.
        category = rule.get("category")
        classification = category if isinstance(category, dict) else {"category": category}
        acceptance = rule.get("rule") if isinstance(rule.get("rule"), dict) else {}
        created = passport.post(
            "/quality-criterion",
            {
                "qualityCriteriaId": quality_criteria_id,
                "featureId": features_by_title.get(rule.get("feature") or name),
                "name": name,
                "description": rule.get("description"),
                "category": classification.get("category"),
                "context": classification.get("context") or rule.get("context"),
                "subcategory": classification.get("subcategory") or rule.get("subcategory"),
                "language": rule.get("language") or ("application/sql" if rule.get("expression") else None),
                "expression": rule.get("expression"),
                "ruleExpression": acceptance.get("expression") or rule.get("ruleExpression"),
                "low": acceptance.get("low", rule.get("low")),
                "high": acceptance.get("high", rule.get("high")),
            },
        )
        by_name[name] = created.get("qualityCriterionId")
        logger.info("  + QualityCriterion %s created", name)

    return by_name


def _record_assessment(
    passport: PassportClient,
    dataset: Dict[str, Any],
    quality_criteria_id: str,
    criteria_id: str,
    report: Dict[str, Any],
    criterion_by_name: Dict[str, str],
) -> bool:
    """
    Record one run of a criteria set over the dataset, with a result per rule.

    :return: whether a new assessment was recorded
    """
    dataset_id = dataset.get("datasetId")
    results = report.get("results") or []
    passed = [r for r in results if r.get("passed")]
    executed_at = _as_instant(report.get("issued"))

    existing = next(
        (
            a
            for a in passport.quality_assessments(dataset_id)
            if a.get("qualityCriteriaId") == quality_criteria_id and a.get("executedAt") == executed_at
        ),
        None,
    )
    if existing:
        logger.info("  = QualityAssessment for %s already recorded", executed_at)
        assessment_id = existing.get("qualityAssessmentId")
        _record_results(passport, assessment_id, report, criterion_by_name, passport.quality_results(assessment_id))
        return False

    assessment = passport.post(
        "/quality-assessment",
        {
            "datasetId": dataset_id,
            "qualityCriteriaId": quality_criteria_id,
            "overallResult": "PASSED" if len(passed) == len(results) else "FAILED",
            # names the Studyfyr definition, which is how a re-run recognises this assessment
            "summary": f"{len(passed)} of {len(results)} criteria passed {_criteria_marker(criteria_id)}.",
            "executedAt": executed_at,
            "executedBy": "dataset-sync",
        },
    )
    logger.info("  + QualityAssessment created (%s of %s passed)", len(passed), len(results))
    _record_results(passport, assessment.get("qualityAssessmentId"), report, criterion_by_name, [])
    return True


def _record_results(
    passport: PassportClient,
    assessment_id: str,
    report: Dict[str, Any],
    criterion_by_name: Dict[str, str],
    existing: List[Dict[str, Any]],
) -> int:
    """
    Record the report's per-rule results under an assessment, leaving out those it already has.

    A result is identified by its criterion, or by its detail when the rule has no criterion record.

    :return: how many results were added
    """

    def key(criterion_id: Optional[str], detail: Optional[str]) -> tuple:
        return ("criterion", criterion_id) if criterion_id else ("detail", detail)

    have = {key(r.get("qualityCriterionId"), r.get("detail")) for r in existing}
    added = 0
    for result in report.get("results") or []:
        criterion_id = criterion_by_name.get(result.get("name"))
        identity = key(criterion_id, result.get("description"))
        if identity in have:
            continue
        have.add(identity)
        passport.post(
            "/quality-criterion-assessment-result",
            {
                "qualityAssessmentId": assessment_id,
                "qualityCriterionId": criterion_id,
                "value": result.get("value"),
                "result": "PASSED" if result.get("passed") else "FAILED",
                "detail": result.get("description"),
            },
        )
        added += 1
    if existing and added:
        logger.info("  + %s missing QualityCriterionAssessmentResult(s) added", added)
    return added
