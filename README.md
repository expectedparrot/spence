# Spence

<p align="center">
  <img src="docs/assets/spence-artwork.png" alt="Spence artwork featuring a peacock with its tail fanned inside brackets" width="640">
</p>

Spence is an applicant tracking CLI built on EDSL humanize. It connects job posts, hosted application forms, candidate records, and structured human reviews in one private workspace.

**Status: working 0.1 alpha.** The local workflow and EDSL client contracts are tested. A real hosted acceptance run is still required before using it with applicants. Hosted closure, access revocation, file sharing, and erasure have explicit limitations described in [provider contracts](docs/provider-contracts.md).

**[Read the worked tutorial](https://expectedparrot.github.io/spence/)** — a complete walkthrough from a job post to an application, two reviewers, and a recorded decision. You can also open [docs/index.html](docs/index.html) locally.

## Install

Python 3.11+ and Git are required. From this directory:

```sh
uv venv .venv
uv pip install --python .venv/bin/python -e '.[dev]'
source .venv/bin/activate
spence --help
```

The dependency follows **`expectedparrot/edsl` on GitHub `main`**, rather than the PyPI release. Each saved form records the installed EDSL version and Git commit when available. To refresh that dependency later:

```sh
uv pip install --python .venv/bin/python --upgrade-package edsl -e '.[dev]'
```

## Try the complete workflow locally

```sh
python examples/demo.py /tmp/spence-demo
```

Choose a new destination. This uses fictitious candidates and reviewers, sends no email, and makes no provider requests. It creates an opening, a post with an Apply link, a submitted application, two review assignments, one completed review, a private HTML report, and a SQLite backup. Demo URLs use `demo.invalid` and are deliberately not hosted forms.

The script prints the report path. You can continue exploring:

```sh
cd /tmp/spence-demo
spence opening list
spence applications list --opening backend
spence reviews list --opening backend
spence operation list
```

## What works

- Import ordinary Markdown posts and role JSON, or import a McCall post export with its content hash.
- Create reusable application forms and review rubrics, or import your own EDSL Survey and humanize schema.
- Freeze the form, post, and candidate packet used by each publication; export human-only `.jobs.ep` artifacts.
- Publish applications through humanize and export a post containing its Apply link.
- Import completed responses into private SQLite records; deduplicate by provider response identity and preserve revisions.
- Search candidates, record notes, and move applications through submitted, screening, review, interview, offer, hired, rejected, or withdrawn stages.
- Prepare candidate-specific reviewer rosters and selected text packets, publish personal links, explicitly send invitations, and track delivery separately from feedback.
- Produce escaped HTML reports showing attributed opinions, outstanding reviews, and reviews of older application versions.
- Quarantine incomplete or unattributable submissions and reconcile remote operations whose outcome is uncertain.

Spence operates as one trusted team workspace. It has no employer web dashboard or multi-user authorization service. It does not run candidate data through language models or compute an automatic hiring rank.

## Use hosted applications

Configure Expected Parrot authentication as you normally do for `ep`. Spence uses `Coop()` and does not store API credentials in its workspace configuration.

```sh
spence init hiring --provider coop --account my-team --owner hiring-operator
cd hiring
spence role import /path/to/role.json --id engineer
spence post import /path/to/post.md --id advert
spence opening create --id backend --role engineer
spence opening add-post backend --post advert
spence application-form init --id short
spence application-form build short --opening backend --post advert --output application.jobs.ep
spence opening publish backend --post advert --form short
```

A role JSON needs at least `{"title": "Backend Engineer"}`. Publication prints the Apply URL and publication ID. `build` is local; `publish` uploads the form and its public job context. Publication does not send invitations.

```sh
spence opening export-post backend --publication PUB_ID --output post-with-apply.md
spence applications sync --opening backend
spence applications list --opening backend --stage submitted
spence applications list --search taylor
spence application show APP_ID
spence application move APP_ID --to screening --reason 'Relevant experience merits a review.'
```

Replace uppercase IDs with the IDs returned by your workspace. A McCall export is imported with `spence post import post.json --mccall-export`. McCall remains independent and focused on job-post studies.

## Design an application or rubric

Use EDSL to author a Survey, including answer-based skip rules, and save it as Survey JSON or `.ep`. Import it together with a humanize schema and a mapping from application questions to contact/profile fields:

```sh
spence application-form import --id custom --survey survey.ep --schema humanize.json --mapping fields.json
spence application-form validate custom
spence application-form show custom
spence application-form export custom --output custom-definition.json
```

Example `fields.json`:

```json
{
  "full_name": "contact.name",
  "email_address": "contact.email",
  "experience": "profile.experience",
  "resume": "attachments.resume"
}
```

Both contact fields are required mappings and must use free-text questions. Supported question types are free text, multiple choice, checkboxes, numerical, list, and file upload. Static answer constraints and answer-based skip rules are supported; advanced computed or dynamically templated answer constraints are outside this alpha's validated contract. Question text can use the public `job_post` scenario field. Review questions can additionally use `candidate_packet`.

`application-form init --id with-resume --resume` adds a file-upload question. Spence stores provider file references; it does not download files or expose them in reviewer packets. Hosted upload and attachment access still need verification. Requiredness is evaluated along the submitted survey path. Unknown or malformed responses remain available in quarantine.

Use `--revise` to create a new form or rubric revision. Already prepared publications retain their original snapshots. Content containing Jinja delimiters is rejected when building a job or candidate packet because EDSL could interpret it as a template.

## Request human feedback

Import a reviewer JSON object, or a list such as:

```json
[
  {"id": "alex", "name": "Alex", "email": "alex@example.com"},
  {"id": "sam", "name": "Sam", "email": "sam@example.com"}
]
```

```sh
spence reviewer import reviewers.json
spence review-template init --id rubric
spence review prepare APP_ID --template rubric --reviewer alex --reviewer sam
spence review inspect BATCH_ID
spence review publish BATCH_ID
spence review send BATCH_ID
spence operation delivery-status OP_ID
spence reviews sync --opening backend
spence application report APP_ID --output candidate-report.html
```

**`review send` sends real email in a Coop workspace.** Use it only after inspecting the packet and recipient roster. Repeated sends require `--remind`; a batch containing completed or revoked reviews must be replaced with an appropriate roster. Default packets include mapped `profile.*` text answers and the public job post. Repeated `--field QUESTION_NAME` flags select different answer fields. Contact information, file references, internal notes, and prior reviews are excluded by default.

To import your own feedback survey, use `review-template import --id rubric --survey review.ep --schema review-ui.json`. `review links BATCH_ID --output links.json` exports personal links as a private file. Treat those links as access credentials.

## Recovery and private data

Read [operations](docs/operations.md) for quarantine imports, timeout recovery, backups, and the limits of closing or deleting data. `spence capabilities` reports the configured adapter's supported controls.

Private records, respondent links, packets, and raw responses live in `.spence-private/`, excluded from Git. Its directory is owner-only and generated private files use mode `0600`. The database is not encrypted; operating-system access controls protect this local workspace. Reports and exports contain personal data even when stored outside that directory.

## Development

```sh
pytest -q
ruff check src tests examples
ruff format --check src tests examples
```

The tests use a local demo provider and mocked Coop calls checked against real EDSL client signatures. They do not make live publications or send messages. CI tests Python 3.11 and 3.12 against GitHub `main`.

See the [architecture](docs/architecture.md), [provider contracts and outstanding work](docs/provider-contracts.md), and the broader [ATS specification](docs/ats-spec.md).

## License

[MIT](LICENSE) · Copyright (c) 2026 Expected Parrot.
