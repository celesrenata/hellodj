"""Pod Spec Builder for tenant bot instances.

Generates Kubernetes Pod specifications based on subscription tier,
addons, and GPU scheduling requirements.

Resource tiers:
  - Base_Plan: 250m CPU, 512Mi RAM, no GPU
  - Video_Addon: 500m CPU, 1Gi RAM, 1 intel.com/sriov-gpudevice,
    supplementalGroups [26], privileged, /dev/dri mount
  - CUDA workloads: nvidia.com/gpu: 1, node affinity → gremlin-1

No node affinity for Intel SR-IOV VFs — the device plugin handles
accounting and Kubernetes distributes naturally across all 4 gremlin nodes.

Requirements: 10.1, 10.3, 10.5, 11.1, 11.2, 11.3, 11.4, 11.5
"""

from __future__ import annotations

import os
import uuid

from kubernetes.client import (
    V1Affinity,
    V1Container,
    V1EmptyDirVolumeSource,
    V1EnvVar,
    V1EnvVarSource,
    V1HostPathVolumeSource,
    V1NodeAffinity,
    V1NodeSelector,
    V1NodeSelectorRequirement,
    V1NodeSelectorTerm,
    V1ObjectMeta,
    V1Pod,
    V1PodSecurityContext,
    V1PodSpec,
    V1ResourceRequirements,
    V1SecretKeySelector,
    V1SecurityContext,
    V1Volume,
    V1VolumeMount,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NAMESPACE = "hellodj-service"
BOT_IMAGE_REGISTRY = "registry.celestium.life/hellodj/bot"
DEFAULT_IMAGE_TAG = os.environ.get("HELLODJ_BOT_IMAGE_TAG", "latest")
LAVALINK_HOST = "lavalink-pool.hellodj-service.svc.cluster.local:2333"

# K8s secret names
SECRET_DB_KEY = "hellodj-db-key"
SECRET_PG_URI = "hellodj-pg-uri"

# Resource definitions per tier
RESOURCE_BASE = {
    "cpu": "250m",
    "memory": "512Mi",
}

RESOURCE_VIDEO = {
    "cpu": "500m",
    "memory": "1Gi",
    "intel.com/sriov-gpudevice": "1",
}

RESOURCE_CUDA = {
    "cpu": "500m",
    "memory": "1Gi",
    "nvidia.com/gpu": "1",
}


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_pod_spec(
    instance_id: uuid.UUID,
    tenant_id: uuid.UUID,
    addons: list[str] | None = None,
    image_tag: str | None = None,
    cuda: bool = False,
) -> V1Pod:
    """Build a Kubernetes Pod spec for a tenant bot instance.

    Args:
        instance_id: Unique identifier for the bot instance.
        tenant_id: Owning tenant's UUID.
        addons: List of active addon names (e.g., ['video', 'premium']).
        image_tag: Docker image tag override. Defaults to env or 'latest'.
        cuda: If True, schedule on gremlin-1 with NVIDIA GPU request.

    Returns:
        A fully-configured V1Pod object ready for creation via the K8s API.
    """
    addons = addons or []
    tag = image_tag or DEFAULT_IMAGE_TAG
    image = f"{BOT_IMAGE_REGISTRY}:{tag}"
    instance_id_short = str(instance_id)[:8]
    pod_name = f"tenant-bot-{instance_id_short}"

    has_video = "video" in addons

    # Determine resource tier
    if cuda:
        resources = RESOURCE_CUDA.copy()
    elif has_video:
        resources = RESOURCE_VIDEO.copy()
    else:
        resources = RESOURCE_BASE.copy()

    # --- Init container: render-lavalink-config ---
    init_container = _build_init_container(image)

    # --- Bot container ---
    bot_container = _build_bot_container(
        image=image,
        tenant_id=tenant_id,
        resources=resources,
        has_video=has_video,
        cuda=cuda,
    )

    # --- Volumes ---
    volumes = _build_volumes(has_video=has_video or cuda)

    # --- Pod security context ---
    pod_security_context = _build_pod_security_context(
        has_video=has_video, cuda=cuda
    )

    # --- Affinity (only for CUDA) ---
    affinity = _build_affinity(cuda=cuda)

    # --- Labels ---
    labels = {
        "app.kubernetes.io/name": "hellodj-tenant-bot",
        "app.kubernetes.io/component": "bot",
        "hellodj.celestium.life/tenant-id": str(tenant_id),
        "hellodj.celestium.life/instance-id": str(instance_id),
    }

    # --- Assemble Pod ---
    pod = V1Pod(
        api_version="v1",
        kind="Pod",
        metadata=V1ObjectMeta(
            name=pod_name,
            namespace=NAMESPACE,
            labels=labels,
        ),
        spec=V1PodSpec(
            init_containers=[init_container],
            containers=[bot_container],
            volumes=volumes,
            security_context=pod_security_context,
            restart_policy="OnFailure",
            affinity=affinity,
        ),
    )

    return pod


# ---------------------------------------------------------------------------
# Internal builders
# ---------------------------------------------------------------------------


def _build_init_container(image: str) -> V1Container:
    """Build the init container that renders Lavalink config from PG credentials."""
    return V1Container(
        name="render-lavalink-config",
        image=image,
        image_pull_policy="Always",
        command=["python", "/app/render_lavalink_config.py", "/out/application.yml"],
        env=[
            V1EnvVar(
                name="HELLODJ_DB_KEY",
                value_from=V1EnvVarSource(
                    secret_key_ref=V1SecretKeySelector(
                        name=SECRET_DB_KEY,
                        key="HELLODJ_DB_KEY",
                    )
                ),
            ),
            V1EnvVar(
                name="HELLODJ_PG_URI",
                value_from=V1EnvVarSource(
                    secret_key_ref=V1SecretKeySelector(
                        name=SECRET_PG_URI,
                        key="HELLODJ_PG_URI",
                    )
                ),
            ),
        ],
        volume_mounts=[
            V1VolumeMount(
                name="lavalink-config-rendered",
                mount_path="/out",
            ),
        ],
    )


def _build_bot_container(
    image: str,
    tenant_id: uuid.UUID,
    resources: dict[str, str],
    has_video: bool,
    cuda: bool,
) -> V1Container:
    """Build the main bot container with environment, resources, and mounts."""
    env_vars = [
        V1EnvVar(name="TENANT_ID", value=str(tenant_id)),
        V1EnvVar(
            name="HELLODJ_DB_KEY",
            value_from=V1EnvVarSource(
                secret_key_ref=V1SecretKeySelector(
                    name=SECRET_DB_KEY,
                    key="HELLODJ_DB_KEY",
                )
            ),
        ),
        V1EnvVar(
            name="HELLODJ_PG_URI",
            value_from=V1EnvVarSource(
                secret_key_ref=V1SecretKeySelector(
                    name=SECRET_PG_URI,
                    key="HELLODJ_PG_URI",
                )
            ),
        ),
        V1EnvVar(name="LAVALINK_HOST", value=LAVALINK_HOST),
    ]

    volume_mounts = [
        V1VolumeMount(
            name="lavalink-config-rendered",
            mount_path="/opt/Lavalink/application.yml",
            sub_path="application.yml",
            read_only=True,
        ),
    ]

    # GPU-enabled containers get /dev/dri mount and HLS tmpfs
    if has_video or cuda:
        volume_mounts.append(
            V1VolumeMount(
                name="dev-dri",
                mount_path="/dev/dri",
            )
        )
        volume_mounts.append(
            V1VolumeMount(
                name="hls-tmp",
                mount_path="/tmp/hellodj_hls",
            )
        )

    # Security context for GPU access
    container_security_context = None
    if has_video or cuda:
        container_security_context = V1SecurityContext(
            privileged=True,
        )

    # Resource requirements
    resource_requirements = V1ResourceRequirements(
        requests=resources.copy(),
        limits=resources.copy(),
    )

    return V1Container(
        name="bot",
        image=image,
        image_pull_policy="Always",
        env=env_vars,
        resources=resource_requirements,
        volume_mounts=volume_mounts,
        security_context=container_security_context,
    )


def _build_volumes(has_video: bool) -> list[V1Volume]:
    """Build the list of volumes for the pod."""
    volumes = [
        V1Volume(
            name="lavalink-config-rendered",
            empty_dir=V1EmptyDirVolumeSource(),
        ),
    ]

    if has_video:
        volumes.append(
            V1Volume(
                name="dev-dri",
                host_path=V1HostPathVolumeSource(
                    path="/dev/dri",
                    type="Directory",
                ),
            )
        )
        volumes.append(
            V1Volume(
                name="hls-tmp",
                empty_dir=V1EmptyDirVolumeSource(
                    medium="Memory",
                    size_limit="2Gi",
                ),
            )
        )

    return volumes


def _build_pod_security_context(
    has_video: bool, cuda: bool
) -> V1PodSecurityContext:
    """Build pod-level security context.

    Video addon requires supplementalGroups [26] for /dev/dri access.
    """
    context = V1PodSecurityContext(
        run_as_user=1000,
        run_as_group=1000,
        fs_group=1000,
    )

    if has_video or cuda:
        context.supplemental_groups = [26]

    return context


def _build_affinity(cuda: bool) -> V1Affinity | None:
    """Build node affinity for CUDA workloads (gremlin-1 only).

    Intel SR-IOV VFs have NO node affinity — Kubernetes distributes
    naturally across all 4 gremlin nodes based on device plugin capacity.
    """
    if not cuda:
        return None

    return V1Affinity(
        node_affinity=V1NodeAffinity(
            required_during_scheduling_ignored_during_execution=V1NodeSelector(
                node_selector_terms=[
                    V1NodeSelectorTerm(
                        match_expressions=[
                            V1NodeSelectorRequirement(
                                key="kubernetes.io/hostname",
                                operator="In",
                                values=["gremlin-1"],
                            )
                        ]
                    )
                ]
            )
        )
    )
