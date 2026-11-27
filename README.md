# AI4HF Passport Node Agent

The node side of the federated AI Product Passport. One deployable runs at each participating
hospital and does the three things the site owes the central Passport — and nothing more.

Everything here pushes **outward**: the node reads its own systems and writes to the central
Passport. The Passport never calls in, there is no central trigger and no queue, and no patient-level
or per-site result ever leaves the hospital.

| Module | Runs when | Reads | Writes |
|---|---|---|---|
| `dataset-sync` | after a local `$extract` | the Studyfyr dataset descriptor, and `$quality` results | Population, Featureset, Feature, Dataset (+characteristics, concepts), QualityCriteria(+Criterion), QualityAssessment(+Results) |
| `metadata-publisher` | after a Data Steward approves publication | the Passport, for this organization's datasets | the site's own FAIR Data Point (HealthDCAT-AP RDF), then `CatalogueRegistration` back to the Passport |
| `monitoring-import` | at retraining only | the site's own Elasticsearch | `ModelEvaluation` (`trigger='monitoring'`, this organization) + its measures |

## Running it

```bash
cp .env.example .env
```

Then bring up whichever module's moment has arrived — none of them is a daemon:

```bash
docker compose run --rm dataset-sync
```

```bash
docker compose run --rm monitoring-import --model-id <replaced-model-id> --period-start 2026-01-01 --period-end 2026-03-31
```

Without Docker:

```bash
pip install -r requirements.txt && python -m agent dataset-sync
```

## Three identities, not one

Each module authenticates as its **own** Keycloak service account with the `client_credentials`
grant. There is no offline token to store and no user to impersonate. The token's `azp` claim names
the client, which the Passport resolves to a `SoftwareAgent`, so the audit log book distinguishes
"the dataset sync wrote this" from "the monitoring import wrote that".

A service account joins a study group exactly as a person does, which is what makes the Passport's
per-study authorization work for a machine with no special case. Each account needs the role its
writes require:

| Module | Role |
|---|---|
| `dataset-sync` | `DATA_ENGINEER` |
| `metadata-publisher` | `DATA_STEWARD` |
| `monitoring-import` | `DATA_SCIENTIST` |

## Re-running is safe

Every write is lookup-then-create, keyed on what identifies the record in the domain:

- a definition by `(url, version)`,
- a dataset by the extraction that produced it (the Studyfyr job id in `wasGeneratedBy`, whatever
  address `FEAST_URL` reaches Studyfyr at),
- a quality assessment by `(dataset, criteria definition)` — `$quality` runs once; results missing
  from an existing assessment are added to it (evaluating again only then),
- an evaluation by `(model, organization, period)`, whatever `--reason` says and however the period
  is typed (`2026-03-31` and `2026-03-31T23:59:59Z` are the same end; a bound is a `YYYY-MM-DD` date
  or a full ISO-8601 timestamp, and `--period-start`/`--period-end` are given both or neither);
  measures missing from an existing evaluation are added to it.

So a run interrupted half way can simply be run again: it completes the work rather than creating a
second copy of it. The prototype connectors this replaces duplicated everything on every run, which
is why they could not be scheduled.

The exception is `metadata-publisher`: every run publishes every approved dataset again, as a new
FDP entry with a new `CatalogueRegistration` ("updated" once the dataset was listed before). A dataset
the FDP rejects stops the run (code 1). A failed FDP login (`FDP_EMAIL`/`FDP_PASSWORD`, or `FDP_TOKEN`)
or an FDP that cannot be reached (`FDP_URL`) stops the run with code 3, as a failed Passport login does.

## Definition versions

Studyfyr identifies a definition the way FHIR does, packing the version into the url:

```
https://ai4hf.eu/cohorts/maggic|1.3
```

The Passport keeps the two apart, because `(url, version)` is the identity the whole sync is keyed
on. The agent splits on `|`; a definition that declares no version is recorded with a null version
rather than an invented one.

## What it does not do

- **No extraction.** `$extract` is the site's own operation on its own schedule. The agent reads the
  result afterwards.
- **No per-site monitoring upload.** Monitoring data stays in the hospital. Only at a retraining is
  the replaced model's deployment period summarised and sent, so the new model's passport can
  document why it was retrained.
- **No central catalogue.** Publication is to the site's *own* FDP. Whether a central catalogue
  harvests those FDPs or is pushed to is still undecided.

## Related

- [`../passport`](../passport) — the central Passport API this writes to
- [`../onfhir-feast`](../onfhir-feast) — Studyfyr, the extraction engine this reads
- [`../feature-extraction-suite`](../feature-extraction-suite) — the definitions Studyfyr executes
- [`../design/integration-design.md`](../design/integration-design.md) — the decisions behind all of this
