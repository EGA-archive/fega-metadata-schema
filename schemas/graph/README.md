# EGA graph schemas

The graph schema validates a typed named JSON-LD graph.

Graph validation currently covers JSON Schema shape only. It does not validate graph-wide relationship semantics (e.g., that a process has compatible input/output types, that relationship identifiers resolve to another graph item, or that the graph has a particular workflow). Those graph-wide constraints are reserved for future SHACL work.

There are three main threads of constraints within the `graph` directory:

- Every item in `@graph` is first identified by a lightweight `@type` declaration predicate and, when that declaration is structurally usable, validated against the corresponding entity schema. Keeping type detection separate from entity validation prevents one malformed item from being tested against every entity branch.
- Small reusable graph requirements are named definitions under [`schema.json`](./schema.json)'s `$defs`.
- Reusable profiles are individual files under `profiles/` and compose the base graph schema with those requirements. Requirements test whether a node declares the required type, but the base graph schema remains responsible for validating that node completely. For example, [`profiles/dataset-and-datafile.schema.json`](./profiles/dataset-and-datafile.schema.json) requires at least one Dataset and one Datafile in the `@graph`.

To validate all the examples (valid and invalid appropriately), start Biovalidator with the repository schemas preloaded, then run:

```bash
python scripts/py/validate_examples.py \
  --root schemas --entity graph -v
```
