"""Bot Orchestrator Controller for the HelloDJ SaaS platform.

Manages tenant bot instance lifecycle via the Kubernetes API:
- Provisioning (Pod creation with tier-based resource limits)
- Deprovisioning (graceful SIGTERM + force kill after grace period)
- Health checking (Redis heartbeat monitoring, automatic restarts)
- Restart tracking (max 5 restarts in 10 minutes → mark as failed)

Pod namespace: hellodj-service
Heartbeat key pattern: heartbeat:{instance_id} (30s TTL, checked every 30s)
No heartbeat for 60s → automatic restart.
Max 5 restarts in 10 minutes → status 'failed', tenant notified.

Requirements: 10.1, 10.4, 10.5, 10.6, 10.8, 10.9
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import psycopg2
import psycopg2.extras
import psycopg2.extensions

try:
    from kubernetes import client as k8s_client
    from kubernetes import config as k8s_config
    from kubernetes.client.exceptions import ApiException
except ImportError:
    k8s_client = None
    k8s_config = None
    ApiException = Exception

try:
    import redis
except ImportError:
    redis = None

log = logging.getLogger(__name__)

# Register UUID adapter for psycopg2
psycopg2.extensions.register_adapter(
    uuid.UUID, lambda u: psycopg2.extensions.AsIs(f"'{u}'")
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PG_URI = os.environ.get(
    "HELLODJ_PG_URI",
    "postgresql://hellodj:hellodj@postgresql-rw.postgresql-service.svc.cluster.local:5432/hellodj",
)

REDIS_URL = os.environ.get(
    "HELLODJ_REDIS_URL",
    "redis://redis.redis-service.svc.cluster.local:6379/0",
)

POD_NAMESPACE = "hellodj-service"

# Heartbeat configuration
HEARTBEAT_KEY_PREFIX = "heartbeat:"
HEARTBEAT_TIMEOUT_SECONDS = 60  # No heartbeat for 60s → restart

# Restart tracking
MAX_RESTARTS = 5
RESTART_WINDOW_SECONDS = 600  # 10 minutes
RESTART_COUNT_PREFIX = "restart_count:"
RESTART_WINDOW_PREFIX = "restart_window:"

# Pending resources retry
MAX_PROVISION_RETRIES = 10
PROVISION_RETRY_INTERVAL_SECONDS = 60

# Resource limits per tier
RESOURCE_LIMITS = {
    "base": {
        "cpu_request": "250m",
        "cpu_limit": "250m",
        "memory_request": "512Mi",
        "memory_limit": "512Mi",
        "gpu_vfs": 0,
    },
    "video": {
        "cpu_request": "500m",
        "cpu_limit": "500m",
        "memory_request": "1Gi",
        "memory_limit": "1Gi",
        "gpu_vfs": 1,
    },
}

# Bot image tag (configurable via env)
BOT_IMAGE = os.environ.get(
    "HELLODJ_BOT_IMAGE",
    "registry.celestium.life/hellodj/bot:latest",
)

LAVALINK_HOST = os.environ.get(
    "HELLODJ_LAVALINK_HOST",
    "lavalink-pool.hellodj-service.svc.cluster.local",
)

LAVALINK_PORT = os.environ.get("HELLODJ_LAVALINK_PORT", "2333")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def set_pg_uri(uri: str) -> None:
    """Override the PG URI (for testing)."""
    global PG_URI
    PG_URI = uri


def _get_pg_conn():
    """Get a psycopg2 connection to PostgreSQL."""
    return psycopg2.connect(PG_URI)


def _get_redis_client():
    """Get a Redis client instance."""
    if redis is None:
        raise RuntimeError("redis package not installed")
    return redis.Redis.from_url(REDIS_URL, decode_responses=True)


def _load_k8s_config():
    """Load Kubernetes configuration (in-cluster or from kubeconfig)."""
    if k8s_config is None:
        raise RuntimeError("kubernetes package not installed")
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class OrchestratorError(Exception):
    """Base exception for bot orchestrator operations."""
    pass


class InstanceNotFoundError(OrchestratorError):
    """Raised when a bot instance ID does not exist in the database."""
    pass


class InsufficientResourcesError(OrchestratorError):
    """Raised when cluster resources are insufficient to schedule the Pod."""
    pass


class MaxRestartsExceededError(OrchestratorError):
    """Raised when an instance has exceeded maximum restart attempts."""
    pass


# ---------------------------------------------------------------------------
# BotOrchestrator
# ---------------------------------------------------------------------------


class BotOrchestrator:
    """Manages tenant bot Pod lifecycle via the Kubernetes API.

    Handles provisioning, deprovisioning, health checking, and restart
    tracking for multi-tenant bot instances.
    """

    def __init__(
        self,
        pg_uri: str | None = None,
        redis_url: str | None = None,
        k8s_configured: bool = False,
    ):
        """Initialize the bot orchestrator.

        Args:
            pg_uri: PostgreSQL connection URI override.
            redis_url: Redis connection URL override.
            k8s_configured: If True, skip K8s config loading (for testing).
        """
        self._pg_uri = pg_uri or PG_URI
        self._redis_url = redis_url or REDIS_URL
        self._k8s_configured = k8s_configured

        if not k8s_configured:
            _load_k8s_config()

    def _get_conn(self):
        """Get a database connection."""
        return psycopg2.connect(self._pg_uri)

    def _get_redis(self):
        """Get a Redis client."""
        if redis is None:
            raise RuntimeError("redis package not installed")
        return redis.Redis.from_url(self._redis_url, decode_responses=True)

    def _get_k8s_core_api(self):
        """Get the Kubernetes CoreV1Api client."""
        if k8s_client is None:
            raise RuntimeError("kubernetes package not installed")
        return k8s_client.CoreV1Api()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def provision(
        self,
        tenant_id: uuid.UUID,
        subscription: dict[str, Any],
    ) -> dict[str, Any]:
        """Provision a new bot instance as a Kubernetes Pod.

        Creates a bot_instances record in PostgreSQL and schedules a Pod
        in the hellodj-service namespace with resource limits based on
        the subscription tier.

        Args:
            tenant_id: The owning tenant's UUID.
            subscription: Subscription record dict (must include 'plan', 'addons').

        Returns:
            The created bot_instance record as a dict.

        Raises:
            InsufficientResourcesError: If cluster resources are insufficient.
            OrchestratorError: On other provisioning failures.
        """
        instance_id = uuid.uuid4()
        plan = subscription.get("plan", "base")
        addons = subscription.get("addons", [])

        # Determine resource tier
        has_video = "video" in addons
        tier = "video" if has_video else "base"

        # Create the DB record with status 'provisioning'
        conn = self._get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                pod_name = f"tenant-bot-{str(instance_id)[:8]}"
                cur.execute(
                    """
                    INSERT INTO bot_instances (id, tenant_id, status, pod_name)
                    VALUES (%s, %s, 'provisioning', %s)
                    RETURNING id, tenant_id, discord_bot_token_encrypted, guild_ids,
                              status, node_name, pod_name, created_at
                    """,
                    (instance_id, tenant_id, pod_name),
                )
                instance = dict(cur.fetchone())
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        log.info(
            "Created bot instance record: id=%s tenant=%s tier=%s pod=%s",
            instance_id, tenant_id, tier, pod_name,
        )

        # Build and create the Pod
        try:
            pod_spec = self._build_pod_spec(
                instance_id=instance_id,
                tenant_id=tenant_id,
                pod_name=pod_name,
                tier=tier,
            )
            self._create_pod(pod_spec)
        except InsufficientResourcesError:
            # Mark as pending_resources
            self._update_instance_status(instance_id, "pending_resources")
            instance["status"] = "pending_resources"
            log.warning(
                "Insufficient resources for bot instance %s, set to pending_resources",
                instance_id,
            )
            return instance
        except Exception as exc:
            # Mark as error on unexpected failure
            self._update_instance_status(instance_id, "error")
            instance["status"] = "error"
            log.error(
                "Failed to create Pod for instance %s: %s", instance_id, exc
            )
            raise OrchestratorError(
                f"Failed to provision bot instance: {exc}"
            ) from exc

        log.info("Pod created successfully for instance %s", instance_id)
        return instance

    def deprovision(
        self,
        bot_instance_id: uuid.UUID,
        grace_seconds: int = 30,
    ) -> None:
        """Deprovision a bot instance by sending SIGTERM and deleting the Pod.

        Sends SIGTERM to the Pod process, allows up to `grace_seconds` for
        graceful shutdown (session persistence, Discord disconnect), then
        force-terminates.

        Args:
            bot_instance_id: The bot instance UUID to deprovision.
            grace_seconds: Seconds to wait for graceful shutdown (default 30).

        Raises:
            InstanceNotFoundError: If the bot instance does not exist.
            OrchestratorError: On deletion failure.
        """
        instance = self._get_instance(bot_instance_id)
        if instance is None:
            raise InstanceNotFoundError(
                f"Bot instance not found: {bot_instance_id}"
            )

        pod_name = instance["pod_name"]
        if not pod_name:
            log.warning(
                "No pod_name for instance %s, marking as stopped",
                bot_instance_id,
            )
            self._update_instance_status(bot_instance_id, "stopped")
            return

        try:
            api = self._get_k8s_core_api()
            # Delete with grace period — K8s sends SIGTERM, waits, then SIGKILL
            api.delete_namespaced_pod(
                name=pod_name,
                namespace=POD_NAMESPACE,
                body=k8s_client.V1DeleteOptions(
                    grace_period_seconds=grace_seconds,
                ),
            )
            log.info(
                "Pod %s deletion initiated (grace=%ds) for instance %s",
                pod_name, grace_seconds, bot_instance_id,
            )
        except ApiException as exc:
            if exc.status == 404:
                log.warning("Pod %s already gone for instance %s", pod_name, bot_instance_id)
            else:
                log.error(
                    "Failed to delete Pod %s for instance %s: %s",
                    pod_name, bot_instance_id, exc,
                )
                raise OrchestratorError(
                    f"Failed to deprovision bot instance: {exc}"
                ) from exc

        # Update status to stopped
        self._update_instance_status(bot_instance_id, "stopped")

        # Clean up restart tracking
        self._clear_restart_tracking(bot_instance_id)

        log.info("Bot instance %s deprovisioned successfully", bot_instance_id)

    def health_check(self) -> list[dict[str, Any]]:
        """Check Redis heartbeats for all running instances.

        For instances with no heartbeat for 60+ seconds, trigger a restart.
        Returns a list of health status dicts for all running instances.

        Returns:
            List of dicts with keys: instance_id, status, last_heartbeat, action.
        """
        results = []
        redis_client = self._get_redis()

        # Get all instances with status 'running'
        running_instances = self._get_instances_by_status("running")

        for instance in running_instances:
            instance_id = instance["id"]
            heartbeat_key = f"{HEARTBEAT_KEY_PREFIX}{instance_id}"

            # Check heartbeat existence in Redis
            last_heartbeat = redis_client.get(heartbeat_key)

            health_entry = {
                "instance_id": instance_id,
                "pod_name": instance["pod_name"],
                "tenant_id": instance["tenant_id"],
                "status": "healthy",
                "last_heartbeat": last_heartbeat,
                "action": None,
            }

            if last_heartbeat is None:
                # No heartbeat key exists — either expired (>30s since last set)
                # or never sent. Since TTL is 30s and we check timeout at 60s,
                # the key being absent means no heartbeat for at least 30s.
                # We treat missing key as unhealthy (timeout exceeded).
                log.warning(
                    "No heartbeat for instance %s (pod=%s), triggering restart",
                    instance_id, instance["pod_name"],
                )
                health_entry["status"] = "unhealthy"
                health_entry["action"] = "restart"

                try:
                    self.restart(instance_id)
                except MaxRestartsExceededError:
                    health_entry["action"] = "failed"
                    health_entry["status"] = "failed"
                except OrchestratorError as exc:
                    log.error(
                        "Restart failed for instance %s: %s", instance_id, exc
                    )
                    health_entry["action"] = "restart_failed"

            results.append(health_entry)

        # Also check instances in 'pending_resources' for retry
        pending_instances = self._get_instances_by_status("pending_resources")
        for instance in pending_instances:
            health_entry = {
                "instance_id": instance["id"],
                "pod_name": instance["pod_name"],
                "tenant_id": instance["tenant_id"],
                "status": "pending_resources",
                "last_heartbeat": None,
                "action": "retry_provision",
            }
            results.append(health_entry)

        return results

    def restart(self, bot_instance_id: uuid.UUID) -> dict[str, Any]:
        """Restart a bot instance Pod, tracking restart count.

        Deletes the existing Pod and creates a new one. Tracks restart
        count with a 10-minute sliding window. If max restarts (5) are
        exceeded in the window, marks the instance as 'failed' and
        notifies the tenant.

        Args:
            bot_instance_id: The bot instance UUID to restart.

        Returns:
            The updated bot_instance record.

        Raises:
            InstanceNotFoundError: If the bot instance does not exist.
            MaxRestartsExceededError: If restart limit is exceeded.
            OrchestratorError: On restart failure.
        """
        instance = self._get_instance(bot_instance_id)
        if instance is None:
            raise InstanceNotFoundError(
                f"Bot instance not found: {bot_instance_id}"
            )

        # Check restart count
        if self._is_restart_limit_exceeded(bot_instance_id):
            # Mark as failed
            self._update_instance_status(bot_instance_id, "failed")
            self._notify_tenant_failed(instance)
            log.error(
                "Max restarts (%d in %ds) exceeded for instance %s, marked as failed",
                MAX_RESTARTS, RESTART_WINDOW_SECONDS, bot_instance_id,
            )
            raise MaxRestartsExceededError(
                f"Bot instance {bot_instance_id} exceeded max restarts "
                f"({MAX_RESTARTS} in {RESTART_WINDOW_SECONDS}s). "
                f"Manual intervention required."
            )

        # Increment restart count
        self._increment_restart_count(bot_instance_id)

        # Delete the existing Pod (if any)
        pod_name = instance["pod_name"]
        if pod_name:
            try:
                api = self._get_k8s_core_api()
                api.delete_namespaced_pod(
                    name=pod_name,
                    namespace=POD_NAMESPACE,
                    body=k8s_client.V1DeleteOptions(grace_period_seconds=10),
                )
                log.info("Deleted Pod %s for restart of instance %s", pod_name, bot_instance_id)
            except ApiException as exc:
                if exc.status != 404:
                    log.error(
                        "Failed to delete Pod %s during restart: %s", pod_name, exc
                    )

        # Update status to provisioning during restart
        self._update_instance_status(bot_instance_id, "provisioning")

        # Determine tier from subscription
        tier = self._get_instance_tier(instance)

        # Create a new Pod
        try:
            pod_spec = self._build_pod_spec(
                instance_id=bot_instance_id,
                tenant_id=instance["tenant_id"],
                pod_name=pod_name,
                tier=tier,
            )
            self._create_pod(pod_spec)
            log.info(
                "Restarted instance %s (pod=%s), restart count incremented",
                bot_instance_id, pod_name,
            )
        except InsufficientResourcesError:
            self._update_instance_status(bot_instance_id, "pending_resources")
            log.warning(
                "Insufficient resources during restart of instance %s", bot_instance_id
            )
        except Exception as exc:
            self._update_instance_status(bot_instance_id, "error")
            log.error("Restart Pod creation failed for %s: %s", bot_instance_id, exc)
            raise OrchestratorError(f"Restart failed: {exc}") from exc

        return self._get_instance(bot_instance_id)

    def retry_pending_provisions(self) -> list[dict[str, Any]]:
        """Retry provisioning for instances in 'pending_resources' status.

        Called periodically (every 60s). Each instance gets up to 10 retry
        attempts before being marked as 'failed'.

        Returns:
            List of instance dicts that were retried.
        """
        redis_client = self._get_redis()
        retried = []

        pending_instances = self._get_instances_by_status("pending_resources")

        for instance in pending_instances:
            instance_id = instance["id"]
            retry_key = f"provision_retry:{instance_id}"

            # Get current retry count
            retry_count_str = redis_client.get(retry_key)
            retry_count = int(retry_count_str) if retry_count_str else 0

            if retry_count >= MAX_PROVISION_RETRIES:
                # Max retries exceeded — mark as failed
                self._update_instance_status(instance_id, "failed")
                self._notify_tenant_failed(instance)
                log.error(
                    "Max provision retries (%d) exhausted for instance %s, marked as failed",
                    MAX_PROVISION_RETRIES, instance_id,
                )
                # Clean up retry counter
                redis_client.delete(retry_key)
                continue

            # Attempt to create the Pod
            tier = self._get_instance_tier(instance)
            pod_name = instance["pod_name"]

            try:
                pod_spec = self._build_pod_spec(
                    instance_id=instance_id,
                    tenant_id=instance["tenant_id"],
                    pod_name=pod_name,
                    tier=tier,
                )
                self._create_pod(pod_spec)

                # Success — update status to provisioning
                self._update_instance_status(instance_id, "provisioning")
                redis_client.delete(retry_key)
                log.info(
                    "Retry provision succeeded for instance %s (attempt %d)",
                    instance_id, retry_count + 1,
                )
                retried.append(instance)

            except InsufficientResourcesError:
                # Still not enough resources — increment retry count
                redis_client.set(
                    retry_key,
                    str(retry_count + 1),
                    ex=MAX_PROVISION_RETRIES * PROVISION_RETRY_INTERVAL_SECONDS,
                )
                log.info(
                    "Retry provision still insufficient for instance %s (attempt %d/%d)",
                    instance_id, retry_count + 1, MAX_PROVISION_RETRIES,
                )
            except Exception as exc:
                log.error(
                    "Retry provision error for instance %s: %s", instance_id, exc
                )

        return retried

    # ------------------------------------------------------------------
    # Internal: Pod management
    # ------------------------------------------------------------------

    def _build_pod_spec(
        self,
        instance_id: uuid.UUID,
        tenant_id: uuid.UUID,
        pod_name: str,
        tier: str,
    ) -> k8s_client.V1Pod:
        """Build a Kubernetes Pod spec for a tenant bot instance.

        Args:
            instance_id: The bot instance UUID.
            tenant_id: The owning tenant UUID.
            pod_name: The Pod name.
            tier: Resource tier ('base' or 'video').

        Returns:
            A V1Pod object ready for creation.
        """
        limits = RESOURCE_LIMITS.get(tier, RESOURCE_LIMITS["base"])

        # Build resource requirements
        resources = k8s_client.V1ResourceRequirements(
            requests={
                "cpu": limits["cpu_request"],
                "memory": limits["memory_request"],
            },
            limits={
                "cpu": limits["cpu_limit"],
                "memory": limits["memory_limit"],
            },
        )

        # Add GPU resource for video tier
        if limits["gpu_vfs"] > 0:
            resources.requests["intel.com/sriov-gpudevice"] = str(limits["gpu_vfs"])
            resources.limits["intel.com/sriov-gpudevice"] = str(limits["gpu_vfs"])

        # Environment variables
        env_vars = [
            k8s_client.V1EnvVar(
                name="TENANT_ID", value=str(tenant_id)
            ),
            k8s_client.V1EnvVar(
                name="BOT_INSTANCE_ID", value=str(instance_id)
            ),
            k8s_client.V1EnvVar(
                name="HELLODJ_PG_URI",
                value_from=k8s_client.V1EnvVarSource(
                    secret_key_ref=k8s_client.V1SecretKeySelector(
                        name="hellodj-pg-uri", key="uri"
                    )
                ),
            ),
            k8s_client.V1EnvVar(
                name="HELLODJ_DB_KEY",
                value_from=k8s_client.V1EnvVarSource(
                    secret_key_ref=k8s_client.V1SecretKeySelector(
                        name="hellodj-db-key", key="key"
                    )
                ),
            ),
            k8s_client.V1EnvVar(
                name="LAVALINK_HOST", value=LAVALINK_HOST
            ),
            k8s_client.V1EnvVar(
                name="LAVALINK_PORT", value=LAVALINK_PORT
            ),
            k8s_client.V1EnvVar(
                name="HELLODJ_REDIS_URL",
                value=REDIS_URL,
            ),
        ]

        # Security context for video tier (GPU access)
        security_context = None
        volume_mounts = []
        volumes = []

        if tier == "video":
            security_context = k8s_client.V1SecurityContext(
                privileged=True,
            )
            volume_mounts.append(
                k8s_client.V1VolumeMount(
                    name="dev-dri",
                    mount_path="/dev/dri",
                )
            )
            volumes.append(
                k8s_client.V1Volume(
                    name="dev-dri",
                    host_path=k8s_client.V1HostPathVolumeSource(
                        path="/dev/dri",
                    ),
                )
            )

        # Lavalink config rendered volume (emptyDir for init container output)
        volumes.append(
            k8s_client.V1Volume(
                name="lavalink-config-rendered",
                empty_dir=k8s_client.V1EmptyDirVolumeSource(),
            )
        )

        # Init container: render-lavalink-config
        init_container = k8s_client.V1Container(
            name="render-lavalink-config",
            image=BOT_IMAGE,
            command=["python", "/app/render_lavalink_config.py", "/out/application.yml"],
            env=[
                k8s_client.V1EnvVar(
                    name="HELLODJ_DB_KEY",
                    value_from=k8s_client.V1EnvVarSource(
                        secret_key_ref=k8s_client.V1SecretKeySelector(
                            name="hellodj-db-key", key="key"
                        )
                    ),
                ),
                k8s_client.V1EnvVar(
                    name="HELLODJ_PG_URI",
                    value_from=k8s_client.V1EnvVarSource(
                        secret_key_ref=k8s_client.V1SecretKeySelector(
                            name="hellodj-pg-uri", key="uri"
                        )
                    ),
                ),
            ],
            volume_mounts=[
                k8s_client.V1VolumeMount(
                    name="lavalink-config-rendered",
                    mount_path="/out",
                ),
            ],
        )

        # Main bot container
        bot_container = k8s_client.V1Container(
            name="bot",
            image=BOT_IMAGE,
            env=env_vars,
            resources=resources,
            security_context=security_context,
            volume_mounts=volume_mounts,
        )

        # Pod security context
        pod_security_context = k8s_client.V1PodSecurityContext(
            run_as_user=1000,
            run_as_group=1000,
            fs_group=1000,
        )
        if tier == "video":
            pod_security_context.supplemental_groups = [26]

        # Build Pod
        pod = k8s_client.V1Pod(
            api_version="v1",
            kind="Pod",
            metadata=k8s_client.V1ObjectMeta(
                name=pod_name,
                namespace=POD_NAMESPACE,
                labels={
                    "app.kubernetes.io/name": "hellodj-tenant-bot",
                    "app.kubernetes.io/component": "bot",
                    "hellodj.celestium.life/tenant-id": str(tenant_id),
                    "hellodj.celestium.life/instance-id": str(instance_id),
                },
            ),
            spec=k8s_client.V1PodSpec(
                init_containers=[init_container],
                containers=[bot_container],
                volumes=volumes,
                security_context=pod_security_context,
                restart_policy="OnFailure",
            ),
        )

        return pod

    def _create_pod(self, pod: k8s_client.V1Pod) -> None:
        """Create a Pod in the cluster.

        Args:
            pod: The V1Pod spec to create.

        Raises:
            InsufficientResourcesError: If scheduling fails due to resource constraints.
            OrchestratorError: On other API errors.
        """
        api = self._get_k8s_core_api()
        try:
            api.create_namespaced_pod(
                namespace=POD_NAMESPACE,
                body=pod,
            )
        except ApiException as exc:
            # HTTP 403 or specific reason indicating resource exhaustion
            if exc.status == 403 or (
                exc.reason and "Forbidden" in str(exc.reason)
            ):
                raise InsufficientResourcesError(
                    f"Insufficient cluster resources to schedule Pod: {exc.reason}"
                ) from exc
            # HTTP 422 can indicate resource quota exceeded
            if exc.status == 422:
                body_str = str(exc.body) if exc.body else ""
                if "exceeded quota" in body_str.lower() or "insufficient" in body_str.lower():
                    raise InsufficientResourcesError(
                        f"Resource quota exceeded: {exc.body}"
                    ) from exc
            raise OrchestratorError(
                f"Kubernetes API error creating Pod: status={exc.status} reason={exc.reason}"
            ) from exc

    # ------------------------------------------------------------------
    # Internal: Database operations
    # ------------------------------------------------------------------

    def _get_instance(self, instance_id: uuid.UUID) -> dict[str, Any] | None:
        """Fetch a bot instance record from the database."""
        conn = self._get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM bot_instances WHERE id = %s",
                    (instance_id,),
                )
                row = cur.fetchone()
                return dict(row) if row else None
        finally:
            conn.close()

    def _get_instances_by_status(self, status: str) -> list[dict[str, Any]]:
        """Fetch all bot instances with the given status."""
        conn = self._get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM bot_instances WHERE status = %s",
                    (status,),
                )
                rows = cur.fetchall()
                return [dict(row) for row in rows]
        finally:
            conn.close()

    def _update_instance_status(
        self, instance_id: uuid.UUID, status: str, node_name: str | None = None
    ) -> None:
        """Update the status of a bot instance in the database."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                if node_name:
                    cur.execute(
                        "UPDATE bot_instances SET status = %s, node_name = %s WHERE id = %s",
                        (status, node_name, instance_id),
                    )
                else:
                    cur.execute(
                        "UPDATE bot_instances SET status = %s WHERE id = %s",
                        (status, instance_id),
                    )
                conn.commit()
                log.debug(
                    "Updated instance %s status to '%s'", instance_id, status
                )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Internal: Restart tracking (Redis)
    # ------------------------------------------------------------------

    def _is_restart_limit_exceeded(self, instance_id: uuid.UUID) -> bool:
        """Check if the instance has exceeded the restart limit.

        Uses Redis to track restart count within a sliding window.
        """
        redis_client = self._get_redis()
        count_key = f"{RESTART_COUNT_PREFIX}{instance_id}"
        window_key = f"{RESTART_WINDOW_PREFIX}{instance_id}"

        count_str = redis_client.get(count_key)
        if count_str is None:
            return False

        count = int(count_str)

        # Check if we're still within the window
        window_start_str = redis_client.get(window_key)
        if window_start_str is None:
            # No window — treat as fresh
            return False

        window_start = float(window_start_str)
        elapsed = time.time() - window_start

        if elapsed > RESTART_WINDOW_SECONDS:
            # Window expired — reset
            redis_client.delete(count_key, window_key)
            return False

        return count >= MAX_RESTARTS

    def _increment_restart_count(self, instance_id: uuid.UUID) -> int:
        """Increment the restart count for an instance within the tracking window.

        Returns the new restart count.
        """
        redis_client = self._get_redis()
        count_key = f"{RESTART_COUNT_PREFIX}{instance_id}"
        window_key = f"{RESTART_WINDOW_PREFIX}{instance_id}"

        # Check if window exists
        window_start_str = redis_client.get(window_key)
        now = time.time()

        if window_start_str is None or (now - float(window_start_str)) > RESTART_WINDOW_SECONDS:
            # Start a new window
            redis_client.set(window_key, str(now), ex=RESTART_WINDOW_SECONDS)
            redis_client.set(count_key, "1", ex=RESTART_WINDOW_SECONDS)
            return 1

        # Increment within existing window
        new_count = redis_client.incr(count_key)
        return new_count

    def _clear_restart_tracking(self, instance_id: uuid.UUID) -> None:
        """Clear restart tracking data for an instance."""
        try:
            redis_client = self._get_redis()
            count_key = f"{RESTART_COUNT_PREFIX}{instance_id}"
            window_key = f"{RESTART_WINDOW_PREFIX}{instance_id}"
            redis_client.delete(count_key, window_key)
        except Exception as exc:
            log.warning(
                "Failed to clear restart tracking for %s: %s", instance_id, exc
            )

    # ------------------------------------------------------------------
    # Internal: Tier detection
    # ------------------------------------------------------------------

    def _get_instance_tier(self, instance: dict[str, Any]) -> str:
        """Determine the resource tier for an instance based on its subscription.

        Queries the active subscription for the tenant to check for Video addon.
        """
        tenant_id = instance["tenant_id"]
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT addons FROM subscriptions
                    WHERE tenant_id = %s AND status = 'active'
                    ORDER BY started_at DESC
                    LIMIT 1
                    """,
                    (tenant_id,),
                )
                row = cur.fetchone()
                if row and row[0]:
                    addons = row[0]
                    if "video" in addons:
                        return "video"
        finally:
            conn.close()

        return "base"

    # ------------------------------------------------------------------
    # Internal: Tenant notification
    # ------------------------------------------------------------------

    def _notify_tenant_failed(self, instance: dict[str, Any]) -> None:
        """Notify a tenant that their bot instance has been marked as failed.

        Currently logs the notification. In production, this would send a
        WebSocket message, email, or Discord DM.
        """
        tenant_id = instance["tenant_id"]
        instance_id = instance["id"]
        log.warning(
            "NOTIFICATION: Tenant %s bot instance %s has been marked as FAILED. "
            "Manual intervention or support contact is required.",
            tenant_id, instance_id,
        )
        # TODO: Integrate with notification system (WebSocket push, email, Discord DM)
        # For now, publish to Redis so the web portal can display it
        try:
            redis_client = self._get_redis()
            redis_client.publish(
                f"bot_failed:{tenant_id}",
                str(instance_id),
            )
        except Exception as exc:
            log.warning(
                "Failed to publish failure notification for tenant %s: %s",
                tenant_id, exc,
            )
