"""Check examples with local references and unconditional inherited property rules.

Projection follows only $ref/allOf, never conditional or alternative branches:
those are evaluated by jsonschema when validating the containing instance.
Custom-keyword constraints require the optional Biovalidator backend.
"""

from __future__ import annotations

import re
import json

from jsonschema import FormatChecker
from jsonschema.validators import validator_for
from referencing.exceptions import Unresolvable

from fega_tools.validation_common import VALID_STATUS
from .api_examples import (
    ApiExampleValidator,
    bundle_constraints,
    contains_custom_keywords,
)
from .inheritance import effective_constraints
from .model import Node, pointer
from .resolution import OutsideResolutionRoot


def check_format_dependencies() -> None:
    """Do not silently accept unchecked URI/date-time examples in partial installs."""
    # These formats are used by the maintained schemas but require jsonschema's
    # optional dependencies. Its default behavior is to ignore missing checkers.
    missing = {"uri", "date-time"} - FormatChecker().checkers.keys()
    if missing:
        raise RuntimeError(
            "Missing JSON Schema format validators: "
            + ", ".join(sorted(missing))
            + ". Install the format extras in this Python environment with "
            "python -m pip install -r requirements.txt."
        )


def examples(
    node: Node,
    *,
    api: ApiExampleValidator | None = None,
    skipped: set[str] | None = None,
):
    """Validate each example against its inherited constraints."""
    values = node.schema.get("examples")
    if not isinstance(values, list):
        return
    try:
        # Keep resolvers attached to each inherited constraint, so relative refs,
        # nested IDs and anchors retain their original scope.
        constraints = {}
        for bound in effective_constraints(node):
            constraints[id(bound.schema)] = bound
        custom = {
            key for key, bound in constraints.items() if contains_custom_keywords(bound)
        }
    except (Unresolvable, ValueError, TypeError, RecursionError, re.error) as exc:
        yield node.issue(
            "example-validation",
            f"Cannot resolve inherited constraints: {exc}",
            "examples",
        )
        return
    if custom:
        reason = "custom keywords require --check-api"
        if api is not None:
            try:
                schema = bundle_constraints(list(constraints.values()))
            except OutsideResolutionRoot:
                reason = "API check depends on an out-of-scope reference"
            except (Unresolvable, ValueError, TypeError, RecursionError) as exc:
                yield node.issue(
                    "example-validation",
                    f"Cannot prepare Biovalidator schema: {exc}",
                    "examples",
                )
                return
            else:
                for index, example in enumerate(values):
                    result = api.validate(schema, example)
                    if result["status"] != VALID_STATUS:
                        yield node.issue(
                            "example-validation",
                            f"Biovalidator {result['status']}: "
                            + json.dumps(result.get("errors", []), ensure_ascii=False),
                            "examples",
                            index,
                        )
                return
        if skipped is not None:
            skipped.add(f"{node.file}#{pointer(node.path + ('examples',))}: {reason}")
    for index, example in enumerate(values):
        messages = set()
        for key, bound in constraints.items():
            if key in custom:
                # Ignoring a custom keyword can change oneOf/not/if outcomes.
                # Only constraints independent of that logic are checked locally.
                continue
            try:
                cls = validator_for(bound.schema)
                validator = cls(
                    bound.schema,
                    _resolver=bound.resolver,
                    format_checker=FormatChecker(),
                )
                for error in validator.iter_errors(example):
                    message = f"Instance #{pointer(tuple(error.absolute_path))}: {error.message}"
                    if message not in messages:
                        messages.add(message)
                        yield node.issue(
                            "example-validation", message, "examples", index
                        )
            except OutsideResolutionRoot:
                # Never substitute true/false for an excluded reference: either
                # can produce incorrect results under not/oneOf/if. Leave this
                # dependent constraint unchecked and continue independent ones.
                continue
            except (
                Unresolvable,
                ValueError,
                TypeError,
                RecursionError,
                re.error,
            ) as exc:
                yield node.issue(
                    "example-validation",
                    f"Cannot validate example: {exc}",
                    "examples",
                    index,
                )
