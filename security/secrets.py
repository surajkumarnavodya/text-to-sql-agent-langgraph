"""Re-exports Pydantic's `SecretStr` as this project's one secret-value type.

Previously a hand-rolled `str` subclass that redacted only `repr()`/`%r`
while staying fully transparent everywhere else (equality, `str()`,
f-string interpolation, even `.upper()`) -- convenient, but exactly that
transparency was the risk: any code path that turned a `Settings` field
into a plain string (`str(x)`, an f-string, `json.dumps` on a naive
`dataclasses.asdict()` dump) silently produced the real secret.

`pydantic.SecretStr` closes that gap by being *narrower*, not just
differently-redacted: `str()`, `repr()`, and `%r`-formatting all mask it
("**********"); only `.get_secret_value()` returns the real value, and it
integrates with Pydantic's own model validation/serialization (used
throughout `config/settings.py` and `api/schemas.py`) so a `Settings`
instance can never accidentally leak a secret through `.model_dump()`/
`.model_dump_json()` either -- a real gap the old transparent-`str`
version had no protection against at all.

The real cost: code that relied on the old transparent behavior must be
explicit now. Every call site that needs the actual value (building a
SQLAlchemy connection URL, a bearer-token comparison, a redaction
search-string, a third-party API payload) must call `.get_secret_value()`
-- verified against every such call site in this codebase as part of this
migration (`db/connection.py`, `api/auth.py`, `rag/store.py`,
`search/web_search.py`, `security/redaction.py`); a leftover `str(secret)`
anywhere would silently start sending/comparing/searching for the literal
string "**********" instead of the real value, which is exactly the kind
of bug this migration's own review had to catch by hand, not something
either version of `SecretStr` prevents automatically.
"""

from __future__ import annotations

from pydantic import SecretStr

__all__ = ["SecretStr"]
