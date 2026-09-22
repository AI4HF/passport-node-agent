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

2. **Idempotency is not optional.** Every write goes through `PassportClient.find_or_create` with a
   domain predicate. Adding a write means deciding what identifies that record when the agent runs
   twice. Getting this wrong is invisible until the run is scheduled and the Passport fills with
   duplicates — the exact failure of the prototypes this replaces.

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
  definitions checkout (`DEFINITIONS_DIR`) instead.

## Testing

There are no automated tests, matching the rest of the workspace. `canonical.py` carries doctests:

```bash
python -m doctest agent/canonical.py -v
```

Verify a change by running the module against a live Passport. A Studyfyr instance is heavy to stand
up; serving `onfhir-feast/complications-metadata.json` over a stub HTTP server at `/Dataset/{id}`
exercises the whole path — fetch, canonical split, idempotent write — without Spark and a FHIR repo.
Run the module twice: the second run must create nothing.

## Open

- **The retraining-event contract.** Nothing yet tells a node that a retraining has begun, so
  `monitoring-import` is invoked explicitly with `--model-id` and a period. When the contract is
  settled, that becomes the trigger.
- **The FDP's write API.** `FdpClient.publish` posts Turtle to `{FDP_URL}/dataset` and reads the
  `Location` header. This has not been exercised against a real FAIR Data Point.
- **`metadata-publisher` and `monitoring-import` have not been run end to end** — only
  `dataset-sync` has, against a live Passport.
