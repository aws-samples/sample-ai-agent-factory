"""Canonical deployed-code templates — single source of truth.

The ``.py`` files in this package are NOT application code. They are
syntax-valid Python source *templates* that get read as text and embedded
into deployed artifacts:

- ``gateway_deployer``  — zips them into Gateway tool Lambda functions
- ``code_generator``    — injects the tool-impl block into generated agent code
- ``cfn_template_generator`` — ships them inside downloadable CFN bundles

Keeping them as real Python files (instead of f-strings/string literals in
three separate services) means one bugfix lands everywhere, the code is
lint/compile-checked, and there is no brace-escaping hell. Substitution, when
needed, uses plain ``str.replace`` markers — never f-strings.
"""

from importlib import resources

_DOCSTRING_DELIM = '"""'
_IMPORTS_START = "# --- template-only imports (stripped when rendered) ---"
_IMPORTS_END = "# --- end template-only imports ---"


def load(name: str) -> str:
    """Return the raw source text of a template module in this package."""
    return resources.files(__package__).joinpath(f"{name}.py").read_text(encoding="utf-8")


def load_impl(name: str) -> str:
    """Return template source ready for embedding in generated/deployed code.

    Strips the leading module docstring (template provenance notes, not
    meaningful in deployed code) and the marked template-only import block
    (imports that exist only so the template file lints and compiles
    standalone; when rendered, those names are provided by the surrounding
    composed source).
    """
    src = load(name)
    if src.startswith(_DOCSTRING_DELIM):
        end = src.find(_DOCSTRING_DELIM, len(_DOCSTRING_DELIM))
        if end >= 0:
            src = src[end + len(_DOCSTRING_DELIM) :]
    start = src.find(_IMPORTS_START)
    end = src.find(_IMPORTS_END)
    if start >= 0 and end > start:
        src = src[:start] + src[end + len(_IMPORTS_END) :]
    return src.lstrip("\n")


def dynamic_tools_lambda_source() -> str:
    """Full source of the dynamic-tools Gateway Lambda.

    Composes the canonical web-tool implementations (search / wikipedia /
    weather / SSRF-guarded webpage fetch), the customer-support demo data +
    handlers, and the tool-name dispatcher into one self-contained module.
    """
    return "\n\n".join(
        (
            load_impl("dynamic_tools_impl"),
            load_impl("customer_support_impl"),
            load_impl("dynamic_tools_handler"),
        )
    )


def customer_support_tools_lambda_source() -> str:
    """Standalone customer-support tools Lambda (data + handlers + dispatcher).

    Used by the CFN bundle's tool-lambdas.zip so exported stacks deploy the
    same hardened implementations as the UI deploy path.
    """
    return "\n\n".join(
        (
            load_impl("customer_support_impl"),
            load_impl("customer_support_handler"),
        )
    )
