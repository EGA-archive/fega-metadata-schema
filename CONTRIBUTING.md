# Contributing to **fega-metadata-schema**

Thank you for your interest in improving the EGA metadata model!  We welcome pull-requests and issue reports from **FEGA nodes, downstream integrators, and the wider community**.  This document explains the conventions and workflow we follow in this repository.

> **Scope** – This repo contains JSON Schemas, JSON-LD contexts/frames, examples, validation utilities and documentation *specific to the EGA metadata model*.  Issues relating to other FEGA software (portal, download client, etc.) should be opened in their respective repositories. For general inquiries, please fill the [**Need-help form**](https://ega-archive.org/need-help/).

## 1  Ways to contribute

| Activity | How |
| --------------------------------------- | ----------------- |
| **Report a bug**                        | Use the *Bug report* issue template. It collects schema/tool versions, reproduction steps, and expected vs. observed behaviour.       |
| **Suggest a change**              | Use the *Change request* issue template. Describe the use-case first, then a proposal (new property, enum value, structural change, etc.). |
| **Direct contributions**               | Open a pull-request containing the pertinent changes, and following the [PR template](./.github/pull_request_template.md). |
| **Ask a question / start a discussion** | Either create a [blank GH issue](https://github.com/EGA-archive/fega-metadata-schema/issues/new) or reach out to the community through the [ELIXIR FEGA Slack channel](https://elixir-europe.slack.com/archives/C05UHABF0CT). |

## 2  Before you start

* **Check existing [issues](https://github.com/EGA-archive/fega-metadata-schema/issues)/[PRs](https://github.com/EGA-archive/fega-metadata-schema/pulls)** – someone might be working on the same topic.
* **Follow the Code of Conduct** – be respectful and constructive.
* **Use descriptive titles** – this helps triaging and search.
* **Provide detailed descriptions** – this helps fixing or improving the content faster.

## 3  Pull-request workflow

1. **Fork** the repository (or create a feature branch if you have push rights).
2. Create a **topic branch** off `main`, e.g. `feat/add-sample-tissue-enum`.
3. Make the smallest complete change and add or update examples/docs so its behaviour is demonstrable. CI runs the independent Python, schema, JSON-LD, frame, RDF, SHACL, PR-note, release-policy and release-consistency checks on the pull request.
4. Fill the PR template's strict `## Release notes` section. If compatibility is reported as `unknown`, add a concrete `## Compatibility review` rationale.
5. Manually update top-level `meta:version` in every affected component schema. Do not edit the generated manifest, versioned changelog, citation version, component inventory or URI snapshots; preparation generates and asserts them.
6. Run focused checks locally where practical; a realistic minimum is `env PYTHONPATH=src .venv/bin/python scripts/py/release.py discover -v` followed by `env PYTHONPATH=src .venv/bin/python scripts/py/release.py verify --mode development -v`.
7. **Add or update data / docs** so behaviour is demonstrably correct.
8. Commit with a **clear message** (`type(scope): summary`, e.g. `feat(schema): add new library_strategy enum values`).
9. Push and **open the PR**.  The pull-request template will guide you through the last checks.

### Review & merge rules

* Two approving reviews from maintainers are required.
* CI **must pass** (e.g., lints, validation).
* Squash-and-merge is acceptable for ordinary PRs. Generated two-commit release PRs are merged with a merge commit so their reviewed R1 and R2 commits remain addressable.

The complete preparation, review, publication and recovery runbook is in the [release guide](./docs/releases/README.md).

## 4  Coding & style guidelines

| Area                | Tool / Convention   |
| ------------------- | ------------------- |
| **Python**          | Type hints encouraged; keep reusable logic in `src/fega_tools/`.          |
| **JSON Schemas**    | `$id`, `title`, `description` and `meta:version` are mandatory; use `$ref` over copy-paste. More details at [``schemas/entities``](./schemas/entities/README.md). |
| **Commit messages** | Conventional Commits style (`fix:`, `feat:`, `docs:` …).                                       |
| **Docs**            | Markdown (`.md`), Mermaid for process diagrams, and SVG/PNG in `docs/images/` for static graphics. |

## 5  License & contributor certificate

By submitting code, documentation or schema changes you agree that your contribution is licensed under the terms stated in [`LICENSE`](./LICENSE).  If you include third-party material, ensure it is compatible with this license and properly attributed.

## 6  Need help?

* **Helpdesk:** [Need-help form](https://ega-archive.org/need-help/)
* **Slack:** [ELIXIR FEGA Slack channel](https://elixir-europe.slack.com/archives/C05UHABF0CT)

We appreciate every contribution – large or small – that makes the FEGA metadata ecosystem more robust and easier to use.  Thank you for helping the community!
