"""Response/request models for `api/navigation.py` -- Prompt 32."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class NavItemOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    path: str
    group: str


class NavigationOut(BaseModel):
    """Everything the frontend needs to draw navigation and guard routes:
    the screens this caller may open, and the actions they may offer. Both
    come from the same server-side decision (`security.navigation`)."""

    model_config = ConfigDict(frozen=True)

    items: list[NavItemOut]
    capabilities: dict[str, bool]
    roles: list[str]
    #: The tenant this caller is scoped to, resolved server-side. `None` for a
    #: caller with no local account (OIDC, static token, auth off).
    tenant_id: str | None


class AccessDeniedReport(BaseModel):
    """A signed-in caller reporting that they were sent to a screen they may
    not use. `path` is accepted only if it matches a real screen; anything
    else is ignored, so this endpoint cannot be used to write arbitrary text
    into the audit log."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    path: str = Field(min_length=1, max_length=200)
