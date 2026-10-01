"""Client-database onboarding engine -- Prompt 08
(`08_ONBOARDING_ENGINE_CONTRACT.md`).

Pure-logic pipeline stages (this package) are fully independent of
`identity/`'s persistence layer and FastAPI -- every module here can be
unit-tested with plain Python objects, no database, no HTTP. `identity
/repositories/onboarding.py` is the persistence layer; `api/onboarding.py`
is the REST surface wiring the two together. See each module's own
docstring for its exact scope.
"""
