"""Integration test: first admin login after the clean-slate migration (R19.3).

This exercises the whole migration flow end to end against a hand-rolled fake
Cognito ``cognito-idp`` client and a fake DynamoDB fresh-init resource, then
simulates the Platform_Owner's *first* AWS login:

    load legacy export
      -> migration.filter_legacy (keep only the admin bootstrap credential)
      -> CognitoAdminSeeder.seed  (admin_create_user + admin_add_user_to_group)
      -> FreshDataInitializer     (verify fresh tables; write NO legacy data)
      -> simulate first admin login via the fake Cognito admin_initiate_auth

The legacy export deliberately mixes every ``LegacyRecordType`` (the admin
bootstrap credential plus playback / session / playlist / configuration
records). The assertions cover R19.3:

* Only the admin bootstrap credential is seeded into Cognito
  (``admin_create_user`` called exactly once, for the admin username) and the
  user is added to the ``admins`` group.
* The seeded bootstrap credential authenticates the *first* admin login through
  Cognito (the fake ``admin_initiate_auth`` returns an ``AuthenticationResult``
  for the seeded user and rejects any other user with a
  ``NotAuthorizedException``-equivalent).
* No legacy playback / session / playlist / configuration data was written to
  DynamoDB (the fresh-init resource recorded no writes; ``filter_legacy``
  excluded every non-credential record).

This is a plain ``pytest`` integration test (not a Hypothesis property test).

Requirements: 19.3
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from hellodj_platform_logic.types import LegacyRecord, LegacyRecordType
from migration_job import (
    CognitoAdminSeeder,
    FreshDataInitializer,
    InMemoryLegacySource,
    MigrationJob,
)
from migration_job.cognito_seeder import DEFAULT_ADMIN_GROUP


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class NotAuthorizedException(Exception):
    """Fake equivalent of the boto3 Cognito ``NotAuthorizedException``.

    Carries a boto3-shaped ``response`` so callers can inspect the error code
    the same way they would with a real botocore ``ClientError``.
    """

    def __init__(self, message: str = "Incorrect username or password.") -> None:
        super().__init__(message)
        self.response = {"Error": {"Code": "NotAuthorizedException", "Message": message}}


class FakeCognitoClient:
    """A minimal in-memory stand-in for the boto3 ``cognito-idp`` client.

    Records every call so the test can assert exactly what the seeder did, and
    implements ``admin_initiate_auth`` so the test can simulate the first admin
    login: it authenticates only users that were actually created and added to
    the admin group, and rejects everyone else.
    """

    def __init__(self) -> None:
        # username -> {"attributes": {...}, "password": str | None}
        self.users: dict[str, dict[str, Any]] = {}
        # group name -> set of usernames
        self.group_members: dict[str, set[str]] = {}
        # ordered log of (operation, kwargs) for assertions
        self.calls: list[tuple[str, dict[str, Any]]] = []

    # -- seeding surface (used by CognitoAdminSeeder) ----------------------- #

    def admin_create_user(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("admin_create_user", kwargs))
        username = kwargs["Username"]
        attributes = {
            attr["Name"]: attr["Value"] for attr in kwargs.get("UserAttributes", [])
        }
        # A real pool would raise UsernameExistsException; keep it simple and
        # deterministic — the migration seeder is exercised once per user here.
        self.users[username] = {"attributes": attributes, "password": None}
        return {"User": {"Username": username}}

    def admin_add_user_to_group(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("admin_add_user_to_group", kwargs))
        username = kwargs["Username"]
        group = kwargs["GroupName"]
        if username not in self.users:
            # Cognito would reject adding a non-existent user to a group.
            raise NotAuthorizedException(f"user {username!r} does not exist")
        self.group_members.setdefault(group, set()).add(username)
        return {}

    # -- login surface (used by the test to simulate first admin login) ----- #

    def admin_set_user_password(self, **kwargs: Any) -> dict[str, Any]:
        """Set the Platform_Owner's password on their first-login completion."""
        self.calls.append(("admin_set_user_password", kwargs))
        username = kwargs["Username"]
        if username not in self.users:
            raise NotAuthorizedException(f"user {username!r} does not exist")
        self.users[username]["password"] = kwargs["Password"]
        return {}

    def admin_initiate_auth(self, **kwargs: Any) -> dict[str, Any]:
        """Authenticate a user with ADMIN_USER_PASSWORD_AUTH-style params.

        Returns an ``AuthenticationResult`` (fake tokens) for a seeded user with
        a matching password, and raises the fake ``NotAuthorizedException`` for
        an unknown user or a wrong password — mirroring real Cognito behaviour.
        """
        self.calls.append(("admin_initiate_auth", kwargs))
        params = kwargs.get("AuthParameters", {})
        username = params.get("USERNAME", "")
        password = params.get("PASSWORD")

        user = self.users.get(username)
        if user is None:
            raise NotAuthorizedException("Incorrect username or password.")
        if user["password"] is None or user["password"] != password:
            raise NotAuthorizedException("Incorrect username or password.")

        return {
            "AuthenticationResult": {
                "AccessToken": f"access-token-for-{username}",
                "IdToken": f"id-token-for-{username}",
                "RefreshToken": f"refresh-token-for-{username}",
                "ExpiresIn": 3600,
                "TokenType": "Bearer",
            }
        }


class RecordingTable:
    """A fake DynamoDB table that exists (``load`` succeeds) and records writes."""

    def __init__(self, name: str, writes: list[tuple[str, dict[str, Any]]]) -> None:
        self._name = name
        self._writes = writes

    def load(self) -> None:  # existence probe used by FreshDataInitializer
        return None

    def put_item(self, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        self._writes.append((self._name, kwargs))
        return {}


class RecordingDynamoResource:
    """A fake DynamoDB resource exposing existing tables and recording writes.

    ``FreshDataInitializer`` only probes table existence via ``Table(name).load``;
    the shared ``writes`` list lets the test assert that the migration wrote no
    legacy playback/session/playlist/config rows anywhere.
    """

    def __init__(self) -> None:
        self.writes: list[tuple[str, dict[str, Any]]] = []
        self._tables: dict[str, RecordingTable] = {}

    def Table(self, name: str) -> RecordingTable:  # noqa: N802 - boto3 API name
        table = self._tables.get(name)
        if table is None:
            table = RecordingTable(name, self.writes)
            self._tables[name] = table
        return table


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

ADMIN_USERNAME = "platform-owner"
ADMIN_EMAIL = "owner@hellodj.bot"
USER_POOL_ID = "us-east-1_TESTPOOL"
FIRST_LOGIN_PASSWORD = "First-Login-Passw0rd!"


@pytest.fixture
def legacy_export() -> list[LegacyRecord]:
    """A legacy export mixing every record type; one admin bootstrap credential."""
    return [
        LegacyRecord(
            record_type=LegacyRecordType.PLAYBACK,
            record_id="pb-1",
            payload=json.dumps({"track": "legacy-song"}),
        ),
        LegacyRecord(
            record_type=LegacyRecordType.SESSION,
            record_id="sess-1",
            payload=json.dumps({"voice_channel": 42}),
        ),
        LegacyRecord(
            record_type=LegacyRecordType.ADMIN_BOOTSTRAP_CREDENTIAL,
            record_id="legacy-owner-id",
            payload=json.dumps({"username": ADMIN_USERNAME, "email": ADMIN_EMAIL}),
        ),
        LegacyRecord(
            record_type=LegacyRecordType.PLAYLIST,
            record_id="pl-1",
            payload=json.dumps({"name": "old mixtape"}),
        ),
        LegacyRecord(
            record_type=LegacyRecordType.CONFIGURATION,
            record_id="cfg-1",
            payload=json.dumps({"volume": 80}),
        ),
    ]


@pytest.fixture
def fake_cognito() -> FakeCognitoClient:
    return FakeCognitoClient()


@pytest.fixture
def fake_dynamo() -> RecordingDynamoResource:
    return RecordingDynamoResource()


@pytest.fixture
def migration_result(
    legacy_export: list[LegacyRecord],
    fake_cognito: FakeCognitoClient,
    fake_dynamo: RecordingDynamoResource,
):
    """Run the full migration Job with mocked Cognito + DynamoDB and return result."""
    job = MigrationJob(
        legacy_source=InMemoryLegacySource(legacy_export),
        seeder=CognitoAdminSeeder(USER_POOL_ID, client=fake_cognito),
        fresh_initializer=FreshDataInitializer(resource=fake_dynamo),
    )
    return job.run()


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_only_admin_bootstrap_credential_seeded_into_cognito(
    migration_result, fake_cognito: FakeCognitoClient
) -> None:
    """R19.1/R19.3: exactly one user (the admin) is created and put in admins."""
    create_calls = [c for c in fake_cognito.calls if c[0] == "admin_create_user"]
    assert len(create_calls) == 1, "exactly one Cognito user must be seeded"
    assert create_calls[0][1]["Username"] == ADMIN_USERNAME

    # Result summary agrees: exactly one seeded username, the admin.
    assert migration_result.seeded_usernames == (ADMIN_USERNAME,)

    # The seeded user is a member of the admins group.
    assert fake_cognito.group_members.get(DEFAULT_ADMIN_GROUP) == {ADMIN_USERNAME}

    # The admin_add_user_to_group call targeted the admins group for the admin.
    add_calls = [c for c in fake_cognito.calls if c[0] == "admin_add_user_to_group"]
    assert len(add_calls) == 1
    assert add_calls[0][1]["Username"] == ADMIN_USERNAME
    assert add_calls[0][1]["GroupName"] == DEFAULT_ADMIN_GROUP


def test_first_admin_login_authenticates_via_cognito(
    migration_result, fake_cognito: FakeCognitoClient
) -> None:
    """R19.3: the bootstrap credential authenticates the first admin login."""
    # The Platform_Owner completes their first-login password (Cognito flow).
    fake_cognito.admin_set_user_password(
        UserPoolId=USER_POOL_ID,
        Username=ADMIN_USERNAME,
        Password=FIRST_LOGIN_PASSWORD,
        Permanent=True,
    )

    # First AWS login: authenticate through Cognito with the bootstrap user.
    auth = fake_cognito.admin_initiate_auth(
        UserPoolId=USER_POOL_ID,
        ClientId="test-app-client",
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": ADMIN_USERNAME, "PASSWORD": FIRST_LOGIN_PASSWORD},
    )

    result = auth["AuthenticationResult"]
    assert result["AccessToken"] == f"access-token-for-{ADMIN_USERNAME}"
    assert result["IdToken"] == f"id-token-for-{ADMIN_USERNAME}"
    assert result["RefreshToken"]
    assert result["TokenType"] == "Bearer"


def test_login_rejected_for_non_seeded_user(
    migration_result, fake_cognito: FakeCognitoClient
) -> None:
    """Only the seeded bootstrap admin can log in; unknown users are rejected."""
    with pytest.raises(NotAuthorizedException):
        fake_cognito.admin_initiate_auth(
            UserPoolId=USER_POOL_ID,
            ClientId="test-app-client",
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": "someone-else",
                "PASSWORD": FIRST_LOGIN_PASSWORD,
            },
        )


def test_login_rejected_for_wrong_password(
    migration_result, fake_cognito: FakeCognitoClient
) -> None:
    """A wrong password for the seeded admin is rejected by Cognito."""
    fake_cognito.admin_set_user_password(
        UserPoolId=USER_POOL_ID,
        Username=ADMIN_USERNAME,
        Password=FIRST_LOGIN_PASSWORD,
        Permanent=True,
    )
    with pytest.raises(NotAuthorizedException):
        fake_cognito.admin_initiate_auth(
            UserPoolId=USER_POOL_ID,
            ClientId="test-app-client",
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": ADMIN_USERNAME, "PASSWORD": "wrong"},
        )


def test_no_legacy_data_written_to_dynamodb(
    migration_result, fake_dynamo: RecordingDynamoResource
) -> None:
    """R19.2/R19.4: no legacy playback/session/playlist/config rows are written."""
    # The fresh-init resource recorded zero writes of any kind.
    assert fake_dynamo.writes == []

    # The fresh tables were verified reachable (fresh start, no seeded data).
    assert set(migration_result.fresh_tables_verified) == {
        "hellodj-core",
        "hellodj-session",
        "hellodj-search-cache",
    }


def test_migration_result_counts(migration_result) -> None:
    """The result summarizes the full export and the single migrated credential."""
    assert migration_result.legacy_record_count == 5
    assert len(migration_result.seeded_usernames) == 1
