# Operating a Spence workspace

## Synchronization and provenance

`applications sync --opening ID` and `reviews sync --opening ID` read completed responses for all known publications of that type. They deliberately perform full reads: the upstream started-time filters are not a safe completion cursor. The adapter validates the remote Survey, scenario, and reviewer roster before importing a live response set.

Response identity is `(configured account, human survey UUID, response UUID)`. A payload hash deduplicates each source revision. Email matches flag possible duplicates without merging people. Candidate identity is self-reported and is recorded as unverified.

An application response revision creates a new immutable submission snapshot and preserves the application's stage. A review records the candidate version and rubric it actually assessed. Reports flag feedback on earlier application versions. Source timestamps remain unknown when the provider/export omits them; Spence does not invent submission times.

Export imports are deliberately explicit about their selected publication:

```sh
spence applications ingest responses.json --publication PUB_ID
spence reviews ingest reviews.ep --publication BATCH_ID
spence quarantine list
spence quarantine show QUARANTINE_ID
spence quarantine retry QUARANTINE_ID --accept-revisions
```

Supported input formats are raw response-envelope JSON, EDSL Results JSON/`.ep`, and ScenarioList JSON. An export cannot prove its own origin: the operator must select the correct publication. Embedded mismatched survey IDs are rejected. Results reconstructed with EDSL's `Model("test")` are human transport records, not automatically simulations. ScenarioList fallbacks without respondent identity can supply application responses but cannot attribute reviewer feedback.

Changed export payloads require `--accept-revisions`. Live sync accepts revisions from the verified provider. Missing source identity, required answers, invalid contact information, or ambiguous reviewer attribution produce quarantine records; failed imports roll back entirely for that row. Quarantine contains private source material.

## Timeouts and retries

Publication and invitation commands write a durable operation before contacting the provider. If the call times out, the operation becomes `outcome_unknown` and blocks another attempt. A process killed mid-call leaves `pending`, which has the same retry protection.

```sh
spence operation list
spence operation show OP_ID
spence operation reconcile OP_ID --remote-id REMOTE_ID
```

For publication, `REMOTE_ID` is the human survey UUID; its form, scenario, and roster must match the prepared publication. For a send, it is the delivery UUID under the recorded human survey. The operator must verify that the delivery corresponds to this operation; the inspected delivery-detail client does not return the original operation name. Do not reconcile using an unrelated delivery.

If provider inspection confirms that **nothing was created**, record the evidence before retrying:

```sh
spence operation resolve-not-created OP_ID --reason 'Provider inspection confirmed no survey/delivery for this operation; reference ...'
```

This is an explicit operator assertion recorded in history. A timeout alone is not evidence of absence. There is no automatic retry of an uncertain write.

If publication succeeded but personal-link configuration failed, run `publication configure PUB_ID` after resolving the cause. It reuses the recorded remote survey. Published rosters are frozen: create a new batch to change recipients.

## Stages and decisions

Every `application move` requires a reason and records the evidence version. Use `--expected-revision N` when acting on a previously inspected record. Moving out of hired, rejected, or withdrawn requires `--reopen`.

```sh
spence application move APP_ID --to withdrawn --reason 'Candidate withdrew.'
spence application note APP_ID 'Internal scheduling note.'
```

These are local hiring records, not candidate notifications. Notes remain private and do not enter reviewer packets.

## Closure, revocation, and deletion

In demo mode, closing an opening prevents further demo application submissions and revocation blocks the demo respondent. With Coop, these hosted controls are not verified:

- `opening close ID` records a cutoff and remains `closing` if hosted closure cannot be confirmed. Existing links may still work. Imports after the cutoff are classified using source submission time; late or unknown-time responses are visibly marked `late_or_unknown`.
- `review revoke ASSIGNMENT_ID` remains `revocation_pending` if hosted access cannot be revoked. New feedback from that assignment is quarantined and further invitations for the batch are blocked. Existing provider access is not claimed removed.
- `application delete-request APP_ID --reason ...` records a worklist for the database, provider responses, derived review packets, backups, and exports. It blocks new processing and revised imports for that application. **It does not erase any copy.** Provider erasure, local purging, and retention automation remain outstanding.

Do not treat a local status change as proof that a distributed hosted link stopped working. See the named upstream tasks in [provider contracts](provider-contracts.md).

## Backups and recovery

```sh
spence backup --output /secure/location/ats-backup.sqlite3
```

The command uses SQLite's backup API and refuses to overwrite an existing file. It copies the database, not `.spence-private/artifacts/`, demo-provider state, the workspace config, or external exports. While Spence is stopped, copy those files and directories separately to protected backup storage. Preserve the same `spence.json` and account label with a restored database; the label is a local source namespace, not an authentication boundary. Do not change it casually or share one database across provider accounts.

Restore into a new protected directory while no Spence process is running, put the database at `.spence-private/ats.sqlite3`, restore the associated artifacts and config, and preserve owner-only permissions. This alpha validates schema version 1 and refuses unknown schema versions; there is no migration mechanism yet.

Spence refuses to open a workspace whose private directory is tracked by Git. It does not encrypt files, prevent an authorized OS user from modifying SQLite, or enforce roles between users who share the same filesystem account. Keep credentials out of config, source control, reports, and exported packets.
