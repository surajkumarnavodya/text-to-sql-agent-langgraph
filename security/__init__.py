"""Shared, dependency-free security primitives used by both `agent/` and `db/`.

Deliberately a peer package to `agent/`/`db/`, not nested inside either: text
sanitization is needed at two independent points that must not depend on
each other -- untrusted *user* input (`agent/input_guard.py`) and untrusted
*database* content flowing into a prompt (`db/schema_introspection.py`,
`db/value_sampling.py`). Putting the shared logic here, with zero imports
from either `agent` or `db`, is what keeps it a true leaf module usable by
both without creating a dependency cycle or making one layer's security
code secretly depend on the other's.
"""
