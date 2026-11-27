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

Every run publishes every such dataset again, as a new FDP entry with a new CatalogueRegistration
(``changeType`` "updated" when the dataset was listed before). It is not idempotent: a run creates a
new entry in the FDP whether or not the dataset changed.
"""

import logging
import re
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from ..auth import AuthenticationError
from ..config import AgentConfig
from ..passport import PassportClient

logger = logging.getLogger(__name__)

HEALTH_DCAT_AP = "http://healthdataportal.eu/ns/health#"

ACCESS_RIGHTS = "http://publications.europa.eu/resource/authority/access-right/"

CATALOGUE_TYPE = "FDP"

# Turtle's IRIREF forbids these unescaped: <>"{}|^`\ and everything up to and including the space. DEL
# and the C1 controls are not forbidden by the grammar but are never meant.
_IRI_FORBIDDEN = re.compile(r'[<>"{}|^`\\\x00-\x20\x7f-\x9f]')
_IRI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:.")


class FdpError(RuntimeError):
    """Raised when the local FAIR Data Point rejects a publication."""


class FdpUnreachableError(AuthenticationError):
    """Raised when the FAIR Data Point cannot be reached at all; like a failed login it stops the run (exit 3)."""


def safe_iri(value: Optional[Any], label: str = "", field: str = "") -> Optional[str]:
    """
    The value as an IRI that can be written between ``<`` and ``>`` in Turtle, or None.

    Steward and Passport text ends up in IRIs, so it is accepted only as an absolute IRI (with a scheme)
    without any character Turtle forbids there. Spaces inside an http(s) url are percent-encoded, which
    is what a browser does with them; anything else is left out of the description, with a warning.

    >>> safe_iri("https://example.org/a b")
    'https://example.org/a%20b'
    >>> safe_iri("mailto:a>b@c.org") is None
    True
    >>> safe_iri("relative/path") is None
    True

    :param value: the candidate IRI
    :param label: the dataset it belongs to, for the warning
    :param field: the field it came from, for the warning
    :return: the IRI, or None when it is empty or not a valid absolute IRI
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.lower().startswith(("http://", "https://")):
        text = text.replace(" ", "%20")
    if _IRI_SCHEME.match(text) and not _IRI_FORBIDDEN.search(text):
        return text
    logger.warning("! %s: %s %r is not a valid absolute IRI; left out of the description", label, field, value)
    return None


def iso_date(value: Optional[Any], label: str = "", field: str = "") -> Optional[str]:
    """
    The value as an ``xsd:date`` lexical form (YYYY-MM-DD), or None.

    A timestamp is reduced to its date; anything else is left out of the description, with a warning.

    :param value: a date or ISO-8601 timestamp
    :param label: the dataset it belongs to, for the warning
    :param field: the field it came from, for the warning
    :return: the date, or None
    """
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return date.fromisoformat(text).isoformat()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ][0-9:.+\-Z]+", text):
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    logger.warning("! %s: %s %r is not an ISO date; left out of the description", label, field, value)
    return None


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


def _contact_iri(value: Optional[str]) -> Optional[str]:
    """
    The contact point as an IRI, which DCAT and the FDP's Dataset shape require: an e-mail address as
    typed by the steward becomes a mailto: IRI.
    """
    if not value:
        return None
    value = value.strip()
    if value.startswith(("http://", "https://", "mailto:", "tel:")):
        return value
    if "@" in value and " " not in value:
        return f"mailto:{value}"
    return None


def to_healthdcat_ap(
    dataset: Dict[str, Any],
    publication: Dict[str, Any],
    concepts: List[Dict[str, Any]],
    distributions: List[Dict[str, Any]],
    parent: Optional[str] = None,
    label: Optional[str] = None,
) -> str:
    """
    Render a dataset's publication metadata as HealthDCAT-AP in Turtle.

    Only the fields the steward approved for publication are rendered; the Passport's internal ids and
    the dataset's contents are not part of the description. Every IRI goes through :func:`safe_iri` and every date through
    :func:`iso_date`, so no field can break out of its term.

    :param dataset: the Passport dataset
    :param publication: its CatalogueDataset publication record
    :param concepts: its controlled-vocabulary values
    :param distributions: how the published dataset can be reached
    :param parent: the IRI of the FDP catalog the dataset is listed in (dct:isPartOf), which the FDP requires
    :param label: how the dataset is named in warnings
    :return: a Turtle document
    """
    label = label or str(dataset.get("title") or dataset.get("datasetId") or "dataset")
    lines = [
        "@prefix dcat: <http://www.w3.org/ns/dcat#> .",
        "@prefix dct: <http://purl.org/dc/terms/> .",
        "@prefix foaf: <http://xmlns.com/foaf/0.1/> .",
        "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .",
        "@prefix dcatap: <http://data.europa.eu/r5r/> .",
        "@prefix dqv: <http://www.w3.org/ns/dqv#> .",
        "@prefix oa: <http://www.w3.org/ns/oa#> .",
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

    def iri(predicate: str, value: Optional[str], field: str) -> None:
        checked = safe_iri(value, label, field)
        if checked:
            lines.append(f"    {predicate} <{checked}> ;")

    def agent(predicate: str, name: Optional[str], homepage: Optional[str] = None,
              kind: Optional[str] = None, field: str = "") -> None:
        # a foaf:Agent node: the FDP's Resource shape requires foaf:name on the publisher
        homepage = safe_iri(homepage, label, f"{field} homepage")
        if not name and not homepage:
            return
        inner = ["a foaf:Agent"]
        if name:
            inner.append(f'foaf:name "{_escape(name)}"')
        if homepage:
            inner.append(f"foaf:homepage <{homepage}>")
        if kind and kind.strip().startswith(("http://", "https://")):
            kind = safe_iri(kind, label, f"{field} type")
            if kind:
                inner.append(f"dct:type <{kind}>")
        lines.append(f"    {predicate} [ {' ; '.join(inner)} ] ;")

    iri("dct:isPartOf", parent, "catalog")
    literal("dct:title", publication.get("publicTitle") or dataset.get("title"))
    literal("dct:description", publication.get("publicDescription") or dataset.get("description"))
    literal("dct:identifier", dataset.get("persistentIdentifier"))
    literal("dcat:version", dataset.get("version"))

    # the EU access-right vocabulary when the steward typed one of its terms
    access_rights = (publication.get("accessRights") or "").strip()
    if access_rights.lower() in ("public", "restricted", "non_public", "non-public"):
        iri("dct:accessRights", ACCESS_RIGHTS + access_rights.upper().replace("-", "_"), "accessRights")
    elif access_rights.startswith(("http://", "https://")):
        iri("dct:accessRights", access_rights, "accessRights")
    else:
        literal("dct:accessRights", access_rights)

    literal("dct:provenance", dataset.get("provenanceStatement"))
    literal("dct:conformsTo", dataset.get("conformsTo"))
    literal("dct:spatial", dataset.get("geographicalCoverage"))
    literal("dcat:temporalResolution", dataset.get("temporalResolution"))
    iri("dcat:landingPage", publication.get("landingPage"), "landingPage")
    iri("dct:isReferencedBy", publication.get("documentation"), "documentation")
    agent("dct:publisher", publication.get("publisherName"), kind=publication.get("publisherType"),
          field="publisher")
    iri("dcat:contactPoint", _contact_iri(publication.get("contactPoint")), "contactPoint")
    if publication.get("qualityAnnotation"):
        lines.append(
            "    dqv:hasQualityAnnotation [ a dqv:QualityAnnotation ; "
            f'oa:bodyValue "{_escape(publication["qualityAnnotation"])}" ] ;'
        )

    # HealthDCAT-AP specifics: who may release it, on what legal basis, and what it covers.
    agent("health:hdab", publication.get("hdabName"), homepage=publication.get("hdabUri"), field="hdab")
    literal("health:legalBasis", publication.get("legalBasis"))
    literal("health:purpose", publication.get("purpose"))
    iri("dcatap:applicableLegislation", publication.get("applicableLegislation"), "applicableLegislation")
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

    literal(
        "health:retentionPeriodStart",
        iso_date(publication.get("retentionPeriodStart"), label, "retentionPeriodStart"),
        "xsd:date",
    )
    literal(
        "health:retentionPeriodEnd",
        iso_date(publication.get("retentionPeriodEnd"), label, "retentionPeriodEnd"),
        "xsd:date",
    )

    # iso_date only lets YYYY-MM-DD through, so the dates need no further escaping
    start = iso_date(dataset.get("temporalCoverageStart"), label, "temporalCoverageStart")
    end = iso_date(dataset.get("temporalCoverageEnd"), label, "temporalCoverageEnd")
    if start or end:
        parts = []
        if start:
            parts.append(f'dcat:startDate "{start}"^^xsd:date')
        if end:
            parts.append(f'dcat:endDate "{end}"^^xsd:date')
        lines.append(f"    dct:temporal [ a dct:PeriodOfTime ; {' ; '.join(parts)} ] ;")

    for concept in concepts:
        iri("dcat:theme", concept.get("conceptUri"), "conceptUri")

    # Distributions are not inlined: the FDP manages them as child resources of the dataset and drops an
    # inline dcat:distribution link (see to_distribution). Without an FDP parent they are inlined.
    if not parent:
        for distribution in distributions:
            lines.append(f"    dcat:distribution [ {_distribution_properties(distribution, label)} ] ;")

    # close the description
    lines[-1] = lines[-1].rstrip(" ;") + " ."
    return "\n".join(lines) + "\n"


def _distribution_properties(distribution: Dict[str, Any], label: str = "") -> str:
    """The DCAT properties of a distribution, as Turtle predicate-object pairs."""
    inner = ["a dcat:Distribution"]
    if distribution.get("title"):
        inner.append(f'dct:title "{_escape(str(distribution["title"]))}"')
    if distribution.get("description"):
        inner.append(f'dct:description "{_escape(str(distribution["description"]))}"')
    access_url = safe_iri(distribution.get("accessUrl"), label, "distribution accessUrl")
    if access_url:
        inner.append(f"dcat:accessURL <{access_url}>")
    download_url = safe_iri(distribution.get("downloadUrl"), label, "distribution downloadUrl")
    if download_url:
        inner.append(f"dcat:downloadURL <{download_url}>")
    if distribution.get("mediaType"):
        inner.append(f'dcat:mediaType "{_escape(str(distribution["mediaType"]))}"')
    if distribution.get("format"):
        inner.append(f'dcat:format "{_escape(str(distribution["format"]))}"')
    if distribution.get("byteSize") is not None:
        inner.append(f'dcat:byteSize "{_escape(str(distribution["byteSize"]))}"^^xsd:nonNegativeInteger')
    licence = str(distribution.get("licence") or "").strip()
    if licence.startswith(("http://", "https://")):
        licence_iri = safe_iri(licence, label, "distribution licence")
        if licence_iri:
            inner.append(f"dct:license <{licence_iri}>")
    elif licence:
        inner.append(f'dct:license "{_escape(licence)}"')
    return " ; ".join(inner)


def to_distribution(
    distribution: Dict[str, Any], dataset: Dict[str, Any], publication: Dict[str, Any], parent: str,
    label: str = "",
) -> str:
    """
    Render a distribution as an FDP Distribution entry, a child of the dataset's entry.

    The FDP's shapes require what every FDP resource has (title, version, a publisher with a name) and a
    media type, so the distribution carries the dataset's version and publisher.

    :param distribution: the Passport distribution
    :param dataset: the Passport dataset it belongs to
    :param publication: the dataset's publication record (publisher)
    :param parent: the IRI of the dataset's FDP entry
    :param label: how the dataset is named in warnings
    :return: a Turtle document
    """
    checked_parent = safe_iri(parent, label, "parent entry")
    if not checked_parent:
        raise FdpError(f"Not a valid parent entry IRI: {parent!r}")
    lines = [
        "@prefix dcat: <http://www.w3.org/ns/dcat#> .",
        "@prefix dct: <http://purl.org/dc/terms/> .",
        "@prefix foaf: <http://xmlns.com/foaf/0.1/> .",
        "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .",
        "",
        f"<> {_distribution_properties(distribution, label)} ;",
        f"    dct:isPartOf <{checked_parent}> ;",
    ]
    if not distribution.get("title"):
        lines.append(f'    dct:title "{_escape(publication.get("publicTitle") or dataset.get("title") or "Distribution")}" ;')
    if dataset.get("version"):
        lines.append(f'    dcat:version "{_escape(str(dataset["version"]))}" ;')
    if publication.get("publisherName"):
        lines.append(f'    dct:publisher [ a foaf:Agent ; foaf:name "{_escape(publication["publisherName"])}" ] ;')
    lines[-1] = lines[-1].rstrip(" ;") + " ."
    return "\n".join(lines) + "\n"


class FdpClient:
    """
    Lists dataset descriptions in the site's FAIR Data Point (FDP 1.x API).

    A new entry is created under the configured catalog - named by ``dct:isPartOf`` in the body, the
    only way the FDP takes a parent - and then published: the FDP keeps new metadata as a draft that
    nobody else can see. A dataset entry is published only after its distributions are in (the FDP
    accepts distributions under a draft dataset). The FDP mints IRIs under its persistent url, which
    need not be the url the agent reaches it at, so IRIs are mapped onto ``base_url`` for requests.

    :param base_url: the url the agent reaches the FDP at
    :param catalog_id: the catalog new datasets are filed under (its uuid or IRI)
    :param token: a bearer token, when no credentials are given
    :param timeout: per-request timeout in seconds
    :param email: the FDP account of this node, to obtain tokens with (they expire)
    :param password: its password
    """

    def __init__(
        self,
        base_url: str,
        catalog_id: Optional[str] = None,
        token: Optional[str] = None,
        timeout: int = 60,
        email: Optional[str] = None,
        password: Optional[str] = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._catalog_id = catalog_id
        self._token = token
        self._timeout = timeout
        self._email = email
        self._password = password
        self._persistent_url: Optional[str] = None

    def _unreachable(self, method: str, url: str, error: Exception) -> "FdpUnreachableError":
        return FdpUnreachableError(f"FDP unreachable: {method} {url}: {type(error).__name__}: {error}; check FDP_URL")

    def _login(self) -> None:
        url = f"{self._base_url}/tokens"
        try:
            response = requests.post(
                url, json={"email": self._email, "password": self._password}, timeout=self._timeout
            )
        except (requests.ConnectionError, requests.Timeout) as error:
            raise self._unreachable("POST", url, error) from error
        # a failed login fails every dataset alike, so it stops the run (AuthenticationError)
        if not response.ok:
            raise AuthenticationError(
                f"FDP login failed: POST {url} -> {response.status_code} {response.text[:300]}; "
                "check FDP_EMAIL/FDP_PASSWORD"
            )
        try:
            token = response.json().get("token")
        except (ValueError, AttributeError):
            token = None
        if not token:
            raise AuthenticationError(
                f"FDP login failed: POST {url} answered {response.status_code} without a token; "
                "check FDP_EMAIL/FDP_PASSWORD"
            )
        self._token = token

    def _request(self, method: str, url: str, headers: Optional[Dict[str, str]] = None,
                 **kwargs: Any) -> requests.Response:
        if not self._token and self._email:
            self._login()

        def send() -> requests.Response:
            sent = dict(headers or {})
            if self._token:
                sent["Authorization"] = f"Bearer {self._token}"
            try:
                return requests.request(method, url, headers=sent, timeout=self._timeout, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as error:
                # an FDP that cannot be reached fails every dataset alike, so it stops the run
                raise self._unreachable(method, url, error) from error

        response = send()
        if response.status_code == 401 and self._email:
            # the token expired: log in again, once (a 403 is a missing permission, which a new token
            # of the same account does not fix)
            self._login()
            response = send()
        if response.status_code == 401:
            # a rejected FDP_TOKEN, or a fresh login's token rejected: no dataset would get further
            credentials = "FDP_EMAIL/FDP_PASSWORD" if self._email else "FDP_TOKEN"
            raise AuthenticationError(
                f"FDP login failed: {method} {url} -> 401 {response.text[:300]}; check {credentials}"
            )
        if not response.ok:
            raise FdpError(f"{method} {url} -> {response.status_code} {response.text[:300]}")
        return response

    @property
    def persistent_url(self) -> str:
        """The url the FDP mints its IRIs under, as its root metadata states."""
        if self._persistent_url is None:
            response = self._request("GET", f"{self._base_url}/", headers={"Accept": "application/ld+json"})
            nodes = response.json()
            if isinstance(nodes, dict):
                nodes = [nodes]
            # The FDP's JSON-LD leaves the root's @type out (it sits in a nested @graph); the root is the
            # node that states the FDP software version
            root = next(
                (n for n in nodes if any(k.endswith("#fdpSoftwareVersion") for k in n)),
                None,
            )
            self._persistent_url = ((root or {}).get("@id") or self._base_url).rstrip("/")
        return self._persistent_url

    @property
    def catalog_iri(self) -> Optional[str]:
        """The IRI of the catalog new entries are filed under."""
        if not self._catalog_id:
            return None
        if self._catalog_id.startswith(("http://", "https://")):
            return self._catalog_id
        return f"{self.persistent_url}/catalog/{self._catalog_id}"

    def _local(self, iri: str) -> str:
        """The url the agent reaches an FDP IRI at."""
        if iri.startswith(self.persistent_url):
            return self._base_url + iri[len(self.persistent_url):]
        return iri

    def create(self, turtle: str, kind: str = "dataset") -> str:
        """
        Create an entry, which the FDP keeps as a draft, and return its IRI.

        :param turtle: the description, with its parent as ``dct:isPartOf``
        :param kind: the FDP resource type: ``dataset`` or ``distribution``
        :return: the draft entry's IRI
        :raises FdpError: when the FDP rejects it
        """
        response = self._request(
            "POST",
            f"{self._base_url}/{kind}",
            headers={"Content-Type": "text/turtle"},
            data=turtle.encode("utf8"),
        )
        location = safe_iri(response.headers.get("Location"), kind, "Location")
        if not location:
            raise FdpError(f"The FDP created the {kind} but returned no valid Location")
        return location

    def publish(self, turtle: str, kind: str = "dataset") -> str:
        """
        Create an entry, publish it and return its IRI.

        :param turtle: the description, with its parent as ``dct:isPartOf``
        :param kind: the FDP resource type: ``dataset`` or ``distribution``
        :return: the published entry's IRI
        :raises FdpError: when the FDP rejects it
        """
        location = self.create(turtle, kind)
        self.ensure_published(location, known_draft=True)
        return location

    def ensure_published(self, iri: str, known_draft: bool = False) -> None:
        """
        Publish an entry that is still a draft (a create interrupted before its state was set leaves one).

        :param iri: the entry's IRI
        :param known_draft: skip reading the state of an entry just created
        """
        if not known_draft:
            meta = self._request("GET", f"{self._local(iri)}/meta", headers={"Accept": "application/json"})
            try:
                state = ((meta.json() or {}).get("state") or {}).get("current")
            except (ValueError, AttributeError):
                state = None
            if state == "PUBLISHED":
                return
        self._request(
            "PUT",
            f"{self._local(iri)}/meta/state",
            headers={"Content-Type": "application/json"},
            json={"current": "PUBLISHED"},
        )


def run(config: AgentConfig, passport: PassportClient) -> Dict[str, Any]:
    """
    Publish every approved dataset of this organization to the site's FDP.

    The first dataset that fails (the FDP rejects it, a Passport call fails) stops the run.

    :param config: the module's configuration
    :param passport: the Passport client, authenticated as the metadata-publisher service account
    :return: a summary of what was published
    """
    fdp = FdpClient(
        config.fdp_url,
        config.fdp_catalog_id,
        config.fdp_token,
        config.request_timeout,
        config.fdp_email,
        config.fdp_password,
    )

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

        # The study listing is a projection on older Passports: render from the full record
        dataset = passport.dataset(dataset_id) or own_datasets[dataset_id]
        label = f"{publication.get('publicTitle') or dataset.get('title')} (dataset {dataset_id})"
        distributions = passport.get("/dataset-distribution", {"catalogueDatasetId": catalogue_dataset_id}) or []

        # Published last: the entry stays a draft until its distributions are in, so nobody else sees it
        # half listed
        location = fdp.create(
            to_healthdcat_ap(
                dataset, publication, passport.dataset_concepts(dataset_id), distributions, fdp.catalog_iri,
                label=label,
            )
        )
        for distribution in distributions:
            fdp.publish(to_distribution(distribution, dataset, publication, location, label), kind="distribution")
        fdp.ensure_published(location, known_draft=True)

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
        logger.info("+ Published %s to %s", label, location)

    return {"published": published}
