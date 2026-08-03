# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project will follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) once schema releases begin.

## [Unreleased]

### Added

- Added initial draft metadata schemas for EGA entities and shared common definitions in [schemas](schemas) and [schemas/common](schemas/common).
- Added JSON-LD contexts, frames, and example payloads for schema validation across [schemas/entities](schemas/entities), [schemas/graph](schemas/graph), and related entity directories.
- Added validation tooling and reusable Python helpers for schema, JSON-LD, RDF, and Biovalidator workflows in [scripts/py](scripts/py) and [src/fega_tools](src/fega_tools).
- Added automated test coverage for repository validation and schema behaviour in [tests](tests).
- Added supporting standards material and project documentation under [standards](standards), [docs](docs), [README.md](README.md), and [CONTRIBUTING.md](CONTRIBUTING.md).
