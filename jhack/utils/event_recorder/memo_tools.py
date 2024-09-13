#!/usr/bin/env python3
import ast
import typing
from dataclasses import dataclass
from operator import itemgetter
from pathlib import Path
from textwrap import dedent
from typing import Dict, Literal, Optional, Tuple, Union

import asttokens
from asttokens.util import Token
from astunparse import unparse

if typing.TYPE_CHECKING:
    from recorder import SUPPORTED_SERIALIZERS


@dataclass
class DecorateSpec:
    # on strict caching mode, each call will be separately logged.
    # use this when calling the same method (with the same arguments) at different times CAN return
    # different values.
    # Use loose caching mode when the method is guaranteed to return consistent results throughout
    # a single charm execution.
    caching_policy: Literal["strict", "loose"] = "strict"

    # the memo's namespace will default to the class name it's being defined in
    namespace: Optional[str] = None

    # the memo's name will default to the memoized function's __name__
    name: Optional[str] = None

    # (de) serializer for the return object of the decorated function
    serializer: Union[
        "SUPPORTED_SERIALIZERS", Tuple["SUPPORTED_SERIALIZERS", "SUPPORTED_SERIALIZERS"]
    ] = "json"

    def as_token(self, default_namespace: str) -> Token:
        name = f"'{self.name}'" if self.name else "None"

        if isinstance(self.serializer, str):
            serializer = f"'{self.serializer}'"
        else:
            serializer = f"{str(self.serializer)}"

        raw = dedent(
            f"""@memo(
            name={name},
            namespace='{self.namespace or default_namespace}',
            caching_policy='{self.caching_policy}',
            serializer={serializer},
            )\ndef foo():..."""
        )
        return asttokens.ASTTokens(raw, parse=True).tree.body[0].decorator_list[0]


DECORATE_MODEL = {
    "_ModelBackend": {
        "relation_get": DecorateSpec(),
        "relation_set": DecorateSpec(),
        "is_leader": DecorateSpec(),  # technically could be loose
        "application_version_set": DecorateSpec(),
        "status_get": DecorateSpec(),
        "action_get": DecorateSpec(),
        "add_metrics": DecorateSpec(),  # deprecated, I guess
        "action_set": DecorateSpec(caching_policy="loose"),
        "action_fail": DecorateSpec(caching_policy="loose"),
        "action_log": DecorateSpec(caching_policy="loose"),
        "relation_ids": DecorateSpec(caching_policy="loose"),
        "relation_list": DecorateSpec(caching_policy="loose"),
        "relation_remote_app_name": DecorateSpec(caching_policy="loose"),
        "config_get": DecorateSpec(caching_policy="loose"),
        "resource_get": DecorateSpec(caching_policy="loose"),
        "storage_list": DecorateSpec(caching_policy="loose"),
        "storage_get": DecorateSpec(caching_policy="loose"),
        "network_get": DecorateSpec(caching_policy="loose"),
        # methods that return None can all be loosely cached
        "status_set": DecorateSpec(caching_policy="loose"),
        "storage_add": DecorateSpec(caching_policy="loose"),
        "juju_log": DecorateSpec(caching_policy="loose"),
        "planned_units": DecorateSpec(caching_policy="loose"),
        # 'secret_get',
        # 'secret_set',
        # 'secret_grant',
        # 'secret_remove',
    }
}

DECORATE_PEBBLE = {
    "Client": {
        # todo: we could be more fine-grained and decorate individual Container methods,
        #  e.g. can_connect, ... just like in _ModelBackend we don't just memo `_run`.
        "_request": DecorateSpec(),
        # some methods such as pebble.pull use _request_raw directly,
        # and deal in objects that cannot be json-serialized
        "pull": DecorateSpec(serializer=("json", "io")),
        "push": DecorateSpec(serializer=("PebblePush", "json")),
    }
}


memo_import_block = dedent(
    """try:
    from recorder import memo
except ModuleNotFoundError as e:
    msg = "recorder not installed. " \
          "This can happen if you're playing with Runtime in a local venv. " \
          "In that case all you have to do is ensure that the PYTHONPATH " \
          "is patched to include the path to " \
          "recorder.py before loading this module. " \
          "Tread carefully."
    raise RuntimeError(msg) from e
"""
)


def inject_memoizer(source_file: Path, decorate: Dict[str, Dict[str, DecorateSpec]]):
    """Rewrite source_file by decorating methods in a number of classes.

    Decorate: a dict mapping class names to methods of that class that should be decorated.
    Example::
        >>> inject_memoizer(Path('foo.py'), {'MyClass': {
        ...     'do_x': DecorateSpec(),
        ...     'is_ready': DecorateSpec(caching_policy='loose'),
        ...     'refresh': DecorateSpec(caching_policy='loose'),
        ...     'bar': DecorateSpec(caching_policy='loose')
        ... }})
    """

    atok = asttokens.ASTTokens(source_file.read_text(), parse=True).tree
    last_import_idx = 0
    for last_import_idx, item in enumerate(atok.body):
        if not isinstance(item, ast.ImportFrom):
            # first non-import block
            break

    # insert the memo import after any other import statement
    memo_atok = asttokens.ASTTokens(memo_import_block, parse=True).tree
    while memo_atok.body:
        atok.body.insert(last_import_idx + 1, memo_atok.body.pop())

    def _should_decorate_class(token: ast.AST):
        return isinstance(token, ast.ClassDef) and token.name in decorate

    for cls in filter(_should_decorate_class, atok.body):

        def _should_decorate_method(token: ast.AST):
            return (
                isinstance(token, ast.FunctionDef) and token.name in decorate[cls.name]
            )

        for method in filter(_should_decorate_method, cls.body):
            existing_decorators = {
                token.first_token.string for token in method.decorator_list
            }
            # only add the decorator if the function is not already decorated:
            if "memo" not in existing_decorators:
                spec_token = decorate[cls.name][method.name].as_token(
                    default_namespace=cls.name
                )
                method.decorator_list.append(spec_token)

    # horrible solution for a very weird issue nobody seems to care about
    # https://github.com/simonpercivall/astunparse/issues/58
    unparsed_source = unparse(atok).replace('""""', '" """')
    source_file.write_text(unparsed_source)
