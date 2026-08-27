"""Unit tests for the Pod Spec Builder.

Tests that pod specs are generated correctly per subscription tier,
GPU scheduling, and resource constraints.

Requirements: 10.1, 10.3, 10.5, 11.1, 11.2, 11.3, 11.4, 11.5
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

# Ensure web-ui/ is importable
_webui_dir = str(Path(__file__).resolve().parent.parent / "web-ui")
if _webui_dir not in sys.path:
    sys.path.insert(0, _webui_dir)

from services.pod_spec_builder import (
    LAVALINK_HOST,
    NAMESPACE,
    RESOURCE_BASE,
    RESOURCE_CUDA,
    RESOURCE_VIDEO,
    SECRET_DB_KEY,
    SECRET_PG_URI,
    build_pod_spec,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tenant_id():
    return uuid.uuid4()


@pytest.fixture
def instance_id():
    return uuid.uuid4()


# ---------------------------------------------------------------------------
# Test: Pod naming and metadata
# ---------------------------------------------------------------------------


class TestPodMetadata:
    """Tests for pod naming, labels, and namespace."""

    def test_pod_name_format(self, instance_id, tenant_id):
        """Pod name uses first 8 chars of instance_id."""
        pod = build_pod_spec(instance_id, tenant_id)
        expected_name = f"tenant-bot-{str(instance_id)[:8]}"
        assert pod.metadata.name == expected_name

    def test_pod_namespace(self, instance_id, tenant_id):
        """Pod is created in hellodj-service namespace."""
        pod = build_pod_spec(instance_id, tenant_id)
        assert pod.metadata.namespace == NAMESPACE

    def test_pod_labels(self, instance_id, tenant_id):
        """Pod has required labels for identification and scheduling."""
        pod = build_pod_spec(instance_id, tenant_id)
        labels = pod.metadata.labels

        assert labels["app.kubernetes.io/name"] == "hellodj-tenant-bot"
        assert labels["app.kubernetes.io/component"] == "bot"
        assert labels["hellodj.celestium.life/tenant-id"] == str(tenant_id)
        assert labels["hellodj.celestium.life/instance-id"] == str(instance_id)

    def test_restart_policy(self, instance_id, tenant_id):
        """Pod restart policy is OnFailure."""
        pod = build_pod_spec(instance_id, tenant_id)
        assert pod.spec.restart_policy == "OnFailure"


# ---------------------------------------------------------------------------
# Test: Base Plan resources
# ---------------------------------------------------------------------------


class TestBasePlan:
    """Tests for Base_Plan pod spec (no addons)."""

    def test_base_resources(self, instance_id, tenant_id):
        """Base plan: 250m CPU, 512Mi RAM, no GPU."""
        pod = build_pod_spec(instance_id, tenant_id, addons=[])
        bot = _get_bot_container(pod)

        assert bot.resources.requests["cpu"] == "250m"
        assert bot.resources.requests["memory"] == "512Mi"
        assert "intel.com/sriov-gpudevice" not in bot.resources.requests
        assert "nvidia.com/gpu" not in bot.resources.requests

    def test_base_no_gpu_volumes(self, instance_id, tenant_id):
        """Base plan: no /dev/dri mount or hls-tmp volume."""
        pod = build_pod_spec(instance_id, tenant_id, addons=[])
        volume_names = [v.name for v in pod.spec.volumes]

        assert "dev-dri" not in volume_names
        assert "hls-tmp" not in volume_names

    def test_base_no_privileged(self, instance_id, tenant_id):
        """Base plan: bot container is not privileged."""
        pod = build_pod_spec(instance_id, tenant_id, addons=[])
        bot = _get_bot_container(pod)

        assert bot.security_context is None

    def test_base_no_supplemental_groups(self, instance_id, tenant_id):
        """Base plan: no supplementalGroups (no video group needed)."""
        pod = build_pod_spec(instance_id, tenant_id, addons=[])
        sc = pod.spec.security_context

        assert sc.supplemental_groups is None or sc.supplemental_groups == []

    def test_base_no_affinity(self, instance_id, tenant_id):
        """Base plan: no node affinity constraints."""
        pod = build_pod_spec(instance_id, tenant_id, addons=[])
        assert pod.spec.affinity is None


# ---------------------------------------------------------------------------
# Test: Video Addon resources
# ---------------------------------------------------------------------------


class TestVideoAddon:
    """Tests for Video_Addon pod spec."""

    def test_video_resources(self, instance_id, tenant_id):
        """Video addon: 500m CPU, 1Gi RAM, 1 intel.com/sriov-gpudevice."""
        pod = build_pod_spec(instance_id, tenant_id, addons=["video"])
        bot = _get_bot_container(pod)

        assert bot.resources.requests["cpu"] == "500m"
        assert bot.resources.requests["memory"] == "1Gi"
        assert bot.resources.requests["intel.com/sriov-gpudevice"] == "1"
        assert bot.resources.limits["intel.com/sriov-gpudevice"] == "1"

    def test_video_gpu_volumes(self, instance_id, tenant_id):
        """Video addon: /dev/dri hostPath and hls-tmp emptyDir volumes."""
        pod = build_pod_spec(instance_id, tenant_id, addons=["video"])
        volume_names = [v.name for v in pod.spec.volumes]

        assert "dev-dri" in volume_names
        assert "hls-tmp" in volume_names

        # Check dev-dri is hostPath
        dev_dri = _get_volume(pod, "dev-dri")
        assert dev_dri.host_path.path == "/dev/dri"
        assert dev_dri.host_path.type == "Directory"

        # Check hls-tmp is memory-backed emptyDir
        hls_tmp = _get_volume(pod, "hls-tmp")
        assert hls_tmp.empty_dir.medium == "Memory"
        assert hls_tmp.empty_dir.size_limit == "2Gi"

    def test_video_privileged(self, instance_id, tenant_id):
        """Video addon: bot container runs privileged for /dev/dri."""
        pod = build_pod_spec(instance_id, tenant_id, addons=["video"])
        bot = _get_bot_container(pod)

        assert bot.security_context is not None
        assert bot.security_context.privileged is True

    def test_video_supplemental_groups(self, instance_id, tenant_id):
        """Video addon: supplementalGroups includes 26 (video group)."""
        pod = build_pod_spec(instance_id, tenant_id, addons=["video"])
        sc = pod.spec.security_context

        assert 26 in sc.supplemental_groups

    def test_video_no_node_affinity(self, instance_id, tenant_id):
        """Video addon (Intel VF): NO node affinity, natural distribution."""
        pod = build_pod_spec(instance_id, tenant_id, addons=["video"])
        assert pod.spec.affinity is None

    def test_video_dev_dri_mount(self, instance_id, tenant_id):
        """Video addon: bot container mounts /dev/dri."""
        pod = build_pod_spec(instance_id, tenant_id, addons=["video"])
        bot = _get_bot_container(pod)
        mounts = {m.name: m for m in bot.volume_mounts}

        assert "dev-dri" in mounts
        assert mounts["dev-dri"].mount_path == "/dev/dri"

    def test_video_hls_tmp_mount(self, instance_id, tenant_id):
        """Video addon: bot container mounts /tmp/hellodj_hls."""
        pod = build_pod_spec(instance_id, tenant_id, addons=["video"])
        bot = _get_bot_container(pod)
        mounts = {m.name: m for m in bot.volume_mounts}

        assert "hls-tmp" in mounts
        assert mounts["hls-tmp"].mount_path == "/tmp/hellodj_hls"


# ---------------------------------------------------------------------------
# Test: CUDA workloads
# ---------------------------------------------------------------------------


class TestCudaWorkload:
    """Tests for CUDA/NVIDIA GPU pod spec."""

    def test_cuda_resources(self, instance_id, tenant_id):
        """CUDA: nvidia.com/gpu: 1 in resources."""
        pod = build_pod_spec(instance_id, tenant_id, cuda=True)
        bot = _get_bot_container(pod)

        assert bot.resources.requests["nvidia.com/gpu"] == "1"
        assert bot.resources.limits["nvidia.com/gpu"] == "1"
        assert "intel.com/sriov-gpudevice" not in bot.resources.requests

    def test_cuda_node_affinity_gremlin1(self, instance_id, tenant_id):
        """CUDA: node affinity constrains to gremlin-1."""
        pod = build_pod_spec(instance_id, tenant_id, cuda=True)
        affinity = pod.spec.affinity

        assert affinity is not None
        assert affinity.node_affinity is not None

        node_selector = affinity.node_affinity.required_during_scheduling_ignored_during_execution
        terms = node_selector.node_selector_terms
        assert len(terms) == 1

        expressions = terms[0].match_expressions
        assert len(expressions) == 1
        assert expressions[0].key == "kubernetes.io/hostname"
        assert expressions[0].operator == "In"
        assert expressions[0].values == ["gremlin-1"]

    def test_cuda_privileged(self, instance_id, tenant_id):
        """CUDA: bot container runs privileged."""
        pod = build_pod_spec(instance_id, tenant_id, cuda=True)
        bot = _get_bot_container(pod)

        assert bot.security_context.privileged is True

    def test_cuda_supplemental_groups(self, instance_id, tenant_id):
        """CUDA: supplementalGroups includes 26."""
        pod = build_pod_spec(instance_id, tenant_id, cuda=True)
        sc = pod.spec.security_context

        assert 26 in sc.supplemental_groups


# ---------------------------------------------------------------------------
# Test: Init container
# ---------------------------------------------------------------------------


class TestInitContainer:
    """Tests for the init container (render-lavalink-config)."""

    def test_init_container_present(self, instance_id, tenant_id):
        """Pod has exactly one init container."""
        pod = build_pod_spec(instance_id, tenant_id)
        assert len(pod.spec.init_containers) == 1
        assert pod.spec.init_containers[0].name == "render-lavalink-config"

    def test_init_container_command(self, instance_id, tenant_id):
        """Init container runs render_lavalink_config.py."""
        pod = build_pod_spec(instance_id, tenant_id)
        init = pod.spec.init_containers[0]

        assert init.command == [
            "python",
            "/app/render_lavalink_config.py",
            "/out/application.yml",
        ]

    def test_init_container_secrets(self, instance_id, tenant_id):
        """Init container has HELLODJ_DB_KEY and HELLODJ_PG_URI from secrets."""
        pod = build_pod_spec(instance_id, tenant_id)
        init = pod.spec.init_containers[0]
        env_names = {e.name for e in init.env}

        assert "HELLODJ_DB_KEY" in env_names
        assert "HELLODJ_PG_URI" in env_names

        # Verify they come from secretKeyRef
        for e in init.env:
            if e.name == "HELLODJ_DB_KEY":
                assert e.value_from.secret_key_ref.name == SECRET_DB_KEY
            if e.name == "HELLODJ_PG_URI":
                assert e.value_from.secret_key_ref.name == SECRET_PG_URI

    def test_init_container_volume_mount(self, instance_id, tenant_id):
        """Init container mounts /out for rendered config."""
        pod = build_pod_spec(instance_id, tenant_id)
        init = pod.spec.init_containers[0]
        mounts = {m.name: m for m in init.volume_mounts}

        assert "lavalink-config-rendered" in mounts
        assert mounts["lavalink-config-rendered"].mount_path == "/out"


# ---------------------------------------------------------------------------
# Test: Bot container environment
# ---------------------------------------------------------------------------


class TestBotContainerEnv:
    """Tests for bot container environment variables."""

    def test_tenant_id_env(self, instance_id, tenant_id):
        """Bot container has TENANT_ID env var with correct value."""
        pod = build_pod_spec(instance_id, tenant_id)
        bot = _get_bot_container(pod)
        env_map = {e.name: e for e in bot.env}

        assert "TENANT_ID" in env_map
        assert env_map["TENANT_ID"].value == str(tenant_id)

    def test_lavalink_host_env(self, instance_id, tenant_id):
        """Bot container has LAVALINK_HOST pointing to shared pool."""
        pod = build_pod_spec(instance_id, tenant_id)
        bot = _get_bot_container(pod)
        env_map = {e.name: e for e in bot.env}

        assert "LAVALINK_HOST" in env_map
        assert env_map["LAVALINK_HOST"].value == LAVALINK_HOST

    def test_secrets_from_k8s(self, instance_id, tenant_id):
        """Bot container gets HELLODJ_DB_KEY and HELLODJ_PG_URI from secrets."""
        pod = build_pod_spec(instance_id, tenant_id)
        bot = _get_bot_container(pod)
        env_map = {e.name: e for e in bot.env}

        assert "HELLODJ_DB_KEY" in env_map
        assert env_map["HELLODJ_DB_KEY"].value_from.secret_key_ref.name == SECRET_DB_KEY

        assert "HELLODJ_PG_URI" in env_map
        assert env_map["HELLODJ_PG_URI"].value_from.secret_key_ref.name == SECRET_PG_URI


# ---------------------------------------------------------------------------
# Test: Image tag
# ---------------------------------------------------------------------------


class TestImageTag:
    """Tests for image tag configuration."""

    def test_custom_image_tag(self, instance_id, tenant_id):
        """Custom image tag is used when provided."""
        pod = build_pod_spec(instance_id, tenant_id, image_tag="v2026-09-01")
        bot = _get_bot_container(pod)

        assert bot.image == "registry.celestium.life/hellodj/bot:v2026-09-01"

    def test_default_image_tag(self, instance_id, tenant_id):
        """Default image tag 'latest' is used when not provided."""
        pod = build_pod_spec(instance_id, tenant_id)
        bot = _get_bot_container(pod)

        # Image should end with some tag (default or env)
        assert bot.image.startswith("registry.celestium.life/hellodj/bot:")

    def test_init_and_bot_use_same_image(self, instance_id, tenant_id):
        """Init container and bot container use the same image."""
        pod = build_pod_spec(instance_id, tenant_id, image_tag="test-tag")
        init = pod.spec.init_containers[0]
        bot = _get_bot_container(pod)

        assert init.image == bot.image


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_bot_container(pod):
    """Get the bot container from a pod spec."""
    for c in pod.spec.containers:
        if c.name == "bot":
            return c
    raise ValueError("Bot container not found")


def _get_volume(pod, name: str):
    """Get a volume by name from a pod spec."""
    for v in pod.spec.volumes:
        if v.name == name:
            return v
    raise ValueError(f"Volume '{name}' not found")
