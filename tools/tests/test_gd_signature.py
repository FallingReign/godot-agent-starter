from __future__ import annotations

import sys
import unittest
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

import gd_signature  # noqa: E402


class ProposalSignatureTests(unittest.TestCase):
    def test_canonicalizes_exact_typed_signature(self) -> None:
        parsed = gd_signature.parse_proposal_signature(
            " static  func move( delta : float , active: bool=true )  ->  void "
        )

        self.assertEqual("move", parsed.name)
        self.assertTrue(parsed.is_static)
        self.assertEqual(
            "static func move(delta: float, active: bool = true) -> void",
            parsed.canonical,
        )

    def test_handles_multiline_nested_defaults_strings_and_comments(self) -> None:
        parsed = gd_signature.parse_proposal_signature(
            '''
            func configure(
                values: Array[String] = ["a, # not a comment", make({"x": [1, 2]})],
                # Formatting comments are not part of the signature.
                label: String = "call()) # literal",
            ) -> Dictionary[String, Array[int]]
            '''
        )

        self.assertEqual(
            'func configure(values: Array[String] = ["a, # not a comment", '
            'make({"x": [1, 2]})], label: String = "call()) # literal") '
            '-> Dictionary[String, Array[int]]',
            parsed.canonical,
        )

    def test_rejects_untyped_parameter(self) -> None:
        with self.assertRaisesRegex(
            gd_signature.SignatureError,
            "every parameter must",
        ):
            gd_signature.parse_proposal_signature("func move(delta) -> void")

    def test_rejects_missing_return_type(self) -> None:
        with self.assertRaisesRegex(
            gd_signature.SignatureError,
            "explicit return type",
        ):
            gd_signature.parse_proposal_signature("func move(delta: float)")

    def test_rejects_decorator_source_colon_and_body(self) -> None:
        invalid = (
            '@rpc func move(delta: float) -> void',
            'func move(delta: float) -> void:',
            'func move(delta: float) -> void: pass',
        )
        for signature in invalid:
            with self.subTest(signature=signature):
                with self.assertRaises(gd_signature.SignatureError):
                    gd_signature.parse_proposal_signature(signature)

    def test_rejects_duplicate_parameter_name(self) -> None:
        with self.assertRaisesRegex(gd_signature.SignatureError, "duplicate parameter"):
            gd_signature.parse_proposal_signature(
                "func choose(value: int, value: int) -> int"
            )


class SourceFunctionTests(unittest.TestCase):
    def test_maps_named_declarations_and_ignores_noise_and_lambda(self) -> None:
        source = '''
        extends RefCounted
        # func commented(value: int) -> int:
        const TEXT: String = "func string_value() -> void:"

        @warning_ignore("unused_parameter")
        static func build(
            value: Dictionary[String, Array[int]] = {"x": [1, 2]},
        ) -> int:
            var callback: Callable = func(inner: int) -> int: return inner + 1
            return callback.call(value["x"][0])

        func reset() -> void:
            pass
        '''

        functions = gd_signature.parse_source_functions(source)

        self.assertEqual(["build", "reset"], list(functions))
        self.assertEqual(
            'static func build(value: Dictionary[String, Array[int]] = '
            '{"x": [1, 2]}) -> int',
            functions["build"].signature,
        )
        self.assertEqual("func reset() -> void", functions["reset"].signature)
        self.assertEqual(7, functions["build"].line)
        self.assertLess(functions["build"].body_start, functions["build"].body_end)

    def test_body_digest_ignores_whitespace_comments_and_newline_style(self) -> None:
        first = (
            "func score(value: int) -> int:\r\n"
            "\t# explanation\r\n"
            "\treturn value + 1\r\n"
        )
        second = (
            "func score(value: int) -> int:\n"
            "    return   value+1 # same tokens\n"
        )

        first_function = gd_signature.parse_source_functions(first)["score"]
        second_function = gd_signature.parse_source_functions(second)["score"]

        self.assertEqual(first_function.signature, second_function.signature)
        self.assertEqual(first_function.body_sha256, second_function.body_sha256)

    def test_body_digest_changes_with_implementation_tokens(self) -> None:
        before = gd_signature.parse_source_functions(
            "func score(value: int) -> int:\n    return value + 1\n"
        )["score"]
        after = gd_signature.parse_source_functions(
            "func score(value: int) -> int:\n    return value + 2\n"
        )["score"]

        self.assertNotEqual(before.body_sha256, after.body_sha256)

    def test_body_digest_preserves_semantic_indentation(self) -> None:
        outside = gd_signature.parse_source_functions(
            "func score(active: bool) -> int:\n"
            "    var value: int = 0\n"
            "    if active:\n"
            "        value += 1\n"
            "    return value\n"
        )["score"]
        inside = gd_signature.parse_source_functions(
            "func score(active: bool) -> int:\n"
            "    var value: int = 0\n"
            "    if active:\n"
            "        value += 1\n"
            "        return value\n"
        )["score"]

        # The token stream is identical; only the control-flow indentation moved.
        self.assertNotEqual(outside.body_sha256, inside.body_sha256)

    def test_contiguous_annotations_are_part_of_the_change_digest(self) -> None:
        authority = gd_signature.parse_source_functions(
            '@rpc("authority")\n'
            "func apply(value: int) -> void:\n"
            "    pass\n"
        )["apply"]
        any_peer = gd_signature.parse_source_functions(
            '@rpc( "any_peer" )\n'
            "func apply(value: int) -> void:\n"
            "    pass\n"
        )["apply"]
        reformatted_authority = gd_signature.parse_source_functions(
            '@rpc( "authority" ) func apply(value: int) -> void:\n'
            "    pass\n"
        )["apply"]

        self.assertEqual(('@rpc("authority")',), authority.annotations)
        self.assertNotEqual(authority.body_sha256, any_peer.body_sha256)
        self.assertEqual(
            authority.body_sha256,
            reformatted_authority.body_sha256,
        )

    def test_noncontiguous_script_annotation_is_not_function_identity(self) -> None:
        function = gd_signature.parse_source_functions(
            "@tool\n\n"
            "func apply() -> void:\n"
            "    pass\n"
        )["apply"]

        self.assertEqual((), function.annotations)

    def test_multiline_body_delimiters_do_not_end_on_low_indentation(self) -> None:
        source = '''
func collect() -> Array[int]:
    var values: Array[int] = [
1,
2,
    ]
    return values

func done() -> void:
    pass
'''

        functions = gd_signature.parse_source_functions(source)

        self.assertEqual(["collect", "done"], list(functions))

    def test_inner_class_methods_have_distinct_lexical_identities(self) -> None:
        source = '''
class First:
    func run() -> void:
        pass

class Second:
    func run() -> void:
        pass
'''

        functions = gd_signature.parse_source_functions(source)

        self.assertEqual(["First.run", "Second.run"], list(functions))
        self.assertEqual("run", functions["First.run"].name)
        self.assertEqual("First.run", functions["First.run"].identity)
        self.assertEqual(("First",), functions["First.run"].scope)
        self.assertEqual(("Second",), functions["Second.run"].scope)
        self.assertEqual(
            functions["First.run"].signature,
            functions["Second.run"].signature,
        )

    def test_nested_inner_class_identity_contains_the_full_scope(self) -> None:
        functions = gd_signature.parse_source_functions(
            "class Outer:\n"
            "    class Inner:\n"
            "        func run() -> void:\n"
            "            pass\n"
        )

        self.assertEqual(["Outer.Inner.run"], list(functions))
        self.assertEqual(("Outer", "Inner"), functions["Outer.Inner.run"].scope)

    def test_rejects_duplicate_function_identity_in_one_scope(self) -> None:
        source = '''
class First:
    func run() -> void:
        pass

    func run() -> void:
        pass
'''

        with self.assertRaisesRegex(gd_signature.SignatureError, "duplicate function"):
            gd_signature.parse_source_functions(source)

    def test_parses_bodyless_abstract_method_with_stable_digest(self) -> None:
        multiline = gd_signature.parse_source_functions(
            "@abstract\n"
            "func resolve(value: int) -> String\n"
        )["resolve"]
        inline = gd_signature.parse_source_functions(
            "@abstract func resolve( value : int ) -> String\n"
        )["resolve"]

        self.assertTrue(multiline.is_abstract)
        self.assertEqual(("@abstract",), multiline.annotations)
        self.assertEqual("func resolve(value: int) -> String", multiline.signature)
        self.assertEqual(multiline.body_start, multiline.body_end)
        self.assertEqual(multiline.body_sha256, inline.body_sha256)

    def test_abstract_method_rejects_a_body_or_trailing_colon(self) -> None:
        with self.assertRaisesRegex(gd_signature.SignatureError, "bodyless"):
            gd_signature.parse_source_functions(
                "@abstract\n"
                "func resolve() -> void:\n"
                "    pass\n"
            )

    def test_rejects_malformed_named_declaration(self) -> None:
        invalid = (
            "func missing_type(value) -> void:\n    pass\n",
            "func missing_return(value: int):\n    pass\n",
            "func missing_body(value: int) -> void:\n",
            "func broken(value: Array[int) -> void:\n    pass\n",
        )
        for source in invalid:
            with self.subTest(source=source):
                with self.assertRaises(gd_signature.SignatureError):
                    gd_signature.parse_source_functions(source)


if __name__ == "__main__":
    unittest.main()
