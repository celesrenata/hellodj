"""HelloDJ — Per-guild source credential resolution (bot playback path).

Resolves a guild's per-provider OAuth tokens from AWS Secrets Manager at play
time so a track played in guild A uses guild A's Tidal/Spotify/YouTube auth and
guild B uses guild B's, isolated from every other guild (R6.1, R6.3).

Per_Guild_Secret naming (isolated per guild+provider) — shared VERBATIM with the
web-ui's ``guild_sources.guild_source_secret_name`` so both sides address the
SAME secret::

    hellodj/<stage>/guild/<guildId>/<provider>

The ``guild/<guildId>/`` path segment is what isolates one guild's tokens from
another's, and is the exact prefix the bot's IAM read grant is scoped to
(``hellodj/<stage>/guild/*``).

Fallback (R6.2): if a guild has no secret for a provider, the resolver falls
back to the optional Platform_Owner-controlled global secret (default name
``hellodj/<stage>/<globalLeaf>`` — e.g. ``hellodj/<stage>/tidal-refresh`` /
``hellodj/<stage>/spotify``); if neither exists the provider is skipped
gracefully (``None``).

YouTube / YouTube_Music — per-guild capture, NO global fallback leaf
--------------------------------------------------------------------

``youtube`` and ``youtube_music`` intentionally have **no** entry in
:data:`GLOBAL_FALLBACK_LEAVES`. This is a deliberate, load-bearing design choice
that must NOT be changed:

* A guild that HAS connected its own YouTube (a per-guild secret
  ``hellodj/<stage>/guild/<gid>/youtube`` holding ``oauth_refresh_token`` +
  ``pot_token`` + ``pot_visitor_data``) has those exact creds resolved here and
  injected into Lavalink just-in-time, immediately before that guild's track is
  resolved/played (see :class:`YouTubeCredentialInjector`).
* A guild that has NO per-guild YouTube secret resolves to ``None`` and therefore
  triggers NO per-guild swap — it plays through the **untouched** global
  credential-store push (``bot.py:push_youtube_oauth`` → single ``POST /youtube``)
  exactly as before this change (preservation 3.5). Adding a youtube global
  fallback leaf here would break that separation, so ``GLOBAL_FALLBACK_LEAVES``
  keeps ONLY ``tidal`` and ``spotify`` (3.7).

SHARED-LAVALINK LIMITATION
--------------------------

The youtube-source plugin's ``POST /youtube`` replaces ALL credential fields on
every call, so one shared Lavalink node can hold only ONE YouTube credential set
at a time. :class:`YouTubeCredentialInjector` performs a just-in-time
last-writer-wins swap serialized by a per-node :class:`asyncio.Lock` (held from
the push through track resolution), which guarantees each *resolution* uses the
correct guild's creds. It does NOT provide true concurrent per-guild isolation on
a single node — two guilds resolving YouTube tracks at the very same instant
still serialize on the node lock. The fully isolated answer (a node-per-guild
Lavalink pool) is deferred (Design Risks #1).

Resolution is cached per ``(guild_id, provider)`` with a bounded TTL and
refreshed on expiry (R6.4). The cache key includes the guild id — combined with
the guild-scoped secret name this guarantees one guild's tokens are never
returned for another guild (R6.3).

Unified DynamoDB credential store (R6.1, R6.2, R6.5)
----------------------------------------------------

The unified-oauth-and-token-watchdog feature moves credentials from per-guild
Secrets Manager secrets into the ``hellodj-core`` DynamoDB table, one
envelope-encrypted item per user+provider (``PK=USER#<sub>`` /
``SK=SOURCECRED#<provider>``, entityType ``SourceCredential``), written by the
web-ui and refreshed by the durable watchdog in ``playback-orchestrator``. This
module adds a DynamoDB-backed resolution branch (:class:`DynamoCredentialResolver`)
that the bot consults FIRST:

* resolve the guild's owning Cognito ``sub`` (``PK=GUILD#<gid>`` / ``SK=OWNER``,
  ``data.owner_sub`` — the same ownership item the web-ui writes), then load the
  owner's ``SOURCECRED#<provider>`` credential item and **decrypt** its token
  blob with :func:`hellodj_platform_logic.token_crypto.decrypt_blob` using the
  reader's KMS decrypt grant (R6.1);
* the bot is **READ-ONLY** on tokens (R9.3): it decrypts but never re-encrypts or
  writes the credential item. When the decrypted access token is already expired
  it does NOT use the dead token — it re-reads the item once, bypassing the TTL
  cache, to pick up the value the watchdog refreshed out-of-band (R6.2);
* when no DynamoDB item exists (or the store/KMS is unavailable) it falls back to
  the legacy per-guild Secrets Manager secret so migration is seamless (R6.5).

The DynamoDB path keeps the SAME bounded-TTL, guild-scoped cache contract as the
Secrets Manager path (R6.4) and resolves the owning ``sub`` per guild, so one
user's credential is never returned for another guild/user (R6.3, R6.4).

Fakes-friendly, no hard platform-package import: like
:mod:`playback.user_entitlements`, DynamoDB access + decryption are injected
behind small protocols (:class:`CredentialItemStore`, :class:`OwnerLookup`,
:class:`DecryptBlob`) so this module stays importable with neither ``boto3`` nor
``hellodj_platform_logic`` present, and is exercised with in-memory fakes.
:func:`build_dynamo_credential_resolver` wires the real ``CoreTable`` +
``token_crypto`` seams at bot startup (lazy imports).

Tokens are never written to logs.

Requirements: 6.1, 6.2, 6.3, 6.4, 6.5
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

__all__ = [
    "GLOBAL_FALLBACK_LEAVES",
    "SOURCECRED_ENTITY_TYPE",
    "SOURCECRED_SK_PREFIX",
    "SUPPORTED_PROVIDERS",
    "YOUTUBE_PROVIDERS",
    "CredentialItemStore",
    "DecryptBlob",
    "DynamoCredentialResolver",
    "GuildCredentialResolver",
    "OwnerLookup",
    "SecretsReader",
    "YouTubeCredentialInjector",
    "YouTubePush",
    "build_dynamo_credential_resolver",
    "guild_pk",
    "guild_source_secret_name",
    "sourcecred_sk",
    "token_state_to_tokens",
    "user_pk",
    "youtube_oauth_payload",
]

log = logging.getLogger(__name__)

#: The music providers a guild can own OAuth for. Kept in lock-step with the
#: web-ui's ``guild_sources.SUPPORTED_PROVIDERS``.
SUPPORTED_PROVIDERS = ("youtube", "youtube_music", "tidal", "spotify")

#: The YouTube-family providers that use the per-guild ``POST /youtube`` swap.
#: These are exactly the providers with NO global fallback leaf.
YOUTUBE_PROVIDERS = ("youtube", "youtube_music")

#: Default provider → global-secret leaf mapping for the optional fallback
#: (R6.2, R5.5). The full global name is ``hellodj/<stage>/<leaf>``, matching
#: the AuthStack stage-scoped secret naming (``tidal-refresh`` / ``spotify``).
#: Providers absent from this map (youtube / youtube_music) have no global
#: fallback and are simply skipped when the guild has no secret.
GLOBAL_FALLBACK_LEAVES: dict[str, str] = {
    "tidal": "tidal-refresh",
    "spotify": "spotify",
}

#: Default cache time-to-live, in seconds.
DEFAULT_TTL_SECONDS = 300.0

# ── Unified DynamoDB credential store contract (mirrored verbatim) ──────────
#
# These MUST match the web-ui ``source_credential_service.py`` and the
# ``guild_admin_service.py`` ownership item EXACTLY. The bot and web-ui are
# separate deployables, so this is a deliberate shared copy — change both
# together.

#: Sort-key prefix for a user's per-provider credential item (web-ui
#: ``source_credential_service.SOURCECRED_SK_PREFIX``).
SOURCECRED_SK_PREFIX = "SOURCECRED#"

#: ``entityType`` discriminator for the credential item.
SOURCECRED_ENTITY_TYPE = "SourceCredential"

#: Sort key of a guild's ownership item (``guild_admin_service.OWNER_SK``).
OWNER_SK = "OWNER"

#: Expiry skew, in seconds, applied when deciding whether a decrypted access
#: token is "already expired" for the read-only bot (R6.2). A token expiring
#: within this window is treated as needing the watchdog-refreshed value.
DEFAULT_EXPIRY_SKEW_SECONDS = 30.0


def user_pk(sub: str) -> str:
    """Return the ``hellodj-core`` partition key for a user's items.

    Mirrors the web-ui ``source_credential_service.user_pk`` so the reader (this
    resolver) addresses the SAME item the writer (web-ui) creates, keyed by the
    stable Cognito subject.
    """
    return f"USER#{sub}"


def sourcecred_sk(provider: str) -> str:
    """Return the sort key for a user's per-provider credential item."""
    return f"{SOURCECRED_SK_PREFIX}{provider}"


def guild_pk(guild_id: str) -> str:
    """Return the partition key for a guild's items (``guild_admin_service``)."""
    return f"GUILD#{guild_id}"


def guild_source_secret_name(stage: str, guild_id: str, provider: str) -> str:
    """Return the Per_Guild_Secret name for a guild+provider (isolated).

    Shared verbatim with the web-ui so both the writer (web-ui) and the reader
    (this resolver) address the SAME secret. The ``guild/<guildId>/`` segment
    isolates one guild's tokens from every other guild's (R6.1, R6.3).
    """
    return f"hellodj/{stage}/guild/{guild_id}/{provider}"


class SecretsReader(Protocol):
    """Subset of the boto3 ``secretsmanager`` client used for reads only.

    The bot's IAM grant is read-only on ``hellodj/<stage>/guild/*`` (R7.2), so
    this resolver only ever calls ``get_secret_value``.
    """

    def get_secret_value(self, **kwargs: Any) -> dict[str, Any]: ...


class GuildCredentialResolver:
    """Resolve a guild's per-provider tokens (cached, bounded TTL, fallback).

    Parameters
    ----------
    secrets_client:
        A boto3 ``secretsmanager`` client (or any object satisfying
        :class:`SecretsReader`).
    stage:
        The deployment stage (``beta`` / ``staging`` / ``production``) used in
        the secret name.
    global_fallback_leaves:
        Optional provider → global-secret-leaf map for the fallback. Defaults to
        :data:`GLOBAL_FALLBACK_LEAVES`. Pass an empty dict to disable the global
        fallback entirely.
    ttl_seconds:
        Bounded cache TTL. A resolution is reused for at most this many seconds
        before it is refreshed from Secrets Manager (R6.4).
    time_fn:
        Injectable monotonic clock (defaults to :func:`time.monotonic`) so the
        cache TTL is deterministically testable.
    dynamo_resolver:
        Optional :class:`DynamoCredentialResolver` consulted FIRST for the
        unified per-user DynamoDB credential store (R6.1). When it returns a
        credential the guild's tokens come from DynamoDB; when it returns
        ``None`` (no item / store unavailable) resolution falls back to the
        legacy per-guild Secrets Manager secret (R6.5). ``None`` disables the
        DynamoDB branch entirely (legacy-only behavior, e.g. local dev).
    """

    def __init__(
        self,
        secrets_client: SecretsReader,
        *,
        stage: str,
        global_fallback_leaves: dict[str, str] | None = None,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        time_fn: Callable[[], float] = time.monotonic,
        dynamo_resolver: DynamoCredentialResolver | None = None,
    ) -> None:
        self._secrets = secrets_client
        self._stage = stage
        self._global_leaves = (
            GLOBAL_FALLBACK_LEAVES
            if global_fallback_leaves is None
            else dict(global_fallback_leaves)
        )
        self._ttl = float(ttl_seconds)
        self._now = time_fn
        self._dynamo = dynamo_resolver
        # cache: (guild_id, provider) -> (expires_at_monotonic, value)
        self._cache: dict[tuple[str, str], tuple[float, dict[str, Any] | None]] = {}

    def is_supported(self, provider: str) -> bool:
        """Return whether ``provider`` is a supported source."""
        return provider in SUPPORTED_PROVIDERS

    def resolve(self, guild_id: str | int, provider: str) -> dict[str, Any] | None:
        """Resolve a guild's tokens for a provider.

        Returns the parsed token dict for the guild's own Per_Guild_Secret when
        present (R6.1); otherwise the global fallback tokens if configured and
        present (R6.2); otherwise ``None`` so the caller skips the provider
        gracefully. Results are cached per ``(guild_id, provider)`` with a
        bounded TTL and refreshed on expiry (R6.4). The guild-scoped cache key
        and secret name guarantee no cross-guild leakage (R6.3).
        """
        gid = str(guild_id)
        key = (gid, provider)

        cached = self._cache.get(key)
        if cached is not None and self._now() < cached[0]:
            return cached[1]

        value = self._load(gid, provider)
        self._cache[key] = (self._now() + self._ttl, value)
        return value

    def invalidate(self, guild_id: str | int, provider: str) -> None:
        """Drop any cached resolution for a ``(guild_id, provider)`` pair."""
        self._cache.pop((str(guild_id), provider), None)

    # ── internals ───────────────────────────────────────────────────────

    def _load(self, guild_id: str, provider: str) -> dict[str, Any] | None:
        """Load tokens: DynamoDB first, then legacy secret + global fallback.

        Resolution order (unified-oauth-and-token-watchdog):

        1. the unified per-user DynamoDB credential item, decrypted (R6.1); if
           present it wins;
        2. otherwise the legacy per-guild Secrets Manager secret (R6.5);
        3. otherwise the optional global fallback secret (R6.2 legacy).
        """
        if self._dynamo is not None:
            dynamo_tokens = self._dynamo.resolve(guild_id, provider)
            if dynamo_tokens is not None:
                return dynamo_tokens

        name = guild_source_secret_name(self._stage, guild_id, provider)
        tokens = self._read_secret(name)
        if tokens is not None:
            return tokens

        leaf = self._global_leaves.get(provider)
        if leaf is None:
            return None
        global_name = f"hellodj/{self._stage}/{leaf}"
        fallback = self._read_secret(global_name)
        if fallback is not None:
            log.info(
                "guild_credentials: guild %s provider %s using global fallback",
                guild_id, provider,
            )
        return fallback

    def _read_secret(self, name: str) -> dict[str, Any] | None:
        """Fetch + JSON-parse a secret; return ``None`` if absent/unreadable.

        Never logs the secret value — only its name and the failure reason.
        """
        try:
            resp = self._secrets.get_secret_value(SecretId=name)
        except Exception as exc:  # noqa: BLE001 - absent/denied → treat as missing
            log.debug("guild_credentials: secret %s not available (%s)", name, exc)
            return None
        raw = resp.get("SecretString")
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            log.warning("guild_credentials: secret %s is not valid JSON", name)
            return None
        if not isinstance(parsed, dict):
            log.warning("guild_credentials: secret %s is not a JSON object", name)
            return None
        return parsed


# ── Unified DynamoDB credential resolution (read-only, decrypt) ─────────


class CredentialItemStore(Protocol):
    """Read-only view of the ``hellodj-core`` items the bot needs.

    Backed for real by a ``hellodj_platform_logic.data_access.CoreTable``
    (which already exposes ``get``); replaced by an in-memory fake in tests so
    the resolver runs without live AWS or the platform package. The bot's IAM
    grant is READ on the core table + KMS decrypt only (R9.3), so this protocol
    is deliberately read-only — no ``put``/``update``/``delete``.
    """

    def get(self, pk: str, sk: str) -> dict[str, Any] | None:
        """Return the ``hellodj-core`` item at (``pk``, ``sk``), or ``None``."""
        ...


class OwnerLookup(Protocol):
    """Resolve a guild id to its owning Cognito ``sub``.

    Backed for real by a lookup of the ``PK=GUILD#<gid>`` / ``SK=OWNER`` item
    (``data.owner_sub``) the web-ui ``guild_admin_service`` writes; replaced by
    an in-memory fake in tests. Returns ``None`` for a guild with no recorded
    owner (no unified credential can be resolved for it).
    """

    def owner_of(self, guild_id: str) -> str | None:
        """Return the owning Cognito subject of a guild, or ``None``."""
        ...


class DecryptBlob(Protocol):
    """Decrypt an envelope-encrypted token blob to plaintext bytes.

    Backed for real by :func:`hellodj_platform_logic.token_crypto.decrypt_blob`
    bound to the reader's injected KMS client (decrypt grant only, R9.3);
    replaced by a fake in tests. Given the four stored envelope fields
    (ciphertext, wrapped data key, KMS key id, nonce) it returns the decrypted
    token JSON bytes, or raises when the blob cannot be authentically decrypted
    (R3.4) — the resolver treats any raise as "unusable" and falls back.
    """

    def __call__(
        self,
        *,
        ciphertext: bytes,
        wrapped_key: bytes,
        key_id: str,
        nonce: bytes,
    ) -> bytes:
        ...


def token_state_to_tokens(blob: dict[str, Any]) -> dict[str, Any]:
    """Flatten a decrypted token blob into the resolver's ``tokens`` dict.

    The stored blob is the web-ui ``TokenState`` serialized as
    ``{access_token, refresh_token, expires_at, scope, extra}`` (see
    ``source_credential_service._token_state_to_json_bytes``). This flattens it
    into the SAME shape the legacy Secrets Manager path returns so every
    downstream consumer (notably :func:`youtube_oauth_payload`) is unchanged:

    * ``refresh_token`` is surfaced BOTH as ``refresh_token`` and as
      ``oauth_refresh_token`` (the YouTube payload builder looks for the latter,
      and the youtube-source ``POST /youtube`` swap sends OAuth + poToken +
      visitorData together in ONE request — never split, R6.3);
    * every provider-specific ``extra`` field (e.g. ``pot_token`` /
      ``pot_visitor_data`` for YouTube) is merged in verbatim so the poToken and
      visitorData travel with the refresh token in the same payload (R6.3);
    * ``access_token``, ``expires_at``, and ``scope`` are carried through so the
      reader can detect an expired access token (R6.2).

    Token values are never logged.
    """
    tokens: dict[str, Any] = {}
    # provider-specific fields first so explicit top-level fields win on clash.
    extra = blob.get("extra")
    if isinstance(extra, dict):
        tokens.update(extra)
    refresh = blob.get("refresh_token") or ""
    if refresh:
        tokens["refresh_token"] = refresh
        # The YouTube payload builder + the guild fallback both look for
        # ``oauth_refresh_token``; keep the unified store compatible.
        tokens.setdefault("oauth_refresh_token", refresh)
    access = blob.get("access_token")
    if access:
        tokens["access_token"] = access
    if "expires_at" in blob:
        tokens["expires_at"] = blob.get("expires_at")
    scope = blob.get("scope")
    if scope:
        tokens["scope"] = scope
    return tokens


class DynamoCredentialResolver:
    """Resolve a guild's unified per-user credential from DynamoDB (read-only).

    For a ``(guild_id, provider)`` this:

    1. resolves the guild's owning Cognito ``sub`` via :class:`OwnerLookup`
       (``GUILD#<gid>`` / ``OWNER`` → ``data.owner_sub``);
    2. loads the owner's ``USER#<sub>`` / ``SOURCECRED#<provider>`` credential
       item via :class:`CredentialItemStore`;
    3. decrypts the envelope-encrypted token blob via :class:`DecryptBlob`
       (reader KMS decrypt grant only, R9.3) and flattens it with
       :func:`token_state_to_tokens` (R6.1).

    Read-only + expiry handling (R6.2): the bot never refreshes or writes. When
    the decrypted access token is already expired (within
    :data:`DEFAULT_EXPIRY_SKEW_SECONDS`), the resolver does NOT return the dead
    token from cache — it re-reads the item once, bypassing the TTL cache, to
    pick up the value the durable watchdog refreshed out-of-band, and only then
    returns the freshest available tokens.

    Cache + isolation (R6.3, R6.4): results are cached per ``(guild_id,
    provider)`` with the same bounded TTL contract as the Secrets Manager path.
    The owning ``sub`` is resolved per guild, so one user's credential is never
    returned for another guild/user.

    Any failure — no owner, no item, missing/short envelope fields, a decrypt
    raise, or a store error — resolves to ``None`` so the caller falls back to
    the legacy secret (R6.5) rather than crashing. Token values are never
    logged.

    Parameters
    ----------
    store:
        A :class:`CredentialItemStore` (a ``CoreTable`` in production).
    owners:
        An :class:`OwnerLookup` mapping guild id → owner sub.
    decrypt:
        A :class:`DecryptBlob` seam bound to the reader's KMS decrypt grant.
    ttl_seconds:
        Bounded cache TTL (R6.4).
    time_fn:
        Injectable monotonic clock (defaults to :func:`time.monotonic`).
    wall_clock:
        Injectable epoch-seconds clock used ONLY to compare a decrypted token's
        absolute ``expires_at`` against "now" for the read-only expiry re-read
        (R6.2). Separate from ``time_fn`` (monotonic, cache TTL).
    expiry_skew_seconds:
        A token expiring within this many seconds of ``wall_clock()`` is treated
        as expired for the re-read (R6.2).
    """

    def __init__(
        self,
        store: CredentialItemStore,
        owners: OwnerLookup,
        decrypt: DecryptBlob,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        time_fn: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        expiry_skew_seconds: float = DEFAULT_EXPIRY_SKEW_SECONDS,
    ) -> None:
        self._store = store
        self._owners = owners
        self._decrypt = decrypt
        self._ttl = float(ttl_seconds)
        self._now = time_fn
        self._wall = wall_clock
        self._skew = float(expiry_skew_seconds)
        # cache: (guild_id, provider) -> (expires_at_monotonic, value)
        self._cache: dict[tuple[str, str], tuple[float, dict[str, Any] | None]] = {}

    def resolve(self, guild_id: str | int, provider: str) -> dict[str, Any] | None:
        """Resolve a guild's unified credential tokens, or ``None`` (R6.1, R6.2).

        Cached per ``(guild_id, provider)`` with a bounded TTL (R6.4). If a
        cached (or freshly read) credential's access token is expired, the entry
        is re-read once uncached so the reader uses the watchdog-refreshed value
        rather than a dead token (R6.2).
        """
        gid = str(guild_id)
        key = (gid, provider)

        cached = self._cache.get(key)
        if cached is not None and self._now() < cached[0]:
            value = cached[1]
            if value is not None and self._is_expired(value):
                # R6.2: never serve a dead token from cache — re-read to pick up
                # the watchdog-refreshed value.
                return self._refresh_entry(gid, provider, key)
            return value

        return self._refresh_entry(gid, provider, key)

    def invalidate(self, guild_id: str | int, provider: str) -> None:
        """Drop any cached resolution for a ``(guild_id, provider)`` pair."""
        self._cache.pop((str(guild_id), provider), None)

    # ── internals ───────────────────────────────────────────────────────

    def _refresh_entry(
        self, guild_id: str, provider: str, key: tuple[str, str]
    ) -> dict[str, Any] | None:
        """Load from DynamoDB, cache under ``key``, and return the value."""
        value = self._load(guild_id, provider)
        self._cache[key] = (self._now() + self._ttl, value)
        return value

    def _is_expired(self, tokens: dict[str, Any]) -> bool:
        """Return whether a decrypted token's ``expires_at`` is at/past now-skew.

        Only meaningful when the blob carried an ``expires_at``; a blob without
        one (refresh-only credential) is never considered expired here.
        """
        expires_at = tokens.get("expires_at")
        if expires_at in (None, ""):
            return False
        try:
            return float(expires_at) <= self._wall() + self._skew
        except (TypeError, ValueError):
            return False

    def _load(self, guild_id: str, provider: str) -> dict[str, Any] | None:
        """Resolve owner → item → decrypt → flatten. ``None`` on any failure."""
        try:
            owner_sub = self._owners.owner_of(guild_id)
        except Exception as exc:  # noqa: BLE001 - unavailable → fall back
            log.debug(
                "guild_credentials: owner lookup failed for guild %s (%s)",
                guild_id, exc,
            )
            return None
        if not owner_sub:
            return None

        try:
            item = self._store.get(user_pk(owner_sub), sourcecred_sk(provider))
        except Exception as exc:  # noqa: BLE001 - unavailable → fall back
            log.debug(
                "guild_credentials: credential item read failed for guild %s "
                "provider %s (%s)", guild_id, provider, exc,
            )
            return None
        if item is None:
            return None

        blob = self._decrypt_item(item, guild_id, provider)
        if blob is None:
            return None
        tokens = token_state_to_tokens(blob)
        if not tokens:
            return None
        log.info(
            "guild_credentials: guild %s provider %s resolved from unified "
            "DynamoDB credential store", guild_id, provider,
        )
        return tokens

    def _decrypt_item(
        self, item: dict[str, Any], guild_id: str, provider: str
    ) -> dict[str, Any] | None:
        """Decrypt an item's envelope blob to the token dict, or ``None``.

        A missing/short envelope or a decrypt raise (tamper / KMS failure, R3.4)
        yields ``None`` so the caller falls back — never a crash, never a
        logged token.
        """
        data = item.get("data", {})
        enc_blob = data.get("enc_blob")
        enc_key = data.get("enc_key")
        enc_nonce = data.get("enc_nonce")
        kms_key_id = data.get("kms_key_id")
        if not (enc_blob and enc_key and enc_nonce and kms_key_id):
            return None
        try:
            import base64

            plaintext = self._decrypt(
                ciphertext=base64.b64decode(enc_blob),
                wrapped_key=base64.b64decode(enc_key),
                key_id=str(kms_key_id),
                nonce=base64.b64decode(enc_nonce),
            )
            parsed = json.loads(plaintext.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - decrypt/parse failure → unusable
            log.warning(
                "guild_credentials: credential decrypt failed for guild %s "
                "provider %s (%s) — treating as unusable, falling back",
                guild_id, provider, type(exc).__name__,
            )
            return None
        if not isinstance(parsed, dict):
            return None
        return parsed


def build_dynamo_credential_resolver(
    *,
    table_name: str = "hellodj-core",
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
) -> DynamoCredentialResolver | None:
    """Wire a real ``CoreTable`` + ``token_crypto`` resolver, or ``None``.

    Lazily imports ``boto3`` and the shared ``hellodj_platform_logic`` package
    (``CoreTable`` + ``token_crypto``) so this module stays importable where
    those are absent (local dev / unit tests). On ANY construction failure
    (boto3 missing, no credentials, package unavailable) it logs and returns
    ``None`` so the caller degrades to the legacy Secrets Manager path — the
    same non-fatal convention as :func:`bot._build_guild_credential_resolver`
    and :func:`playback.user_entitlements.build_user_entitlement_resolver`.

    The guild → owner mapping reads the ``GUILD#<gid>`` / ``OWNER`` item's
    ``data.owner_sub`` (the same item the web-ui ``guild_admin_service`` writes)
    directly over the same ``CoreTable``, so the bot resolves ownership exactly
    as the web-ui does without depending on the web-ui package.
    """
    try:
        import boto3  # lazy — only present/needed in the SaaS deployment
        from hellodj_platform_logic.data_access import CoreTable
        from hellodj_platform_logic.token_crypto import (
            EncryptedBlob,
            decrypt_blob,
        )

        kms = boto3.client("kms")
        ddb = boto3.resource("dynamodb")
        core = CoreTable(ddb.Table(table_name))

        class _CoreOwnerLookup:
            """:class:`OwnerLookup` over the ``GUILD#<gid>`` / ``OWNER`` item."""

            def owner_of(self, guild_id: str) -> str | None:
                item = core.get(guild_pk(str(guild_id)), OWNER_SK)
                if item is None:
                    return None
                return item.get("data", {}).get("owner_sub")

        def _decrypt(
            *,
            ciphertext: bytes,
            wrapped_key: bytes,
            key_id: str,
            nonce: bytes,
        ) -> bytes:
            enc = EncryptedBlob(
                ciphertext=ciphertext,
                wrapped_key=wrapped_key,
                key_id=key_id,
                nonce=nonce,
            )
            return decrypt_blob(enc, kms)

        resolver = DynamoCredentialResolver(
            core, _CoreOwnerLookup(), _decrypt, ttl_seconds=ttl_seconds
        )
        log.info(
            "guild_credentials: unified DynamoDB credential resolver wired "
            "(table=%s, ttl=%ss)", table_name, ttl_seconds,
        )
        return resolver
    except Exception as exc:  # noqa: BLE001 - non-fatal: legacy secret fallback
        log.info(
            "guild_credentials: unified DynamoDB resolver unavailable (%s) — "
            "credential resolution falls back to per-guild Secrets Manager", exc,
        )
        return None


# ── YouTube per-guild just-in-time credential injection ─────────────────


def youtube_oauth_payload(
    tokens: dict[str, Any] | None,
    *,
    skip_initialization: bool = False,
) -> dict[str, Any] | None:
    """Build the ``POST /youtube`` payload from an explicit token dict.

    This is the SINGLE payload builder shared by the bot's global push
    (``bot.py:push_youtube_oauth``) and the per-guild just-in-time swap. It
    encodes the load-bearing invariant that OAuth refresh token AND poToken +
    visitorData are sent TOGETHER in ONE request — the youtube-source plugin
    replaces ALL fields on each call, so splitting them would erase the first
    (see hellodj-architecture "single POST /youtube request").

    Parameters
    ----------
    tokens:
        A dict that may contain ``oauth_refresh_token`` (or ``refresh_token``),
        ``pot_token``, and ``pot_visitor_data``. Missing/empty fields are simply
        omitted from the payload.
    skip_initialization:
        Value for the plugin's ``skipInitialization`` field.

    Returns
    -------
    dict | None
        The payload dict, or ``None`` when there is neither a refresh token nor a
        complete poToken pair to push (caller should skip the request).
    """
    tokens = tokens or {}
    refresh = (
        tokens.get("oauth_refresh_token")
        or tokens.get("refreshToken")
        or tokens.get("refresh_token")
        or ""
    )
    pot_token = tokens.get("pot_token") or tokens.get("poToken") or ""
    pot_visitor = (
        tokens.get("pot_visitor_data")
        or tokens.get("visitorData")
        or tokens.get("visitor_data")
        or ""
    )

    if not refresh and not (pot_token and pot_visitor):
        return None

    payload: dict[str, Any] = {"skipInitialization": skip_initialization}
    if refresh:
        payload["refreshToken"] = refresh
    if pot_token and pot_visitor:
        payload["poToken"] = pot_token
        payload["visitorData"] = pot_visitor
    return payload


class YouTubePush(Protocol):
    """Seam for issuing the ``POST /youtube`` request to a Lavalink node.

    Implemented for real by ``bot.py`` (an aiohttp POST to
    ``{LAVALINK_URI}/youtube``); replaced by a fake in unit tests so the
    injector is testable from ``bot/playback/`` without a live Lavalink or the
    discord/wavelink stack. Returns whether the push succeeded.
    """

    async def __call__(self, payload: dict[str, Any]) -> bool: ...


class YouTubeCredentialInjector:
    """Just-in-time per-guild YouTube credential swap on a shared Lavalink node.

    Before a guild's YouTube track is resolved/played, :meth:`inject_for_guild`
    resolves that guild's own ``{oauth_refresh_token, pot_token,
    pot_visitor_data}`` and pushes them via the single ``POST /youtube`` request
    (last-writer-wins). Guilds WITHOUT a per-guild YouTube secret cause NO swap —
    the caller falls through to the untouched global push (preservation 3.5).

    The swap is serialized with a per-Lavalink-node :class:`asyncio.Lock` so a
    concurrent resolution for another guild cannot interleave between the push
    and the track resolution. The caller holds the returned lock context across
    the push AND the subsequent resolve/play (see :meth:`swap_lock`).

    Parameters
    ----------
    resolver:
        A :class:`GuildCredentialResolver` used to fetch the per-guild secret.
    push:
        A :class:`YouTubePush` seam that issues the actual ``POST /youtube``.
    """

    def __init__(self, resolver: GuildCredentialResolver, push: YouTubePush) -> None:
        self._resolver = resolver
        self._push = push
        # node key -> lock. A single shared node uses one lock; a future
        # node-per-guild pool would key by node uri.
        self._locks: dict[str, asyncio.Lock] = {}

    def swap_lock(self, node_key: str = "default") -> asyncio.Lock:
        """Return the per-node lock, creating it on first use.

        The caller MUST hold this lock across the credential push AND the track
        resolution so a concurrent per-guild swap cannot clobber the node's creds
        mid-resolution (SHARED-LAVALINK LIMITATION).
        """
        lock = self._locks.get(node_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[node_key] = lock
        return lock

    def resolve_youtube(self, guild_id: str | int, provider: str) -> dict[str, Any] | None:
        """Return a guild's per-guild YouTube tokens, or ``None`` if it has none.

        Only youtube / youtube_music are per-guild-swappable; any other provider
        returns ``None`` (its resolution/fallback is handled elsewhere).
        """
        if provider not in YOUTUBE_PROVIDERS:
            return None
        tokens = self._resolver.resolve(guild_id, provider)
        if not tokens or not isinstance(tokens, dict):
            return None
        # Only treat it as a usable per-guild secret when a refresh token is
        # present — matches the stored shape written by the web-ui.
        if not (tokens.get("oauth_refresh_token") or tokens.get("refresh_token")):
            return None
        return tokens

    async def inject_for_guild(self, guild_id: str | int, provider: str) -> bool:
        """Resolve + push a guild's own YouTube creds if it has a per-guild secret.

        Returns ``True`` when a per-guild swap was performed (this guild's creds
        are now loaded on the node), ``False`` when the guild has no per-guild
        secret and the caller should use the untouched global push (3.5).

        NOTE: the caller is expected to hold :meth:`swap_lock` around this call
        and the subsequent track resolution.
        """
        tokens = self.resolve_youtube(guild_id, provider)
        if tokens is None:
            return False
        payload = youtube_oauth_payload(tokens, skip_initialization=False)
        if payload is None:
            return False
        ok = await self._push(payload)
        if ok:
            log.info(
                "guild_credentials: swapped per-guild YouTube creds for guild %s "
                "(provider=%s) before playback",
                guild_id, provider,
            )
        else:
            log.warning(
                "guild_credentials: per-guild YouTube cred swap POST failed for "
                "guild %s (provider=%s)",
                guild_id, provider,
            )
        return ok
