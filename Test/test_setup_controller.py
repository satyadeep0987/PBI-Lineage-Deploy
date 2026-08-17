"""Offline regression coverage for session-only setup and authentication."""

from __future__ import annotations

import inspect
import unittest

from pbi_modules.claude_agent import ClaudeConfigurationError, resolve_claude_settings
from pbi_modules import setup_controller as setup


TENANT_ID = "11111111-1111-1111-1111-111111111111"
CLIENT_ID = "22222222-2222-2222-2222-222222222222"
WORKSPACE_ID = "33333333-3333-3333-3333-333333333333"
REPORT_ID = "44444444-4444-4444-4444-444444444444"
SEMANTIC_WORKSPACE_ID = "55555555-5555-5555-5555-555555555555"


def _raw_setup(claude_mode="single"):
    raw = {
        "powerbi": {
            "tenant_id": TENANT_ID,
            "client_id": CLIENT_ID,
            "workspace_id": WORKSPACE_ID,
            "report_ids": REPORT_ID,
        },
        "snowflake": {
            "account": "org-account",
            "database": "ANALYTICS",
            "warehouse": "LINEAGE_WH",
            "role": "LINEAGE_READER",
            "user": "reader@example.com",
            "read_only_confirmed": True,
        },
        "claude": {
            "mode": claude_mode,
            "api_key": "sk-ant-test-session-key",
        },
    }
    if claude_mode == "managed_multi":
        raw["claude"]["environment_id"] = "env_test"
        for field in setup.MANAGED_AGENT_FIELDS:
            raw["claude"][field] = f"agent_{field}"
    return raw


class _FakeCache:
    def __init__(self):
        self.value = ""

    def deserialize(self, value):
        self.value = str(value)

    def serialize(self):
        return self.value or "memory-cache"


class _FakeMsalApp:
    last_instance = None

    def __init__(self, client_id, authority, token_cache):
        self.client_id = client_id
        self.authority = authority
        self.token_cache = token_cache
        self.started = None
        _FakeMsalApp.last_instance = self

    def get_accounts(self, username=None):
        return [
            {
                "home_account_id": "home-account",
                "tenant_id": TENANT_ID,
                "local_account_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "username": username or "reader@example.com",
            }
        ]

    def initiate_auth_code_flow(self, **kwargs):
        self.started = dict(kwargs)
        return {
            "auth_uri": "https://login.microsoftonline.com/authorize",
            "state": kwargs["state"],
            "code_verifier": "pkce-verifier",
        }

    def acquire_token_by_auth_code_flow(self, flow, callback):
        self.token_cache.value = "updated-memory-cache"
        return {
            "access_token": "power-bi-token",
            "expires_in": 3600,
            "id_token_claims": {
                "tid": TENANT_ID,
                "oid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "preferred_username": "reader@example.com",
            },
        }

    def acquire_token_interactive(self, **_kwargs):
        self.token_cache.value = "updated-memory-cache"
        return {
            "access_token": "power-bi-token",
            "expires_in": 3600,
            "id_token_claims": {
                "tid": TENANT_ID,
                "oid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "preferred_username": "reader@example.com",
            },
        }


class _FakeMsal:
    SerializableTokenCache = _FakeCache
    @staticmethod
    def PublicClientApplication(client_id, authority, token_cache, **_kwargs):
        return _FakeMsalApp(client_id, authority, token_cache)


class _OtherUserMsalApp(_FakeMsalApp):
    def acquire_token_by_auth_code_flow(self, flow, callback):
        self.token_cache.value = "other-user-cache"
        return {
            "access_token": "other-user-token",
            "expires_in": 3600,
            "id_token_claims": {
                "tid": TENANT_ID,
                "oid": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "preferred_username": "other@example.com",
            },
        }

    def get_accounts(self, username=None):
        return [
            {
                "home_account_id": "other-home-account",
                "tenant_id": TENANT_ID,
                "local_account_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            }
        ]


class _OtherUserMsal:
    SerializableTokenCache = _FakeCache

    @staticmethod
    def PublicClientApplication(client_id, authority, token_cache, **_kwargs):
        return _OtherUserMsalApp(client_id, authority, token_cache)


class _FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return dict(self._payload)


class _FakeHttp:
    def __init__(self, responses):
        self.responses = dict(responses)
        self.calls = []

    def get(self, url, headers, timeout):
        self.calls.append((url, dict(headers), timeout))
        return self.responses[url]

    def post(self, url, headers, timeout):
        self.calls.append((url, dict(headers), timeout))
        return self.responses[url]


class _FakeCursor:
    def __init__(self, row):
        self.row = row
        self.executions = []
        self.closed = False

    def execute(self, query):
        self.executions.append(query)

    def fetchone(self):
        return self.row

    def close(self):
        self.closed = True


class _FakeConnection:
    def __init__(self, row):
        self.cursor_instance = _FakeCursor(row)
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def close(self):
        self.closed = True


class _FakeBrowserAuth:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.consent_cache_id_token = True


class _FakeConnector:
    AuthByWebBrowser = _FakeBrowserAuth

    def __init__(self, connection):
        self.connection = connection
        self.kwargs = None

    def connect(self, **kwargs):
        self.kwargs = kwargs
        return self.connection


class SetupControllerTests(unittest.TestCase):
    def test_incompatible_setup_state_closes_live_snowflake_before_migration(self):
        connection = _FakeConnection(
            ("ORG_ACCOUNT", "READER", "LINEAGE_READER", "LINEAGE_WH", "ANALYTICS")
        )
        old_state = {
            "version": setup.SETUP_VERSION - 1,
            "credentials": {"snowflake": {"connection": connection}},
        }

        migrated = setup.ensure_setup_state(old_state, now=100)

        self.assertTrue(connection.closed)
        self.assertEqual(migrated["version"], setup.SETUP_VERSION)

    def test_powerbi_requires_only_tenant_and_client(self):
        state = setup.create_empty_setup_state(now=100)

        configured = setup.configure_powerbi(
            state,
            {"tenant_id": TENANT_ID, "client_id": CLIENT_ID},
            now=101,
        )

        self.assertEqual(configured["workspace_id"], "")
        self.assertEqual(configured["report_ids"], [])
        self.assertEqual(configured["scope_mode"], "all_accessible")
        self.assertEqual(state["status"]["snowflake"]["state"], "not_configured")

    def test_optional_powerbi_scope_is_separate_and_validated(self):
        state = setup.create_empty_setup_state(now=100)
        setup.configure_powerbi(
            state,
            {"tenant_id": TENANT_ID, "client_id": CLIENT_ID},
        )
        with self.assertRaisesRegex(setup.SetupValidationError, "Workspace ID"):
            setup.configure_powerbi_scope(state, "", REPORT_ID)

        scoped = setup.configure_powerbi_scope(state, WORKSPACE_ID, "")
        self.assertEqual(scoped["scope_mode"], "workspace")
        self.assertTrue(setup.powerbi_scope_allows(state, WORKSPACE_ID, REPORT_ID))
        self.assertFalse(setup.powerbi_scope_allows(state, SEMANTIC_WORKSPACE_ID, REPORT_ID))

    def test_powerbi_identity_change_drops_previous_optional_scope(self):
        state = setup.create_empty_setup_state(now=100)
        setup.configure_powerbi(
            state,
            {
                "tenant_id": TENANT_ID,
                "client_id": CLIENT_ID,
                "workspace_id": WORKSPACE_ID,
                "report_ids": REPORT_ID,
            },
        )

        configured = setup.configure_powerbi(
            state,
            {
                "tenant_id": "99999999-9999-9999-9999-999999999999",
                "client_id": CLIENT_ID,
            },
        )

        self.assertEqual(configured["workspace_id"], "")
        self.assertEqual(configured["report_ids"], [])
        self.assertEqual(configured["scope_mode"], "all_accessible")

    def test_independent_browser_login_validates_unscoped_powerbi(self):
        state = setup.create_empty_setup_state(now=100)
        setup.configure_powerbi(
            state,
            {"tenant_id": TENANT_ID, "client_id": CLIENT_ID},
        )
        setup.authenticate_powerbi_interactive(state, _FakeMsal, now=110)
        probe_url = "https://api.powerbi.com/v1.0/myorg/groups?$top=1"

        setup.validate_powerbi_targets(
            state,
            _FakeHttp({probe_url: _FakeResponse(200, {"value": []})}),
        )

        self.assertTrue(setup.powerbi_is_ready(state, now=120))
        self.assertFalse(setup.snowflake_is_ready(state))
        self.assertFalse(setup.setup_is_ready(state, now=120))
        bundle = setup.legacy_auth_bundle(state, _FakeMsal, now=120)
        self.assertEqual(bundle["mu"], "power-bi-token")

    def test_workspace_only_validation_calls_no_report_endpoint(self):
        state = setup.create_empty_setup_state(now=100)
        setup.configure_powerbi(
            state,
            {
                "tenant_id": TENANT_ID,
                "client_id": CLIENT_ID,
                "workspace_id": WORKSPACE_ID,
            },
        )
        state["credentials"]["powerbi"]["access_token"] = "test-token"
        state["credentials"]["powerbi"]["expires_at"] = 9999999999
        workspace_url = f"https://api.powerbi.com/v1.0/myorg/groups/{WORKSPACE_ID}"
        http = _FakeHttp(
            {workspace_url: _FakeResponse(200, {"id": WORKSPACE_ID, "name": "Finance"})}
        )

        reports = setup.validate_powerbi_targets(state, http)

        self.assertEqual(reports, [])
        self.assertEqual(len(http.calls), 1)
        self.assertTrue(setup.powerbi_is_ready(state, now=120))

    def test_unscoped_fabric_grant_is_not_claimed_as_target_validated(self):
        state = setup.create_empty_setup_state(now=100)
        setup.configure_powerbi(
            state,
            {"tenant_id": TENANT_ID, "client_id": CLIENT_ID},
        )
        state["credentials"]["powerbi"]["fabric_token"] = "fabric-token"
        http = _FakeHttp({})

        setup.validate_fabric_targets(state, http)

        self.assertEqual(http.calls, [])
        self.assertEqual(state["status"]["fabric"]["state"], "authenticated")

    def test_snowflake_configuration_is_independent_of_powerbi(self):
        state = setup.create_empty_setup_state(now=100)

        configured = setup.configure_snowflake(state, _raw_setup()["snowflake"])

        self.assertEqual(configured["database"], "ANALYTICS")
        self.assertEqual(state["status"]["powerbi"]["state"], "not_configured")

    def test_provider_disconnects_do_not_clear_the_other_connection(self):
        state = setup.create_empty_setup_state(now=100)
        setup.configure_powerbi(
            state,
            {"tenant_id": TENANT_ID, "client_id": CLIENT_ID},
        )
        setup.configure_snowflake(state, _raw_setup()["snowflake"])
        connection = _FakeConnection(
            ("ORG_ACCOUNT", "READER", "LINEAGE_READER", "LINEAGE_WH", "ANALYTICS")
        )
        state["credentials"]["snowflake"]["connection"] = connection
        state["status"]["snowflake"] = {"state": "ready", "message": "ready"}
        state["credentials"]["powerbi"]["access_token"] = "token"

        setup.disconnect_powerbi(state, keep_configuration=True)

        self.assertFalse(connection.closed)
        self.assertIs(state["credentials"]["snowflake"]["connection"], connection)
        setup.disconnect_snowflake(state, keep_configuration=True)
        self.assertTrue(connection.closed)
        self.assertEqual(state["public"]["powerbi"]["tenant_id"], TENANT_ID)

    def test_secret_is_separated_from_public_configuration(self):
        state = setup.create_setup_state(_raw_setup(), now=100)

        self.assertNotIn("api_key", repr(state["public"]))
        self.assertEqual(state["credentials"]["claude_api_key"], "")
        setup.set_claude_api_key(state, "sk-ant-test-session-key")
        self.assertEqual(state["credentials"]["claude_api_key"], "sk-ant-test-session-key")
        self.assertEqual(state["public"]["powerbi"]["report_ids"], [REPORT_ID])

    def test_invalid_ids_and_missing_managed_fields_are_rejected(self):
        raw = _raw_setup()
        raw["powerbi"]["tenant_id"] = "not-a-uuid"
        with self.assertRaisesRegex(setup.SetupValidationError, "valid UUID"):
            setup.create_setup_state(raw)

        raw = _raw_setup("managed_multi")
        raw["claude"].pop("coordinator_agent_id")
        with self.assertRaisesRegex(setup.SetupValidationError, "Coordinator Agent Id"):
            setup.create_setup_state(raw)

        raw = _raw_setup()
        raw["snowflake"]["role"] = "ACCOUNTADMIN"
        with self.assertRaisesRegex(setup.SetupValidationError, "read-only role"):
            setup.create_setup_state(raw)

        raw = _raw_setup()
        raw["snowflake"]["read_only_confirmed"] = False
        with self.assertRaisesRegex(setup.SetupValidationError, "Confirm"):
            setup.create_setup_state(raw)

    def test_managed_multi_agent_settings_include_environment_and_every_agent(self):
        state = setup.create_setup_state(_raw_setup("managed_multi"))
        setup.set_claude_api_key(state, "sk-ant-test-session-key")
        settings = setup.claude_runtime_settings(state, {"model": "claude-sonnet-4-6"})

        self.assertTrue(settings["enabled"])
        self.assertEqual(settings["agent_runtime"], "managed")
        self.assertEqual(settings["orchestration_mode"], "multi")
        self.assertEqual(settings["managed_agents"]["environment_id"], "env_test")
        for field in setup.MANAGED_AGENT_FIELDS:
            self.assertTrue(settings["managed_agents"][field].startswith("agent_"))

    def test_session_only_claude_settings_do_not_fall_back_to_environment_key(self):
        state = setup.create_setup_state(_raw_setup())
        settings = setup.claude_runtime_settings(state, {"model": "claude-sonnet-4-6"})

        with self.assertRaises(ClaudeConfigurationError):
            resolve_claude_settings(settings)

    def test_snowflake_kwargs_force_external_browser_and_exclude_passwords(self):
        state = setup.create_setup_state(_raw_setup())
        kwargs = setup.build_snowflake_connect_kwargs(state)

        self.assertEqual(kwargs["authenticator"], "externalbrowser")
        self.assertFalse(kwargs["client_store_temporary_credential"])
        self.assertFalse(kwargs["session_parameters"]["CLIENT_STORE_TEMPORARY_CREDENTIAL"])
        self.assertNotIn("password", kwargs)
        self.assertNotIn("token", kwargs)

    def test_scope_allows_only_the_configured_workspace_report_pair(self):
        state = setup.create_setup_state(_raw_setup())

        self.assertTrue(setup.powerbi_scope_allows(state, WORKSPACE_ID, REPORT_ID))
        self.assertFalse(setup.powerbi_scope_allows(state, SEMANTIC_WORKSPACE_ID, REPORT_ID))
        self.assertFalse(
            setup.powerbi_scope_allows(
                state,
                WORKSPACE_ID,
                "66666666-6666-6666-6666-666666666666",
            )
        )

    def test_powerbi_pkce_flow_validates_state_and_tenant(self):
        state = setup.create_setup_state(_raw_setup(), now=100)
        auth_uri = setup.begin_entra_authorization(
            state,
            "http://localhost:8501/",
            "powerbi",
            _FakeMsal,
            now=110,
        )
        flow_state = state["credentials"]["powerbi"]["auth_flow"]["state"]

        self.assertTrue(auth_uri.startswith("https://login.microsoftonline.com/"))
        self.assertEqual(_FakeMsalApp.last_instance.client_id, CLIENT_ID)
        self.assertIn("Workspace.Read.All", " ".join(_FakeMsalApp.last_instance.started["scopes"]))

        audience = setup.complete_entra_authorization(
            state,
            {"code": "authorization-code", "state": flow_state},
            _FakeMsal,
            now=120,
        )
        self.assertEqual(audience, "powerbi")
        self.assertEqual(state["credentials"]["powerbi"]["access_token"], "power-bi-token")
        self.assertEqual(
            state["credentials"]["powerbi"]["identity"]["username"],
            "reader@example.com",
        )

    def test_public_client_flow_rejects_remote_https_callback(self):
        state = setup.create_setup_state(_raw_setup(), now=100)

        with self.assertRaisesRegex(setup.SetupValidationError, "loopback Streamlit"):
            setup.begin_entra_authorization(
                state,
                "https://lineage.example.com/",
                "powerbi",
                _FakeMsal,
                now=110,
            )

    def test_powerbi_pkce_flow_rejects_state_mismatch_and_timeout(self):
        state = setup.create_setup_state(_raw_setup(), now=100)
        setup.begin_entra_authorization(
            state,
            "http://localhost:8501/",
            "powerbi",
            _FakeMsal,
            now=110,
        )
        with self.assertRaisesRegex(setup.SetupAuthenticationError, "state validation"):
            setup.complete_entra_authorization(
                state,
                {"code": "authorization-code", "state": "wrong"},
                _FakeMsal,
                now=120,
            )
        self.assertIsNotNone(state["credentials"]["powerbi"]["auth_flow"])

        state = setup.create_setup_state(_raw_setup(), now=100)
        setup.begin_entra_authorization(
            state,
            "http://localhost:8501/",
            "powerbi",
            _FakeMsal,
            now=110,
        )
        with self.assertRaisesRegex(setup.SetupAuthenticationError, "timed out"):
            setup.complete_entra_authorization(
                state,
                {"code": "authorization-code", "state": "unused"},
                _FakeMsal,
                now=110 + setup.AUTH_FLOW_TTL_SECONDS,
            )

    def test_denial_requires_matching_state_and_fabric_requires_same_user(self):
        state = setup.create_setup_state(_raw_setup(), now=100)
        setup.begin_entra_authorization(
            state, "http://localhost:8501/", "powerbi", _FakeMsal, now=110
        )
        flow_state = state["credentials"]["powerbi"]["auth_flow"]["state"]
        with self.assertRaisesRegex(setup.SetupAuthenticationError, "state validation"):
            setup.complete_entra_authorization(
                state,
                {"error": "access_denied", "state": "wrong"},
                _FakeMsal,
                now=120,
            )
        self.assertIsNotNone(state["credentials"]["powerbi"]["auth_flow"])

        setup.complete_entra_authorization(
            state,
            {"code": "authorization-code", "state": flow_state},
            _FakeMsal,
            now=121,
        )
        state["status"]["powerbi"] = {"state": "ready", "message": "ready"}
        setup.begin_entra_authorization(
            state, "http://localhost:8501/", "fabric", _OtherUserMsal, now=130
        )
        fabric_state = state["credentials"]["powerbi"]["auth_flow"]["state"]
        with self.assertRaisesRegex(setup.SetupAuthenticationError, "same Microsoft identity"):
            setup.complete_entra_authorization(
                state,
                {"code": "fabric-code", "state": fabric_state},
                _OtherUserMsal,
                now=140,
            )

    def test_powerbi_validation_preserves_semantic_workspace_identity(self):
        state = setup.create_setup_state(_raw_setup())
        state["credentials"]["powerbi"]["access_token"] = "test-token"
        workspace_url = f"https://api.powerbi.com/v1.0/myorg/groups/{WORKSPACE_ID}"
        report_url = f"{workspace_url}/reports/{REPORT_ID}"
        http = _FakeHttp(
            {
                workspace_url: _FakeResponse(200, {"id": WORKSPACE_ID, "name": "Finance"}),
                report_url: _FakeResponse(
                    200,
                    {
                        "id": REPORT_ID,
                        "name": "Executive",
                        "datasetId": "77777777-7777-7777-7777-777777777777",
                        "datasetWorkspaceId": SEMANTIC_WORKSPACE_ID,
                    },
                ),
            }
        )

        reports = setup.validate_powerbi_targets(state, http)

        self.assertEqual(reports[0]["Workspace ID"], WORKSPACE_ID)
        self.assertEqual(reports[0]["Dataset Workspace ID"], SEMANTIC_WORKSPACE_ID)
        self.assertEqual(state["status"]["powerbi"]["state"], "ready")
        self.assertTrue(all(call[2] == setup.HTTP_TIMEOUT_SECONDS for call in http.calls))
        trusted = setup.validated_powerbi_report(state, WORKSPACE_ID, REPORT_ID)
        self.assertEqual(trusted["Dataset Workspace ID"], SEMANTIC_WORKSPACE_ID)

    def test_validation_rejects_malformed_success_and_probes_fabric_definition(self):
        state = setup.create_setup_state(_raw_setup())
        state["credentials"]["powerbi"]["access_token"] = "test-token"
        workspace_url = f"https://api.powerbi.com/v1.0/myorg/groups/{WORKSPACE_ID}"
        with self.assertRaisesRegex(setup.SetupAuthenticationError, "workspace validation"):
            setup.validate_powerbi_targets(
                state,
                _FakeHttp({workspace_url: _FakeResponse(200, {"name": "Wrong"})}),
            )

        state["credentials"]["powerbi"]["fabric_token"] = "fabric-token"
        definition_url = (
            f"https://api.fabric.microsoft.com/v1/workspaces/{WORKSPACE_ID}/reports/"
            f"{REPORT_ID}/getDefinition"
        )
        http = _FakeHttp({definition_url: _FakeResponse(202)})
        setup.validate_fabric_targets(state, http)
        self.assertEqual(http.calls[0][0], definition_url)
        self.assertEqual(state["status"]["fabric"]["state"], "ready")

    def test_powerbi_permission_failure_is_bounded_and_secret_free(self):
        state = setup.create_setup_state(_raw_setup())
        state["credentials"]["powerbi"]["access_token"] = "sensitive-token"
        workspace_url = f"https://api.powerbi.com/v1.0/myorg/groups/{WORKSPACE_ID}"
        http = _FakeHttp({workspace_url: _FakeResponse(403)})

        with self.assertRaises(setup.SetupAuthenticationError) as raised:
            setup.validate_powerbi_targets(state, http)

        self.assertIn("permission was denied", str(raised.exception))
        self.assertNotIn("sensitive-token", str(raised.exception))

    def test_snowflake_connect_validates_context_and_disables_credential_cache(self):
        state = setup.create_empty_setup_state()
        setup.configure_snowflake(state, _raw_setup()["snowflake"])
        connection = _FakeConnection(
            ("ORG_ACCOUNT", "READER", "LINEAGE_READER", "LINEAGE_WH", "ANALYTICS")
        )
        connector = _FakeConnector(connection)

        result = setup.connect_snowflake_external_browser(state, connector)

        self.assertIs(result, connection)
        self.assertFalse(connector.kwargs["auth_class"].consent_cache_id_token)
        self.assertNotIn("password", connector.kwargs)
        self.assertEqual(state["status"]["snowflake"]["state"], "ready")
        self.assertTrue(connection.cursor_instance.closed)

    def test_snowflake_context_mismatch_closes_connection(self):
        state = setup.create_empty_setup_state()
        setup.configure_snowflake(state, _raw_setup()["snowflake"])
        connection = _FakeConnection(
            ("ORG_ACCOUNT", "READER", "WRONG_ROLE", "LINEAGE_WH", "ANALYTICS")
        )
        connector = _FakeConnector(connection)

        with self.assertRaises(setup.SetupAuthenticationError):
            setup.connect_snowflake_external_browser(state, connector)

        self.assertTrue(connection.closed)
        self.assertEqual(state["status"]["snowflake"]["state"], "error")

    def test_ready_requires_all_services_and_logout_closes_snowflake(self):
        state = setup.create_setup_state(_raw_setup())
        for service in ("powerbi", "snowflake"):
            state["status"][service] = {"state": "ready", "message": "ready"}
        setup.set_claude_api_key(state, "sk-ant-test-session-key")
        state["credentials"]["powerbi"]["access_token"] = "test-token"
        state["credentials"]["powerbi"]["expires_at"] = 9999999999
        state["public"]["powerbi"]["validated_reports"] = [{"Report ID": REPORT_ID}]
        state["public"]["powerbi"]["validated_scope"] = {
            "mode": "reports",
            "fingerprint": setup.powerbi_scope_fingerprint(state),
        }
        connection = _FakeConnection(
            ("ORG_ACCOUNT", "READER", "LINEAGE_READER", "LINEAGE_WH", "ANALYTICS")
        )
        state["credentials"]["snowflake"]["connection"] = connection

        self.assertTrue(setup.setup_is_ready(state, now=100))
        setup.close_setup_resources(state)
        self.assertTrue(connection.closed)
        self.assertIsNone(state["credentials"]["snowflake"]["connection"])

    def test_ready_fails_closed_without_token_validation_or_live_connection(self):
        state = setup.create_setup_state(_raw_setup("disabled"))
        state["status"]["powerbi"] = {"state": "ready", "message": "ready"}
        state["status"]["snowflake"] = {"state": "ready", "message": "ready"}
        self.assertFalse(setup.setup_is_ready(state, now=100))

        state["credentials"]["powerbi"]["access_token"] = "token"
        state["credentials"]["powerbi"]["expires_at"] = 1000
        state["public"]["powerbi"]["validated_reports"] = [{"Report ID": REPORT_ID}]
        self.assertFalse(setup.setup_is_ready(state, now=100))

    def test_expired_powerbi_session_preserves_independent_providers(self):
        state = setup.create_setup_state(_raw_setup())
        state["status"]["powerbi"] = {"state": "ready", "message": "ready"}
        state["status"]["snowflake"] = {"state": "ready", "message": "ready"}
        setup.set_claude_api_key(state, "sk-ant-test-session-key")
        connection = _FakeConnection(
            ("ORG_ACCOUNT", "READER", "LINEAGE_READER", "LINEAGE_WH", "ANALYTICS")
        )
        state["credentials"]["snowflake"]["connection"] = connection
        state["credentials"]["powerbi"]["access_token"] = "expired-token"
        state["credentials"]["powerbi"]["expires_at"] = 120
        state["credentials"]["powerbi"]["token_cache"] = "refresh-material"
        state["complete"] = True

        setup.reconcile_setup_state(state, now=100)

        self.assertFalse(connection.closed)
        self.assertEqual(state["status"]["powerbi"]["state"], "configured")
        self.assertEqual(state["status"]["snowflake"]["state"], "ready")
        self.assertEqual(state["credentials"]["powerbi"]["token_cache"], "")
        self.assertEqual(state["credentials"]["claude_api_key"], "sk-ant-test-session-key")
        self.assertFalse(state["complete"])

    def test_pending_snapshot_never_copies_live_snowflake_connection(self):
        state = setup.create_setup_state(_raw_setup())
        connection = object()
        state["credentials"]["snowflake"]["connection"] = connection

        snapshot = setup.pending_state_snapshot(state)

        self.assertIsNone(snapshot["credentials"]["snowflake"]["connection"])
        self.assertEqual(snapshot["credentials"]["claude_api_key"], "")
        self.assertEqual(snapshot["credentials"]["powerbi"]["access_token"], "")
        self.assertEqual(snapshot["credentials"]["powerbi"]["token_cache"], "")
        self.assertEqual(snapshot["status"]["snowflake"]["state"], "configured")
        self.assertIs(state["credentials"]["snowflake"]["connection"], connection)

    def test_redaction_removes_known_and_shape_based_secrets(self):
        redacted = setup.redact_text(
            "Bearer abc.def and sk-ant-visible and exact-secret",
            ["exact-secret"],
        )

        self.assertNotIn("abc.def", redacted)
        self.assertNotIn("sk-ant-visible", redacted)
        self.assertNotIn("exact-secret", redacted)

    def test_controller_has_no_filesystem_configuration_operations(self):
        source = inspect.getsource(setup)

        self.assertNotIn("Path(", source)
        self.assertNotIn("open(", source)
        self.assertNotIn("json.dump", source)
        self.assertNotIn("dotenv", source.casefold())


if __name__ == "__main__":
    unittest.main()
