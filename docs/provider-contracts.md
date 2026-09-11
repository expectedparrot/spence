# Humanize adapter contracts and release gates

The adapter is implemented against EDSL GitHub `main`, inspected and installed at commit `476cedd7ac958d042118b959490a332faa12cab1` (version `1.0.8.dev1`). The package dependency intentionally tracks `main`; this commit records validation provenance, not a dependency pin.

The local demo and client-signature tests pass without network calls. **No real hosted application, reviewer invitation, file round trip, or access-control acceptance test was performed during this build.** The alpha is not yet an operational hiring release.

## Implemented client integration

| Contract | Implementation | Validation |
| --- | --- | --- |
| Public application | `Coop.create_human_survey`, one scenario, no applicant AgentList or models | Actual client signature and local human Jobs `.ep` round trip |
| Private candidate review | One candidate/version scenario, named reviewer AgentList, private Survey/ScenarioList/AgentList objects | Snapshot isolation and explicit roster checks |
| Personal links | Disable anonymity/resubmission; strict respondent links joined by reviewer ID | Reject missing/duplicate respondent mapping; no matching by name/order |
| Invitations | Delivery creation named with durable Spence operation ID | Duplicate-send and uncertain-outcome tests; no live send |
| Delivery status | Delivery record plus paginated task list | Mocked task pagination and separate delivery/completion state |
| Completed responses | Raw human-survey response endpoint, with underlying Survey UUID check | Source-envelope fixtures, actual client surface, malformed-row quarantine |
| Response validation | Saved Survey and humanize requiredness along answer-based branches | Required/optional, invalid answers, branching, file-reference shape tests |

The raw response adapter calls the client's private `_send_server_request` and `_resolve_server_response` methods because Results reconstruction can drop source metadata. Keep that dependency confined to `provider.py`. It must be reviewed when EDSL changes. Private object visibility does not itself prove that a hosted respondent link is private or that files have the intended authorization.

## Outstanding tasks

These are local task identifiers, not claims that GitHub issues were filed.

| Task | Required result before enabling the capability |
| --- | --- |
| EP-ATS-01: hosted acceptance | One fictitious applicant and two controlled reviewer accounts complete the hosted flow; verify mobile rendering, public job context, receipt, independent packets, stable respondent identity, duplicate behavior, and unauthorized-link behavior. Explicit operator authorization is required before sending test invitations. |
| EP-ATS-02: intake closure | A documented server operation closes new/in-flight intake; an already distributed Apply link must stop accepting responses according to the selected policy. Confirm late-completion timestamps. |
| EP-ATS-03: respondent revocation | A documented operation invalidates an existing personal link and candidate material access, including previously issued file links. |
| EP-ATS-04: file lifecycle | Verify actual upload response shape, size/type constraints, credential and expiry behavior, download authorization, scanning responsibility, and erasure. Spence currently records references only and blocks file fields in reviewer packets. |
| EP-ATS-05: erasure | Document and test deletion of provider responses, uploaded documents, surveys, and derived reviewer packets. Then implement local purging/tombstones and retention worklists without leaving content in immutable versions/events. |
| EP-ATS-06: durable identity | Prefer a public raw-response envelope with stable response/respondent IDs, revision ordering, completion timestamps, pagination semantics, and provider idempotency keys for publication/delivery. |

Spence's pending closure/revocation/deletion states are deliberate: they describe what has actually been confirmed. The demo implements local closure/revocation behavior solely to exercise the workflow; it does not establish hosted capability.

After the hosted gates, the next product work is an employer dashboard with opening-scoped permissions, richer candidate comparison, configurable pipelines, attachment viewing, and calendar integration. Those are outside the CLI alpha.
