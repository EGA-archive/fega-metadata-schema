# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project will follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) once schema releases begin.

## [Unreleased]

### Added

- Added the initial FEGA metadata model, including entity, graph, profile, and shared-definition schemas with version metadata for the `2.0.0-draft.1` test release.
- Added JSON-LD contexts, frames, and example payloads for schema validation across [schemas/entities](schemas/entities), [schemas/graph](schemas/graph), and related directories.
- Added validation tooling and reusable Python helpers for JSON Schema, JSON-LD, RDF/SHACL, and Biovalidator checks in [scripts/py](scripts/py) and [src/fega_tools](src/fega_tools).
- Added automated release tooling for compatibility and version checks, structured release notes, manifests, changelogs, reproducible release candidates, and publication workflows.
- Added automated tests and CI coverage for repository validation, schema behaviour, release preparation, and release verification in [tests](tests) and [.github/workflows](.github/workflows).
- Added supporting standards material, technical and release documentation, and contributor, issue, and discussion guidance under [standards](standards), [docs](docs), [README.md](README.md), and [CONTRIBUTING.md](CONTRIBUTING.md).
