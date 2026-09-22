"""
Metadata publisher (integration #5).

When a Data Steward marks one of their organization's datasets for publication, the description of
that dataset - never its contents - is published as HealthDCAT-AP RDF to the site's **own** FAIR Data
Point. Publication is node-side by design (D6): each organization publishes its own metadata to its
own FDP, under its own governance, rather than a central service publishing on its behalf.

Where it was listed is then written back to the Passport as a CatalogueRegistration, so the passport
records what was published where and when.

The module only ever reads datasets belonging to its own organization. A CatalogueDataset record is
itself the publication decision - the steward creates one only for a dataset meant to be published.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from ..config import AgentConfig
from ..passport import PassportClient

logger = logging.getLogger(__name__)

HEALTH_DCAT_AP = "http://healthdataportal.eu/ns/health#"

CATALOGUE_TYPE = "FDP"


class FdpError(RuntimeError):
    """Raised when the local FAIR Data Point rejects a publication."""


def _now_iso() -> str:
    """The instant the listing was made, as the Passport stores it."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _escape(value: str) -> str:
    """Escape a string for a Turtle literal."""
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


def to_healthdcat_ap(
    dataset: Dict[str, Any],
    publication: Dict[str, Any],
    concepts: List[Dict[str, Any]],
    distributions: List[Dict[str, Any]],
) -> str:
    """
    Render a dataset's publication metadata as HealthDCAT-AP in Turtle.

    Only the fields the steward approved for publication are rendered; the Passport's internal ids and
    the dataset's contents are not part of the description.

    :param dataset: the Passport dataset
    :param publication: its CatalogueDataset publication record
    :param concepts: its controlled-vocabulary values
    :param distributions: how the published dataset can be reached
    :return: a Turtle document
    """
    lines = [
        "@prefix dcat: <http://www.w3.org/ns/dcat#> .",
        "@prefix dct: <http://purl.org/dc/terms/> .",
        "@prefix foaf: <http://xmlns.com/foaf/0.1/> .",
        "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .",
        "@prefix dcatap: <http://data.europa.eu/r5r/> .",
        f"@prefix health: <{HEALTH_DCAT_AP}> .",
        "",
        "<> a dcat:Dataset ;",
    ]

    def literal(predicate: str, value: Optional[Any], datatype: Optional[str] = None) -> None:
        if value in (None, ""):
            return
        rendered = f'"{_escape(str(value))}"'
        if datatype:
            rendered += f"^^{datatype}"
        lines.append(f"    {predicate} {rendered} ;")

    def iri(predicate: str, value: Optional[str]) -> None:
        if value:
            lines.append(f"    {predicate} <{value}> ;")

    literal("dct:title", publication.get("publicTitle") or dataset.get("title"))
    literal("dct:description", publication.get("publicDescription") or dataset.get("description"))
    literal("dct:identifier", dataset.get("persistentIdentifier"))
    literal("dcat:version", dataset.get("version"))
    literal("dct:accessRights", publication.get("accessRights"))
    literal("dct:provenance", dataset.get("provenanceStatement"))
    literal("dct:conformsTo", dataset.get("conformsTo"))
    literal("dct:spatial", dataset.get("geographicalCoverage"))
    literal("dcat:temporalResolution", dataset.get("temporalResolution"))
    iri("dcat:landingPage", publication.get("landingPage"))
    iri("dct:isReferencedBy", publication.get("documentation"))
    literal("dct:publisher", publication.get("publisherName"))
    literal("dcat:contactPoint", publication.get("contactPoint"))
    literal("dct:accrualPeriodicity", publication.get("qualityAnnotation"))

    # HealthDCAT-AP specifics: who may release it, on what legal basis, and what it covers.
    iri("health:hdab", publication.get("hdabUri"))
    literal("foaf:name", publication.get("hdabName"))
    literal("health:legalBasis", publication.get("legalBasis"))
    literal("health:purpose", publication.get("purpose"))
    iri("dcatap:applicableLegislation", publication.get("applicableLegislation"))
    literal(
        "health:numberOfUniqueIndividuals",
        dataset.get("numberOfUniqueIndividuals"),
        "xsd:nonNegativeInteger",
    )
    literal("health:numberOfRecords", dataset.get("numberOfRecords"), "xsd:nonNegativeInteger")
    literal("health:minTypicalAge", dataset.get("minTypicalAge"), "xsd:nonNegativeInteger")
    literal("health:maxTypicalAge", dataset.get("maxTypicalAge"), "xsd:nonNegativeInteger")
    if publication.get("personalData") is not None:
        literal("health:personalData", str(bool(publication.get("personalData"))).lower(), "xsd:boolean")

    literal("health:retentionPeriodStart", publication.get("retentionPeriodStart"), "xsd:date")
    literal("health:retentionPeriodEnd", publication.get("retentionPeriodEnd"), "xsd:date")

    start = dataset.get("temporalCoverageStart")
    end = dataset.get("temporalCoverageEnd")
    if start or end:
        parts = []
        if start:
            parts.append(f'dcat:startDate "{start}"^^xsd:date')
        if end:
            parts.append(f'dcat:endDate "{end}"^^xsd:date')
        lines.append(f"    dct:temporal [ a dct:PeriodOfTime ; {' ; '.join(parts)} ] ;")

    for concept in concepts:
        iri("dcat:theme", concept.get("conceptUri"))

    for distribution in distributions:
        inner = []
        if distribution.get("accessUrl"):
            inner.append(f'dcat:accessURL <{distribution["accessUrl"]}>')
        if distribution.get("mediaType"):
            inner.append(f'dcat:mediaType "{_escape(distribution["mediaType"])}"')
        if distribution.get("title"):
            inner.append(f'dct:title "{_escape(distribution["title"])}"')
        if distribution.get("licence"):
            inner.append(f'dct:license "{_escape(distribution["licence"])}"')
        if distribution.get("format"):
            inner.append(f'dct:format "{_escape(distribution["format"])}"')
        if inner:
            lines.append(f"    dcat:distribution [ a dcat:Distribution ; {' ; '.join(inner)} ] ;")

    # close the description
    lines[-1] = lines[-1].rstrip(" ;") + " ."
    return "\n".join(lines) + "\n"


class FdpClient:
    """
    Publishes Turtle descriptions to the site's FAIR Data Point.

    :param base_url: the FDP base url
    :param catalog_id: the catalog new datasets are filed under
    :param token: bearer token when the FDP requires one
    :param timeout: per-request timeout in seconds
    """

    def __init__(
        self,
        base_url: str,
        catalog_id: Optional[str] = None,
        token: Optional[str] = None,
        timeout: int = 60,
    ):
        self._base_url = base_url.rstrip("/")
        self._catalog_id = catalog_id
        self._token = token
        self._timeout = timeout

    def publish(self, turtle: str) -> str:
        """
        Create a dataset entry and return the url it was published at.

        :param turtle: the HealthDCAT-AP description
        :return: the published resource's url
        :raises FdpError: when the FDP rejects it
        """
        headers = {"Content-Type": "text/turtle"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if self._catalog_id:
            headers["X-Catalog-Id"] = self._catalog_id

        url = f"{self._base_url}/dataset"
        response = requests.post(url, data=turtle.encode("utf8"), headers=headers, timeout=self._timeout)
        if not response.ok:
            raise FdpError(f"POST {url} -> {response.status_code} {response.text[:300]}")

        return response.headers.get("Location") or url


def run(config: AgentConfig, passport: PassportClient) -> Dict[str, Any]:
    """
    Publish every approved dataset of this organization that has not been published yet.

    :param config: the module's configuration
    :param passport: the Passport client, authenticated as the metadata-publisher service account
    :return: a summary of what was published
    """
    fdp = FdpClient(config.fdp_url, config.fdp_catalog_id, config.fdp_token, config.request_timeout)

    own_datasets = {
        d.get("datasetId"): d
        for d in passport.datasets()
        if d.get("organizationId") == config.organization_id
    }

    # A CatalogueDataset record is itself the publication decision: the steward creates one only for a
    # dataset their organization intends to publish.
    published = 0
    for publication in passport.catalogue_datasets():
        dataset_id = publication.get("datasetId")
        if dataset_id not in own_datasets:
            continue
        catalogue_dataset_id = publication.get("catalogueDatasetId")
        already = [
            r
            for r in passport.catalogue_registrations(catalogue_dataset_id)
            if r.get("catalogueType") == CATALOGUE_TYPE
        ]

        dataset = own_datasets[dataset_id]
        turtle = to_healthdcat_ap(
            dataset,
            publication,
            passport.dataset_concepts(dataset_id),
            passport.get("/dataset-distribution", {"catalogueDatasetId": catalogue_dataset_id}) or [],
        )
        location = fdp.publish(turtle)

        passport.post(
            "/catalogue-registration",
            {
                "catalogueDatasetId": catalogue_dataset_id,
                "catalogueType": CATALOGUE_TYPE,
                # where it actually came to rest, which subsumes the FDP base url
                "catalogueUri": location,
                "listingDate": _now_iso(),
                # A listing that already exists is being refreshed, not created anew.
                "changeType": "updated" if already else "created",
            },
        )
        published += 1
        logger.info("+ Published %s to %s", dataset.get("title"), location)

    return {"published": published}
