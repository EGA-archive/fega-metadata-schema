from fega_tools.jsonld_utils import find_undefined_terms


def test_scoped_null_mapping_reports_the_nested_source_path():
    context = {"title": "https://example.org/title", "nested": {
        "@id": "https://example.org/nested", "@context": {"title": None}
    }}
    assert find_undefined_terms({"title": "kept", "nested": {"title": "lost"}}, context) == ["/nested/title"]


def test_type_scopes_and_out_of_scope_terms():
    context = {"kind": {"@id": "https://example.org/Kind", "@context": {"local": "https://example.org/local"}},
               "child": "https://example.org/child"}
    assert find_undefined_terms({"@type": "kind", "local": "kept", "child": {"local": "lost"}}, context) == ["/child/local"]


def test_json_literals_vocab_reverse_and_foreign_extensions_are_preserved():
    context = {"@vocab": "https://example.org/", "payload": {"@id": "https://example.org/payload", "@type": "@json"},
               "parent": {"@reverse": "https://example.org/child"}}
    data = {"payload": {"arbitrary key": {"@id": "literal text"}}, "extension": "ok",
            "parent": {"@id": "https://example.org/parent"}, "https://foreign.example/property": "ok"}
    assert find_undefined_terms(data, context) == []


def test_null_context_reset_and_array_pointer():
    context = {"title": "https://example.org/title", "nested": "https://example.org/nested"}
    data = {"nested": [{"@context": None, "title": "lost"}]}
    assert find_undefined_terms(data, context) == ["/nested/0/title"]
