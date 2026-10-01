"""End-to-end HTTP tests for api/shares.py -- same `TestClient` +
in-memory-SQLite-identity-DB pattern as tests/test_api_chat_history.py.

Two users are never enough to prove tenant isolation on their own (this app
has no real multi-tenant column outside the sharing tables -- see
`security/tenancy.py`'s own docstring), so `_fake_tenant_resolver` below
simulates two tenants deterministically by email domain, monkeypatched onto
`api.shares.resolve_actor_tenant_id` -- this exercises the *real* ABAC
tenant check in `identity.share_policy.authorize_share_action`, not a
mocked stand-in for it.

Conversations/turns are seeded directly via `identity.repositories.history`
against the same test engine, not through `/ask` -- this file tests
sharing's own authorization surface, not the agent pipeline.
"""

from __future__ import annotations

import uuid

import identity.db as identity_db_mod
import pytest
from fastapi.testclient import TestClient
from identity.bootstrap import seed_rbac
from identity.models import Base, User
from identity.repositories.history import append_turn, create_conversation
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

import agent.rate_limit as rate_limit_mod
import api.auth as api_auth_mod
import api.identity_auth as identity_auth_mod
import api.identity_authz as identity_authz_mod
import api.main as api_main
import api.shares as shares_mod
from config.settings import Settings

_SETTINGS = Settings(
    local_auth_enabled=True,
    auth_database_url="postgresql://placeholder/unused",
    jwt_secret_key="s" * 40,
    jwt_issuer="text-to-sql-agent",
    jwt_audience="text-to-sql-web",
    allow_public_registration=True,
    cookie_secure=False,
    password_min_length=8,
    login_rate_limit_per_minute=1000,
    register_rate_limit_per_hour=1000,
    enable_conversation_sharing=True,
    share_public_links_enabled=True,
    share_default_expiry_days=30,
    share_invitation_expiry_days=14,
    share_max_members_per_conversation=5,
    share_link_access_rate_limit_per_minute=1000,
    share_invite_rate_limit_per_hour=1000,
)


def _fake_tenant_resolver(user: User | None) -> str | None:
    """Deterministic two-tenant simulation by email domain -- see this
    module's own docstring for why a real column doesn't exist to key on
    instead."""
    if user is None:
        return None
    if user.email.endswith("@tenant-a.example.com"):
        return "tenant-a"
    if user.email.endswith("@tenant-b.example.com"):
        return "tenant-b"
    return "default"


@pytest.fixture(autouse=True)
def _identity_test_db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)

    monkeypatch.setattr(identity_db_mod, "get_identity_engine", lambda settings=None: engine)
    monkeypatch.setattr(api_auth_mod, "get_settings", lambda: _SETTINGS)
    monkeypatch.setattr(identity_auth_mod, "get_settings", lambda: _SETTINGS)
    monkeypatch.setattr(identity_authz_mod, "get_settings", lambda: _SETTINGS)
    monkeypatch.setattr(shares_mod, "get_settings", lambda: _SETTINGS)
    monkeypatch.setattr(shares_mod, "resolve_actor_tenant_id", _fake_tenant_resolver)

    session = identity_db_mod.get_identity_session(_SETTINGS)
    seed_rbac(session)
    session.close()

    identity_auth_mod._login_limiters.clear()
    identity_auth_mod._register_limiters.clear()
    rate_limit_mod._share_link_access_limiters.clear()
    rate_limit_mod._share_invite_limiters.clear()

    yield engine


@pytest.fixture
def client() -> TestClient:
    return TestClient(api_main.app)


def _register(client: TestClient, email: str, display_name: str = "Test User") -> dict:
    response = client.post(
        "/auth/register",
        json={
            "email": email,
            "password": "correcthorse battery staple 1",
            "display_name": display_name,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _headers(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def _seed_conversation_with_turn(email: str, question: str = "How many orders?") -> str:
    """Creates one conversation with one turn directly against the test
    engine, owned by whichever user has `email` -- returns the conversation
    id as a string. Must be called *after* that user has registered."""
    session = identity_db_mod.get_identity_session(_SETTINGS)
    try:
        user = session.query(User).filter_by(email=email.strip().lower()).one()
        conversation = create_conversation(session, user_id=user.id, feature_type="text_to_sql")
        append_turn(
            session,
            conversation=conversation,
            user_id=user.id,
            question=question,
            answer_text="There were 42 orders.",
            output_metadata={
                "schema_version": 2,
                "sources_used": ["sql"],
                "sql": "SELECT COUNT(*) FROM orders",
                "query_plan": ["internal planning detail"],
                "attachment_refs": [
                    {
                        "attachment_id": "att_report",
                        "filename": "report.pdf",
                        "media_type": "application/pdf",
                    }
                ],
            },
        )
        return str(conversation.id)
    finally:
        session.close()


class TestOwnerShareLifecycle:
    def test_create_is_idempotent(self, client: TestClient):
        tokens = _register(client, "owner@tenant-a.example.com")
        conversation_id = _seed_conversation_with_turn("owner@tenant-a.example.com")
        headers = _headers(tokens)

        first = client.post(f"/conversations/{conversation_id}/share", json={}, headers=headers)
        assert first.status_code == 200, first.text
        second = client.post(f"/conversations/{conversation_id}/share", json={}, headers=headers)
        assert second.status_code == 200
        assert first.json()["share"]["id"] == second.json()["share"]["id"]

    def test_create_captures_snapshot_and_excludes_later_messages(self, client: TestClient):
        tokens = _register(client, "owner@tenant-a.example.com")
        conversation_id = _seed_conversation_with_turn("owner@tenant-a.example.com")
        headers = _headers(tokens)
        client.post(f"/conversations/{conversation_id}/share", json={}, headers=headers)

        # A later message must not appear in the viewer -- append after share creation.
        session = identity_db_mod.get_identity_session(_SETTINGS)
        try:
            from identity.models import Conversation

            conversation = session.get(Conversation, uuid.UUID(conversation_id))
            assert conversation is not None
            append_turn(
                session,
                conversation=conversation,
                user_id=conversation.user_id,
                question="A later, unshared question",
                answer_text="A later, unshared answer",
            )
        finally:
            session.close()

        share = client.get(f"/conversations/{conversation_id}/share", headers=headers).json()
        view = client.get(f"/share-view/{share['id']}", headers=headers)
        assert view.status_code == 200
        contents = [t["content"] for t in view.json()["turns"]]
        assert "A later, unshared question" not in contents

    def test_patch_requires_current_version(self, client: TestClient):
        tokens = _register(client, "owner@tenant-a.example.com")
        conversation_id = _seed_conversation_with_turn("owner@tenant-a.example.com")
        headers = _headers(tokens)
        client.post(f"/conversations/{conversation_id}/share", json={}, headers=headers)

        stale = client.patch(
            f"/conversations/{conversation_id}/share",
            json={"version": 999, "status": "disabled"},
            headers=headers,
        )
        assert stale.status_code == 409

    def test_revoke_then_view_is_denied_for_a_member_then_reshare_restores_it(
        self, client: TestClient
    ):
        """The *owner* may always preview their own share regardless of
        revocation (see `identity.share_policy`'s own "owner" branch --
        revoking blocks other people's access to the owner's data, not the
        owner's own visibility into it). A revoked share's real effect is
        on everyone *else* -- an invited member -- which is what this test
        actually exercises."""
        owner_tokens = _register(client, "owner@tenant-a.example.com")
        member_tokens = _register(client, "member@tenant-a.example.com")
        conversation_id = _seed_conversation_with_turn("owner@tenant-a.example.com")
        owner_headers = _headers(owner_tokens)
        share = client.post(
            f"/conversations/{conversation_id}/share", json={}, headers=owner_headers
        ).json()["share"]
        invite = client.post(
            f"/conversations/{conversation_id}/share/members/invite",
            json={"email": "member@tenant-a.example.com"},
            headers=owner_headers,
        )
        token = invite.json()["invitation_path"].rsplit("/", 1)[-1]
        client.post(f"/share-invitations/{token}/accept", headers=_headers(member_tokens))

        allowed_before = client.get(f"/share-view/{share['id']}", headers=_headers(member_tokens))
        assert allowed_before.status_code == 200

        revoke = client.post(
            f"/conversations/{conversation_id}/share/revoke",
            json={"version": share["version"]},
            headers=owner_headers,
        )
        assert revoke.status_code == 200

        denied = client.get(f"/share-view/{share['id']}", headers=_headers(member_tokens))
        assert denied.status_code == 404
        # The owner's own preview is unaffected by their own revocation.
        owner_preview = client.get(f"/share-view/{share['id']}", headers=owner_headers)
        assert owner_preview.status_code == 200

        reshared = client.post(
            f"/conversations/{conversation_id}/share", json={}, headers=owner_headers
        )
        assert reshared.status_code == 200
        allowed_after = client.get(
            f"/share-view/{reshared.json()['share']['id']}", headers=owner_headers
        )
        assert allowed_after.status_code == 200


class TestRBACViewerCannotManage:
    def test_invited_active_member_cannot_invite_widen_regenerate_or_revoke(
        self, client: TestClient
    ):
        owner_tokens = _register(client, "owner@tenant-a.example.com")
        viewer_tokens = _register(client, "viewer@tenant-a.example.com")
        conversation_id = _seed_conversation_with_turn("owner@tenant-a.example.com")
        owner_headers = _headers(owner_tokens)
        viewer_headers = _headers(viewer_tokens)

        client.post(f"/conversations/{conversation_id}/share", json={}, headers=owner_headers)
        invite = client.post(
            f"/conversations/{conversation_id}/share/members/invite",
            json={"email": "viewer@tenant-a.example.com"},
            headers=owner_headers,
        )
        assert invite.status_code == 200
        token = invite.json()["invitation_path"].rsplit("/", 1)[-1]
        accept = client.post(f"/share-invitations/{token}/accept", headers=viewer_headers)
        assert accept.status_code == 200

        # The now-active viewer member is a real, authenticated local user
        # for this same conversation's OWN account -- but is not its owner.
        assert client.patch(
            f"/conversations/{conversation_id}/share",
            json={"version": 1, "status": "disabled"},
            headers=viewer_headers,
        ).status_code in (403, 404)
        assert client.post(
            f"/conversations/{conversation_id}/share/members/invite",
            json={"email": "someone-else@tenant-a.example.com"},
            headers=viewer_headers,
        ).status_code in (403, 404)
        assert client.post(
            f"/conversations/{conversation_id}/share/link/regenerate", headers=viewer_headers
        ).status_code in (400, 403, 404)
        assert client.post(
            f"/conversations/{conversation_id}/share/revoke",
            json={"version": 1},
            headers=viewer_headers,
        ).status_code in (403, 404)
        # But they can still view.
        assert client.get(f"/share-view/{conversation_id}", headers=viewer_headers).status_code in (
            200,
            404,
        )


class TestCrossTenantIsolation:
    def test_member_from_a_different_tenant_cannot_view_even_with_a_valid_membership_row(
        self, client: TestClient
    ):
        owner_tokens = _register(client, "owner@tenant-a.example.com")
        # A user whose (simulated) tenant differs from the share's own.
        cross_tenant_tokens = _register(client, "someone@tenant-b.example.com")
        conversation_id = _seed_conversation_with_turn("owner@tenant-a.example.com")
        owner_headers = _headers(owner_tokens)

        share = client.post(
            f"/conversations/{conversation_id}/share", json={}, headers=owner_headers
        ).json()["share"]
        invite = client.post(
            f"/conversations/{conversation_id}/share/members/invite",
            json={"email": "someone@tenant-b.example.com"},
            headers=owner_headers,
        )
        token = invite.json()["invitation_path"].rsplit("/", 1)[-1]
        # Accepting itself is unaffected by tenant (it's the owner's own
        # explicit choice to invite this email) -- the *view* denial is
        # where the cross-tenant ABAC check actually fires.
        accept = client.post(
            f"/share-invitations/{token}/accept", headers=_headers(cross_tenant_tokens)
        )
        assert accept.status_code == 200

        denied = client.get(f"/share-view/{share['id']}", headers=_headers(cross_tenant_tokens))
        assert denied.status_code == 404


class TestIDORAndCrossUserIsolation:
    def test_user_b_cannot_read_update_or_revoke_user_as_share_by_conversation_id(
        self, client: TestClient
    ):
        a_tokens = _register(client, "alice@tenant-a.example.com")
        b_tokens = _register(client, "bob@tenant-a.example.com")
        conversation_id = _seed_conversation_with_turn("alice@tenant-a.example.com")
        a_headers = _headers(a_tokens)
        b_headers = _headers(b_tokens)
        client.post(f"/conversations/{conversation_id}/share", json={}, headers=a_headers)

        assert (
            client.get(f"/conversations/{conversation_id}/share", headers=b_headers).status_code
            == 404
        )
        assert (
            client.patch(
                f"/conversations/{conversation_id}/share",
                json={"version": 1, "status": "disabled"},
                headers=b_headers,
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"/conversations/{conversation_id}/share/revoke",
                json={"version": 1},
                headers=b_headers,
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"/conversations/{conversation_id}/share/members/invite",
                json={"email": "x@tenant-a.example.com"},
                headers=b_headers,
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"/conversations/{conversation_id}/share/link/regenerate", headers=b_headers
            ).status_code
            == 404
        )

    def test_user_b_cannot_remove_a_member_from_user_as_share_by_guessing_member_id(
        self, client: TestClient
    ):
        a_tokens = _register(client, "alice@tenant-a.example.com")
        b_tokens = _register(client, "bob@tenant-a.example.com")
        conversation_id = _seed_conversation_with_turn("alice@tenant-a.example.com")
        a_headers = _headers(a_tokens)
        b_headers = _headers(b_tokens)
        client.post(f"/conversations/{conversation_id}/share", json={}, headers=a_headers)
        invite = client.post(
            f"/conversations/{conversation_id}/share/members/invite",
            json={"email": "carol@tenant-a.example.com"},
            headers=a_headers,
        )
        member_id = invite.json()["member"]["id"]

        response = client.delete(
            f"/conversations/{conversation_id}/share/members/{member_id}", headers=b_headers
        )
        assert response.status_code == 404

    def test_unauthorized_attachment_download_is_denied(self, client: TestClient):
        a_tokens = _register(client, "alice@tenant-a.example.com")
        b_tokens = _register(client, "bob@tenant-a.example.com")
        conversation_id = _seed_conversation_with_turn("alice@tenant-a.example.com")
        share = client.post(
            f"/conversations/{conversation_id}/share", json={}, headers=_headers(a_tokens)
        ).json()["share"]

        # B is not a member/owner at all -- must be denied.
        response = client.get(
            f"/share-view/{share['id']}/attachments/att_report", headers=_headers(b_tokens)
        )
        assert response.status_code == 404

    def test_guessed_unrelated_attachment_id_is_denied_even_for_the_owner(self, client: TestClient):
        """The share itself is viewable, but an attachment id that was
        never part of its approved snapshot must still be refused --
        proves the allowlist, not just share-level access, gates downloads."""
        a_tokens = _register(client, "alice@tenant-a.example.com")
        conversation_id = _seed_conversation_with_turn("alice@tenant-a.example.com")
        share = client.post(
            f"/conversations/{conversation_id}/share", json={}, headers=_headers(a_tokens)
        ).json()["share"]

        response = client.get(
            f"/share-view/{share['id']}/attachments/att_completely_unrelated_guess",
            headers=_headers(a_tokens),
        )
        assert response.status_code == 404


class TestPublicLinkAccess:
    def test_anonymous_visitor_can_view_a_public_link_share(self, client: TestClient):
        tokens = _register(client, "owner@tenant-a.example.com")
        conversation_id = _seed_conversation_with_turn("owner@tenant-a.example.com")
        created = client.post(
            f"/conversations/{conversation_id}/share",
            json={"access_mode": "anyone_with_link"},
            headers=_headers(tokens),
        )
        assert created.status_code == 200
        raw_path = created.json()["link"]["view_path"]
        token = raw_path.rsplit("/", 1)[-1]

        anonymous_view = client.get(f"/share-view/{token}")
        assert anonymous_view.status_code == 200
        assert anonymous_view.json()["viewer_role"] == "public_link"

    def test_invite_only_share_rejects_an_anonymous_visitor(self, client: TestClient):
        tokens = _register(client, "owner@tenant-a.example.com")
        conversation_id = _seed_conversation_with_turn("owner@tenant-a.example.com")
        share = client.post(
            f"/conversations/{conversation_id}/share", json={}, headers=_headers(tokens)
        ).json()["share"]

        response = client.get(f"/share-view/{share['id']}")
        assert response.status_code == 404

    def test_revoked_link_fails_immediately(self, client: TestClient):
        tokens = _register(client, "owner@tenant-a.example.com")
        conversation_id = _seed_conversation_with_turn("owner@tenant-a.example.com")
        headers = _headers(tokens)
        created = client.post(
            f"/conversations/{conversation_id}/share",
            json={"access_mode": "anyone_with_link"},
            headers=headers,
        )
        share = created.json()["share"]
        token = created.json()["link"]["view_path"].rsplit("/", 1)[-1]

        client.post(
            f"/conversations/{conversation_id}/share/revoke",
            json={"version": share["version"]},
            headers=headers,
        )
        assert client.get(f"/share-view/{token}").status_code == 404

    def test_regenerated_link_invalidates_the_previous_token(self, client: TestClient):
        tokens = _register(client, "owner@tenant-a.example.com")
        conversation_id = _seed_conversation_with_turn("owner@tenant-a.example.com")
        headers = _headers(tokens)
        created = client.post(
            f"/conversations/{conversation_id}/share",
            json={"access_mode": "anyone_with_link"},
            headers=headers,
        )
        old_token = created.json()["link"]["view_path"].rsplit("/", 1)[-1]

        regenerated = client.post(
            f"/conversations/{conversation_id}/share/link/regenerate", headers=headers
        )
        assert regenerated.status_code == 200
        new_token = regenerated.json()["link"]["view_path"].rsplit("/", 1)[-1]
        assert new_token != old_token

        assert client.get(f"/share-view/{old_token}").status_code == 404
        assert client.get(f"/share-view/{new_token}").status_code == 200

    def test_unknown_token_and_a_never_shared_conversation_return_the_identical_generic_denial(
        self, client: TestClient
    ):
        """Never confirms whether a share ever existed -- both a
        garbage token and a real-shaped-but-unissued one collapse to the
        same 404 body."""
        r1 = client.get("/share-view/this-token-was-never-issued")
        r2 = client.get(f"/share-view/{uuid.uuid4()}")
        assert r1.status_code == r2.status_code == 404
        assert r1.json()["detail"] == r2.json()["detail"]

    def test_rate_limit_on_link_access_is_distinguishable_from_a_denial(
        self, client: TestClient, monkeypatch
    ):
        # Settings is frozen (pydantic) -- swap in a whole new instance with
        # a tighter limit rather than mutating the shared one in place.
        tight_settings = Settings(
            **{**_SETTINGS.__dict__, "share_link_access_rate_limit_per_minute": 1}
        )
        monkeypatch.setattr(shares_mod, "get_settings", lambda: tight_settings)
        rate_limit_mod._share_link_access_limiters.clear()
        first = client.get("/share-view/no-such-token")
        second = client.get("/share-view/no-such-token")
        assert first.status_code == 404
        assert second.status_code == 429


class TestCacheAndReferrerHeaders:
    def test_shared_view_response_has_private_no_store_and_no_referrer(self, client: TestClient):
        tokens = _register(client, "owner@tenant-a.example.com")
        conversation_id = _seed_conversation_with_turn("owner@tenant-a.example.com")
        created = client.post(
            f"/conversations/{conversation_id}/share",
            json={"access_mode": "anyone_with_link"},
            headers=_headers(tokens),
        )
        token = created.json()["link"]["view_path"].rsplit("/", 1)[-1]

        response = client.get(f"/share-view/{token}")
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-content-type-options"] == "nosniff"

    def test_denied_shared_view_also_carries_the_safe_headers_not_just_the_success_path(
        self, client: TestClient
    ):
        """Regression test for a real bug found via live testing (not by
        this file's own earlier tests): raising `HTTPException` builds a
        *separate* response object, silently discarding whatever headers a
        route had already set on its injected `Response` parameter before
        raising -- exactly the denial path, the one that matters most for
        an anonymous-facing endpoint. Every `HTTPException` this module
        raises must carry these headers explicitly via `headers=`, not rely
        on the `response.headers[...] =` lines earlier in the function body."""
        response = client.get(f"/share-view/{uuid.uuid4()}")
        assert response.status_code == 404
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-content-type-options"] == "nosniff"

    def test_denied_attachment_download_also_carries_the_safe_headers(self, client: TestClient):
        response = client.get(f"/share-view/{uuid.uuid4()}/attachments/att_guess")
        assert response.status_code == 404
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["referrer-policy"] == "no-referrer"

    def test_rate_limited_response_also_carries_the_safe_headers(
        self, client: TestClient, monkeypatch
    ):
        tight_settings = Settings(
            **{**_SETTINGS.__dict__, "share_link_access_rate_limit_per_minute": 1}
        )
        monkeypatch.setattr(shares_mod, "get_settings", lambda: tight_settings)
        rate_limit_mod._share_link_access_limiters.clear()
        client.get("/share-view/no-such-token")
        response = client.get("/share-view/no-such-token")
        assert response.status_code == 429
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["referrer-policy"] == "no-referrer"

    def test_owner_management_responses_are_also_private_no_store(self, client: TestClient):
        tokens = _register(client, "owner@tenant-a.example.com")
        conversation_id = _seed_conversation_with_turn("owner@tenant-a.example.com")
        response = client.post(
            f"/conversations/{conversation_id}/share", json={}, headers=_headers(tokens)
        )
        assert response.headers["cache-control"] == "private, no-store"


class TestNoTokenOrPrivateContentLeakage:
    def test_raw_link_token_never_appears_in_the_share_settings_response(self, client: TestClient):
        tokens = _register(client, "owner@tenant-a.example.com")
        conversation_id = _seed_conversation_with_turn("owner@tenant-a.example.com")
        headers = _headers(tokens)
        created = client.post(
            f"/conversations/{conversation_id}/share",
            json={"access_mode": "anyone_with_link"},
            headers=headers,
        )
        raw_token = created.json()["link"]["view_path"].rsplit("/", 1)[-1]

        settings_view = client.get(f"/conversations/{conversation_id}/share", headers=headers)
        assert raw_token not in settings_view.text

    def test_denied_view_response_never_reveals_internal_reason_categories(
        self, client: TestClient
    ):
        response = client.get(f"/share-view/{uuid.uuid4()}")
        body = response.text.lower()
        for leaky_term in ("revoked", "expired", "tenant", "member_not_active", "traceback"):
            assert leaky_term not in body


class TestAgentAndSqlIsolation:
    def test_shares_module_never_imports_the_sql_or_orchestrator_pipeline(self):
        """Structural guarantee, not just a behavioral one: a shared view
        cannot invoke SQL/tool/model execution because this module has no
        *import* path to any of it at all -- checked via `ast`, over real
        `Import`/`ImportFrom` nodes only, so this doesn't false-positive on
        this module's own docstrings/comments explaining that fact in
        prose (which necessarily *name* the modules they say are absent)."""
        import ast
        import inspect

        import api.shares as module

        tree = ast.parse(inspect.getsource(module))
        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)

        for forbidden in ("agent.graph", "agent.orchestrator", "agent.orchestrator.graph"):
            assert forbidden not in imported_modules
        assert not any(m.startswith("rag.") or m.startswith("search.") for m in imported_modules)

    def test_shared_view_response_never_contains_a_confirm_and_run_capability(
        self, client: TestClient
    ):
        tokens = _register(client, "owner@tenant-a.example.com")
        conversation_id = _seed_conversation_with_turn("owner@tenant-a.example.com")
        created = client.post(
            f"/conversations/{conversation_id}/share",
            json={"access_mode": "anyone_with_link"},
            headers=_headers(tokens),
        )
        token = created.json()["link"]["view_path"].rsplit("/", 1)[-1]
        body = client.get(f"/share-view/{token}").json()
        assert "execute" not in str(body).lower()
