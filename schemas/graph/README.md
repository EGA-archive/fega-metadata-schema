# EGA graph schemas

The graph schema validates a typed named JSON-LD graph.

Graph validation currently covers JSON Schema shape only. It does not validate graph-wide relationship semantics (e.g., that a process has compatible input/output types, that relationship identifiers resolve to another graph item, or that the graph has a particular workflow). Those graph-wide constraints are reserved for future SHACL work.

There are three main threads of constraints within the `graph` directory:

- Every item in `@graph` is identified by its EGA `@type` and, based on it, validated against the corresponding entity schema.
- Smaller modular requirements validate parts of the `@graph` and can be reused by profiles.
- Reusable `profiles` compose the base graph schema with presence requirements. For example, [`profiles/dataset-and-datafile/schema.json`](./profiles/dataset-and-datafile/schema.json) requires that there is a dataset and a datafile in the `@graph`.

To validate all the examples (valid and invalid appropriately), start Biovalidator with the repository schemas preloaded, then run:

```bash
python scripts/py/validate_examples.py \
  --root schemas --entity graph -v
```