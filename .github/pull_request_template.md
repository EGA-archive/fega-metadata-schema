## Release notes
<!-- Release-process mandatory. Use exactly one `Category: VALUE` line and Markdown bullets for the public note. VALUE must be "Added", "Changed", "Fixed", "Removed", "Security", or "None"; use "None" with no bullets when nothing belongs in the changelog. -->

Category: Added

- This PR adds X and Y, because of ...

## Summary of changes
<!-- Not used by release automation. This is free-form context, but use Markdown bullets -->

- Add X to improve metadata discovery.
- Update Y documentation for contributors.

## Compatibility review
<!-- Not part of public release notes. Write short prose without bullets only when automated compatibility is "unknown"; otherwise write `Not applicable`. -->

Not applicable

## Ticket / Issue reference
<!-- Optional and free-form. Add one or more readable issue links, or write `Not applicable`. -->

https://example.com/issues/123

## Contributor checklist
<!-- Contributor aid, not controlled release text. Keep Markdown checkboxes and tick each applicable confirmation. -->

I confirm I read the contributing documentation and the following is checked:

* [ ] **Release notes are complete** – the category and concise public summary above describe this PR. I used `None` if it should not appear in the public CHANGELOG.
* [ ] **Schema versions updated** – I updated `meta:version` in each affected schema where applicable.
* [ ] **Schemas validate examples** – I ran relevant [focused checks](https://github.com/EGA-archive/fega-metadata-schema/blob/main/docs/releases/README.md#local-commands-and-ci-gates) to test that my changes work as expected.
* [ ] **Documentation updated** – relevant pages under `docs/` (and any diagrams / examples) reflect my changes.
