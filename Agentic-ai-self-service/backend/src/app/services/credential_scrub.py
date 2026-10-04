"""Write-only credentials never persist.

A LiteLLM virtual key typed into the gateway modal was autosaved and landed in DynamoDB in
plaintext. The frontend now strips such fields before every save, but the backend is the
authority: a stored flow, a registry snapshot, a typed workflow record or any other durable record
may hold only *references* to a credential (``*Ref``, ``*Arn``, ids, names), never the value.

ARCC cnt_dwzZ05hLnqhYXQ: a plaintext secret accepted as API *input* is the antipattern, so this
module is mitigation at every persistence boundary, not compliance. cnt_n8LpZcqYi2t3I2: the value
must live in Secrets Manager and be reached by reference.

Matching is by CANONICAL key: lower-cased with every non-alphanumeric character removed, so
``apiKey``, ``api_key``, ``api-key``, ``API.KEY`` and ``ApiKey`` are one key. A reference is kept
by construction: ``apiKeyRef`` canonicalises to ``apikeyref``, which is not a credential token.

Three kinds of key, three rules:

* a **value key** (``apiKey``, ``password``, ``token``...) -- a scalar under it is dropped; an object
  or list under it keeps only ``*Ref``/``*Arn`` entries holding an AWS ARN;
* a **container key** (``credentials``, ``credential``, ``creds``) -- a scalar under it is dropped; an
  object under it is walked in *container context*, a closed allowlist: structural metadata
  (``clientId``, ``scopes``, ``provider``, ``type``, ``credentialLocation``, parameter/header
  NAMES, ``region``, ``jsonKey``, ``secretName``/``secretId``), https URLs with no userinfo,
  query or fragment, and ``*Ref``/``*Arn`` entries holding an AWS ARN. Every other leaf is
  dropped, whatever it is called;
* every other key -- walked as ordinary data.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: Canonical tokens (see module docstring) whose VALUE is a credential.
WRITE_ONLY_CREDENTIAL_TOKENS: frozenset[str] = frozenset(
    {
        "litellmapikey",
        "apikey",
        "apisecret",
        "secretkey",
        "secretaccesskey",
        "awssecretaccesskey",
        "clientsecret",
        "secretvalue",
        "secret",
        "password",
        "passwd",
        "passphrase",
        "token",
        "accesstoken",
        "refreshtoken",
        "sessiontoken",
        "idtoken",
        "bearertoken",
        "authtoken",
        "authorization",
        "privatekey",
        "hmacsecret",
        "webhooksecret",
        "signingsecret",
        "credentialvalue",
        # F-11: a database/broker connection string embeds its password.
        "connectionstring",
        "connstr",
    }
)

#: F-11. A key whose canonical form ENDS with a token above names a credential too:
#: ``GITHUB_TOKEN`` -> ``githubtoken``, ``OPENAI_API_KEY`` -> ``openaiapikey``, ``X-Api-Key`` ->
#: ``xapikey``, ``slack_token`` -> ``slacktoken``. These are the spellings an MCP node's ``env`` and
#: an Observability node's ``extraHeaders`` actually carry, and exact membership let every one of
#: them persist in plaintext. ``error_sanitizer._SECRET_PATTERNS`` grew a leading ``[a-z0-9_]*`` for
#: precisely this class of name; this is the same rule at the persistence boundary.
#:
#: It is a PREFIX only. No trailing wildcard, for the reason error_sanitizer gives: ``apiKeyRef``,
#: ``tokenEndpoint``, ``secretName`` and ``token_count`` are references, locators and counters, and
#: naming a reference is what makes a record usable. Longest token first, so ``x_client_secret``
#: reports ``clientsecret`` rather than ``secret``.
_PREFIX_MATCH_TOKENS: tuple[str, ...] = tuple(sorted(WRITE_ONLY_CREDENTIAL_TOKENS, key=len, reverse=True))

#: Names that end with a credential token but are opaque positions, not grants: the pagination
#: cursors every AWS list API returns. Nothing else is exempted by name; a flag or counter under a
#: prefixed name is kept by its VALUE shape instead (see ``_flag_or_count``).
_PREFIXED_TOKEN_EXEMPTIONS: frozenset[str] = frozenset(
    {
        "nexttoken",
        "continuationtoken",
        "paginationtoken",
        "pagetoken",
        "startingtoken",
        "exclusivestarttoken",
    }
)

#: Canonical tokens that name a credential CONTAINER (see module docstring).
CREDENTIAL_CONTAINER_TOKENS: frozenset[str] = frozenset({"credential", "credentials", "creds"})

#: Backwards-compatible view for callers that iterate the documented spellings.
WRITE_ONLY_CREDENTIAL_KEYS: frozenset[str] = WRITE_ONLY_CREDENTIAL_TOKENS

#: Credential CONTEXT is everything under a value key or inside a container. There, the rule is a
#: closed allowlist, not a heuristic: a leaf survives only if its name is structural metadata
#: below (with the value shape each requires) or a validated reference; every other scalar --
#: ``opaque``, ``secretSource``, ``label``, ``apiKeyHeader``, anything -- is dropped. Under a VALUE
#: key (``{"apiKey": {...}}``) only validated references survive at all.
_CONTEXT_METADATA_TOKENS: frozenset[str] = frozenset(
    {
        "clientid",
        "provider",
        "providertype",
        "providername",
        "type",
        "kind",
        "credentiallocation",
        "location",
        "credentialparametername",
        "parametername",
        "headername",
        "region",
        "jsonkey",
        "secretname",
        "secretid",
        "name",
        "id",
        "grant",
        "granttype",
        "authtype",
        "scheme",
        "enabled",
    }
)
#: Metadata whose value is a list of plain scalars (OAuth scopes).
_CONTEXT_LIST_TOKENS: frozenset[str] = frozenset({"scopes", "scope", "audiences", "audience"})
#: Metadata whose value must parse as an https URL with no userinfo, query or fragment; a URL that
#: fails is DROPPED (not truncated: truncating would silently change the endpoint's meaning).
_CONTEXT_URL_TOKENS: frozenset[str] = frozenset(
    {
        "url",
        "uri",
        "endpoint",
        "tokenurl",
        "discoveryurl",
        "authorizationurl",
        "issuer",
        "issuerurl",
        "baseurl",
        "endpointurl",
    }
)
#: A reference names a stored secret: ``*Ref`` / ``*Arn`` whose value is an AWS ARN.
_REFERENCE_SUFFIXES: tuple[str, ...] = ("ref", "arn")
_ARN_RE = re.compile(r"^arn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:\d{0,12}:.+$")
_AUTH_SCHEME_RE = re.compile(r"^\s*(bearer|basic|token|digest|apikey|api-key)\s+\S", re.I)
_PLAIN_SCALAR_RE = re.compile(r"^[A-Za-z0-9._:/+=@-]{0,256}$")

_NON_ALNUM = re.compile(r"[^a-z0-9]")


def canonical_key(key: str) -> str:
    """Lower-case *key* and strip every non-alphanumeric character."""
    return _NON_ALNUM.sub("", key.lower())


def credential_token_for(key: object) -> str | None:
    """The canonical token *key* names a credential value under, or ``None``.

    Exact membership first (``apiKey`` -> ``apikey``), then the F-11 prefixed form
    (``OPENAI_API_KEY`` -> ``openaiapikey`` ends with ``apikey``). A reference or locator never
    matches: ``apiKeyRef`` ends with ``ref``, ``tokenEndpoint`` with ``endpoint``.
    """
    if not isinstance(key, str):
        return None
    canon = canonical_key(key)
    if canon in WRITE_ONLY_CREDENTIAL_TOKENS:
        return canon
    if canon in _PREFIXED_TOKEN_EXEMPTIONS:
        return None
    for token in _PREFIX_MATCH_TOKENS:
        if canon.endswith(token):
            return token
    return None


def is_write_only_credential_key(key: object) -> bool:
    """True when *key* names a credential VALUE (any casing, separator or prefix), never a reference."""
    return credential_token_for(key) is not None


def _flag_or_count(key: str, value: Any) -> bool:
    """A non-string scalar under a PREFIXED credential name is metadata, not a credential.

    ``showPassword: true``, ``requiresApiKey: false``, ``max_token: 4096`` are UI and model
    configuration; dropping them would silently change behaviour. The allowance is for the
    prefixed class only: an exact token (``password: 1234``) drops every scalar, as before.
    """
    if canonical_key(key) in WRITE_ONLY_CREDENTIAL_TOKENS:
        return False
    return value is None or isinstance(value, (bool, int, float))


def _is_container_key(key: object) -> bool:
    return isinstance(key, str) and canonical_key(key) in CREDENTIAL_CONTAINER_TOKENS


def _is_reference_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    canon = canonical_key(key)
    return canon not in WRITE_ONLY_CREDENTIAL_TOKENS and canon.endswith(_REFERENCE_SUFFIXES)


def _valid_reference(value: Any) -> bool:
    return isinstance(value, str) and bool(_ARN_RE.match(value.strip()))


def _valid_context_url(value: Any) -> bool:
    """An ORIGIN-ONLY https URL. Inside a credential container a path segment is indistinguishable
    from a token by shape, and no path of this application's own credential objects lives there,
    so a URL with a path, query, fragment or userinfo is dropped -- never truncated -- as a whole.
    """
    if not isinstance(value, str):
        return False
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return False
    if parts.scheme.lower() != "https" or not parts.netloc:
        return False
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return False
    return parts.path in ("", "/") and not parts.query and not parts.fragment


def _valid_context_scalar(value: Any) -> bool:
    """A metadata scalar: short, no whitespace, no auth scheme -- ``clientId``, ``region``, ``jsonKey``."""
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return True
    if not isinstance(value, str):
        return False
    return bool(_PLAIN_SCALAR_RE.match(value)) and not _AUTH_SCHEME_RE.match(value)


def _under_value_key(value: Any, path: str, removed: list[str]) -> Any:
    """What survives under a VALUE key: validated references only."""
    if isinstance(value, dict):
        out: dict = {}
        for k, v in value.items():
            here = f"{path}/{k}"
            if _is_reference_key(k) and _valid_reference(v):
                out[k] = v
            elif isinstance(v, (dict, list)):
                out[k] = _under_value_key(v, here, removed)
            else:
                removed.append(here)
        return out
    if isinstance(value, list):
        kept = []
        for i, item in enumerate(value):
            here = f"{path}[{i}]"
            if isinstance(item, (dict, list)):
                kept.append(_under_value_key(item, here, removed))
            else:
                removed.append(here)
        return kept
    removed.append(path)
    return None


def _in_container(value: Any, path: str, removed: list[str]) -> Any:
    """What survives inside a container: allowlisted metadata and validated references."""
    if isinstance(value, dict):
        out: dict = {}
        for k, v in value.items():
            here = f"{path}/{k}"
            if not isinstance(k, str):
                removed.append(here)
                continue
            canon = canonical_key(k)
            if is_write_only_credential_key(k):
                if isinstance(v, (dict, list)):
                    out[k] = _under_value_key(v, here, removed)
                elif _flag_or_count(k, v):
                    out[k] = v
                else:
                    removed.append(here)
            elif isinstance(v, dict):
                out[k] = _in_container(v, here, removed)
            elif _is_reference_key(k):
                if _valid_reference(v):
                    out[k] = v
                else:
                    removed.append(here)
            elif canon in _CONTEXT_URL_TOKENS or canon.endswith(("url", "uri", "endpoint")):
                if _valid_context_url(v):
                    out[k] = v
                else:
                    removed.append(here)
            elif canon in _CONTEXT_LIST_TOKENS and isinstance(v, list):
                kept = [item for item in v if _valid_context_scalar(item)]
                removed.extend(f"{here}[{i}]" for i, item in enumerate(v) if not _valid_context_scalar(item))
                out[k] = kept
            elif isinstance(v, list):
                # An unrecognised list keeps only its object members; with none left it is dropped
                # whole, so an unknown leaf never survives as an empty shell either.
                kept_objects = _in_container(v, here, removed)
                if kept_objects:
                    out[k] = kept_objects
            elif canon in _CONTEXT_METADATA_TOKENS and _valid_context_scalar(v):
                out[k] = v
            else:
                removed.append(here)
        return out
    if isinstance(value, list):
        kept = []
        for i, item in enumerate(value):
            here = f"{path}[{i}]"
            if isinstance(item, (dict, list)):
                kept.append(_in_container(item, here, removed))
            else:
                removed.append(here)
        return kept
    removed.append(path)
    return None


def _walk(value: Any, path: str, removed: list[str]) -> Any:
    if isinstance(value, dict):
        out: dict = {}
        for k, v in value.items():
            here = f"{path}/{k}"
            if is_write_only_credential_key(k):
                if isinstance(v, (dict, list)):
                    out[k] = _under_value_key(v, here, removed)
                elif _flag_or_count(k, v):
                    out[k] = v
                else:
                    removed.append(here)
                continue
            if _is_container_key(k):
                if isinstance(v, (dict, list)):
                    out[k] = _in_container(v, here, removed)
                else:
                    removed.append(here)
                continue
            out[k] = _walk(v, here, removed)
        return out
    if isinstance(value, list):
        return [_walk(v, f"{path}[{i}]", removed) for i, v in enumerate(value)]
    return value


def scrub_write_only_credentials(value: Any, *, _path: str = "", _removed: list[str] | None = None) -> Any:
    """Deep copy of *value* with every write-only credential removed at any depth.

    Logs the COUNT and the canonical field names that matched -- never a value, and never a path:
    an ancestor key is caller-controlled text and could itself be the secret. The names are drawn
    from the closed vocabularies above.
    """
    removed: list[str] = [] if _removed is None else _removed
    result = _walk(value, _path, removed)
    if _removed is None and removed:
        # A prefixed key (``ACME_PROD_GITHUB_TOKEN``) is reported as its canonical token
        # (``token``): the prefix is caller text and stays out of the log.
        matched = sorted(
            {
                name
                for path in removed
                for part in path.split("/")
                if part
                for name in (credential_token_for(part) or (canonical_key(part) if _is_container_key(part) else None),)
                if name
            }
        )
        logger.warning(
            "Dropped %d write-only credential field(s) from a persisted record (fields: %s)",
            len(removed),
            ", ".join(matched) or "under-credential leaves",
        )
    return result


def strip_credential_leaves(value: Any) -> Any:
    """Deep copy of *value* with every string under a credential-value key removed, and nothing else.

    The RESPONSE-boundary counterpart of :func:`scrub_write_only_credentials` (F-02). A served
    record's structure is the caller's, so there are no container semantics here: a ``credentials``
    object keeps ``status`` and ``provider_name`` whatever they are called. Only a leaf whose own key
    names a credential value goes (``client_info.client_secret``, ``headers.X-Api-Key``,
    ``env.GITHUB_TOKEN``); an object under such a key keeps its validated references, exactly as the
    persistence scrub does. Flags and counters under a prefixed name are kept by value shape.

    ARCC cnt_dwzZ05hLnqhYXQ: a secret in an API response is the anti-pattern, whatever row shape
    put it there; cnt_n8LpZcqYi2t3I2: the reference (``client_secret_ref``) is what a response may
    carry, and it survives here by construction (it ends with ``ref``, not a credential token).
    """
    if isinstance(value, dict):
        out: dict = {}
        for k, v in value.items():
            if is_write_only_credential_key(k):
                if isinstance(v, (dict, list)):
                    out[k] = _under_value_key(v, "", [])
                elif _flag_or_count(k, v):
                    out[k] = v
                continue
            out[k] = strip_credential_leaves(v)
        return out
    if isinstance(value, list):
        return [strip_credential_leaves(v) for v in value]
    return value


def contains_write_only_credential(value: Any) -> bool:
    """True when persisting *value* as-is would store a credential value."""
    removed: list[str] = []
    _walk(value, "", removed)
    return bool(removed)
