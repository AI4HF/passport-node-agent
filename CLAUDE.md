# AI4HF Passport Node Agent — repo guide

Python 3.11, `requests` only. One deployable, three modules, one configuration per hospital.
See [`README.md`](README.md) for what it does; this file is about changing it.

Workspace-level and cross-repo concerns are in [`../CLAUDE.md`](../CLAUDE.md). The design decisions
this implements (D2–D6, D9, D10) are in [`../design/integration-design.md`](../design/integration-design.md).

## Layout

```
agent/
  __main__.py     # the CLI: python -m agent <module>
  config.py       # env -> AgentConfig; one service account per module
  auth.py         # client_credentials token provider (D3)
  passport.py     # the Passport REST client, and find_or_create - where idempotency lives
  feast.py        # Studyfyr reader + the definitions checkout
  canonical.py    # url|version splitting
  modules/
    dataset_sync.py         # integrations #1 and #2
    metadata_publisher.py   # integration #5
    monitoring_import.py    # integration #4
```

## The five things to know

1. **Everything is outbound.** The node reads its own systems and writes to the central Passport.
   Nothing accepts an inbound call. If a change needs the centre to call the node, it is the wrong
   shape — re-read D4.

2. **Idempotency is not optional.** Every write is lookup-then-create on a domain identity — through
   `PassportClient.find_or_create` with a predicate where the listing is enough, by hand where it is
   not: a dataset by its Studyfyr job (read from the full records), an assessment by the criteria id
   its summary names, a monitoring run by the period ending its description. Children of an existing
   record (results, measures) are completed,
   not skipped, so an interrupted run finishes on the next one. Adding a write means deciding what
   identifies that record when the agent runs twice. Getting this wrong is invisible until the run is
   scheduled and the Passport fills with duplicates — the exact failure of the prototypes this replaces.
   The exception is `metadata-publisher`, which publishes every approved dataset again on every run (a
   new FDP entry and a new CatalogueRegistration each time).

3. **Three identities.** Each module holds its own service account, so the audit log attributes work
   to the module that did it. Adding a module means a fourth Keycloak client, a fourth
   `SoftwareAgent` row in the Passport's `init-db.sql`, and the study-group membership that carries
   the role its writes need.

4. **Studyfyr serialises instants as epoch milliseconds.** Not ISO strings. `_as_instant` in
   `dataset_sync.py` handles both; do not stringify-and-slice a timestamp.

5. **Definition versions live inside the url.** `url|version`, FHIR-canonical. Always go through
   `canonical.split_canonical` rather than reading `url` directly.

## What Studyfyr does and does not serve

- `GET /Dataset/{id}` — the descriptor. What `dataset-sync` reads.
- `POST /Dataset/{id}/DatasetQualityCriteria/{criteriaId}/$quality` — **runs** an evaluation and
  returns the report. There is no endpoint for a stored report.
- `/definition/{Type}` — Population, FeatureGroup, FeatureSet, and the two pipeline types.
  **DatasetQualityCriteria is not served**, which is why the criteria rules are read from the
  definitions checkout (`DEFINITIONS_DIR`; under compose it is fixed at `/opt/definitions`, the host
  path is `DEFINITIONS_HOST_DIR`) instead. The checkout is optional: an assessment's summary names the
  criteria id, which is what a re-run recognises it by.

## Testing

There are no automated tests, matching the rest of the workspace. `canonical.py` carries doctests:

```bash
python -m doctest agent/canonical.py -v
python -c "import doctest; from agent.modules import metadata_publisher as a, monitoring_import as b, dataset_sync as c; [doctest.testmod(m) for m in (a, b, c)]"
```

Verify a change by running the module against a live Passport. A Studyfyr instance is heavy to stand
up; serving `onfhir-feast/complications-metadata.json` over a stub HTTP server at `/Dataset/{id}`
exercises the whole path — fetch, canonical split, idempotent write — without Spark and a FHIR repo.
Run the module twice: the second run must create nothing.

## Open

- **The retraining-event contract.** Nothing yet tells a node that a retraining has begun, so
  `monitoring-import` is invoked explicitly with `--model-id` and a period. When the contract is
  settled, that becomes the trigger. `--period-start`/`--period-end` are given both or neither
  (neither: the whole history), each a `YYYY-MM-DD` date or a full ISO-8601 timestamp; anything else
  (e.g. `20260331`) is a configuration error. A date-only bound covers the whole day (`period_bound`);
  the period always ends up in the run's description, which is what makes a re-run for the same period
  a no-op.
- **Who produces deployment-period measures.** `monitoring-import` averages `evaluation_measure`
  events in `evaluation-measures-*` matched on `model_id.keyword` (dynamic mapping makes `model_id`
  text) within the period, and skips any that carry `model_evaluation_id` — those are the Passport's
  own training/per-site runs, copied in by the passport-monitoring-platform-connector. Nothing in the
  platform emits the deployment-time ones yet (performance from prediction outcomes); the local
  simulation fakes them.
- **The FDP's write API** has been exercised against fairdata/fairdatapoint 1.17 in the local
  simulation. What it takes: a token from `POST /tokens`; the parent catalog as `dct:isPartOf` in the
  body (no header for it); `PUT {entry}/meta/state` `{"current":"PUBLISHED"}` after a create (new entries
  are drafts) - for a dataset entry only last, after its distributions are in: the FDP accepts
  distributions under a draft dataset and lists them once it is published; the Resource shape (title, version, a `foaf:Agent` publisher with `foaf:name`), the
  Dataset shape (at least one `dcat:theme` IRI; `dcat:contactPoint` an IRI, hence `mailto:`); and
  distributions as child entries (`POST /distribution`, `dct:isPartOf` the dataset; an inline
  `dcat:distribution` is dropped). The FDP mints IRIs under its persistent url, which `FdpClient` maps
  onto `FDP_URL` for requests. Every IRI in the Turtle goes through `safe_iri` (absolute, nothing
  IRIREF forbids) and dates through `iso_date`; a bad value is dropped with a warning. Checked live on
  FDP 1.17: the FDP accepts distributions under a draft dataset (a draft answers 403 anonymously).
  The first failing dataset stops the run (an FDP rejection exits 1). A failed FDP login (bad
  `FDP_EMAIL`/`FDP_PASSWORD`, no token in the answer, a rejected `FDP_TOKEN`) raises
  `AuthenticationError`, and an FDP that cannot be reached (connection error or timeout) raises its
  subclass `FdpUnreachableError`; both stop the run with exit 3, like a failed Passport login.
- All three modules have been run end to end in the local simulation (`../ai4hf-local-sim`): against
  a live Passport, Studyfyr, Monitoring Platform and FAIR Data Point.
