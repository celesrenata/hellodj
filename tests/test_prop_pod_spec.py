"""Property-based test: Pod Spec Correctness Per Subscription Tier.

**Validates: Requirements 10.5, 11.1**

Property 8: For any subscription with a specific plan and set of addons, the
generated Pod spec SHALL contain resource requests/limits matching the tier
definition:
- Base (no video addon): 250m CPU, 512Mi RAM, NO GPU resource requests
- Video_Addon: 500m CPU, 1Gi RAM, 1 intel.com/sriov-gpudevice
- CUDA: 500m CPU, 1Gi RAM, 1 nvidia.com/gpu, node affinity to gremlin-1

Subscriptions without Video_Addon SHALL NOT include GPU resource requests.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

from hypothesis import given, settings

# Ensure web-ui/services is importable
_webui_dir = str(Path(__file__).resolve().parent.parent / "web-ui")
if _webui_dir not in sys.path:
    sys.path.insert(0, _webui_dir)

from services.pod_spec_builder import build_pod_spec

from tests.strategies import addon_sets, tenant_ids


# ---------------------------------------------------------------------------
# Property 8.1: Base plan (no video addon) pod spec correctness
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(tenant_id=tenant_ids, instance_id=tenant_ids)
def test_base_plan_pod_spec_has_correct_resources_no_gpu(
    tenant_id: uuid.UUID, instance_id: uuid.UUID
):
    """Property 8.1: For any tenant_id and instance_id, base plan (no video
    addon) pod specs have cpu=250m, memory=512Mi, and NO
    intel.com/sriov-gpudevice or nvidia.com/gpu in resource requests.

    **Validates: Requirements 10.5, 11.1**
    """
    # Build pod spec with no addons and no CUDA
    pod = build_pod_spec(
        instance_id=instance_id,
        tenant_id=tenant_id,
        addons=[],
        cuda=False,
    )

    # Find the bot container
    bot_container = next(
        c for c in pod.spec.containers if c.name == "bot"
    )

    requests = bot_container.resources.requests
    limits = bot_container.resources.limits

    # Verify base tier resource values
    assert requests["cpu"] == "250m", (
        f"Base plan CPU request should be 250m, got {requests['cpu']}"
    )
    assert requests["memory"] == "512Mi", (
        f"Base plan memory request should be 512Mi, got {requests['memory']}"
    )
    assert limits["cpu"] == "250m", (
        f"Base plan CPU limit should be 250m, got {limits['cpu']}"
    )
    assert limits["memory"] == "512Mi", (
        f"Base plan memory limit should be 512Mi, got {limits['memory']}"
    )

    # Verify NO GPU resources present
    assert "intel.com/sriov-gpudevice" not in requests, (
        "Base plan must NOT have intel.com/sriov-gpudevice in requests"
    )
    assert "nvidia.com/gpu" not in requests, (
        "Base plan must NOT have nvidia.com/gpu in requests"
    )
    assert "intel.com/sriov-gpudevice" not in limits, (
        "Base plan must NOT have intel.com/sriov-gpudevice in limits"
    )
    assert "nvidia.com/gpu" not in limits, (
        "Base plan must NOT have nvidia.com/gpu in limits"
    )


# ---------------------------------------------------------------------------
# Property 8.2: Video addon pod spec correctness
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(tenant_id=tenant_ids, instance_id=tenant_ids)
def test_video_addon_pod_spec_has_correct_resources_with_sriov(
    tenant_id: uuid.UUID, instance_id: uuid.UUID
):
    """Property 8.2: For any tenant_id and instance_id, video addon pod specs
    have cpu=500m, memory=1Gi, and intel.com/sriov-gpudevice: 1 in resource
    requests.

    **Validates: Requirements 10.5, 11.1**
    """
    # Build pod spec with video addon
    pod = build_pod_spec(
        instance_id=instance_id,
        tenant_id=tenant_id,
        addons=["video"],
        cuda=False,
    )

    # Find the bot container
    bot_container = next(
        c for c in pod.spec.containers if c.name == "bot"
    )

    requests = bot_container.resources.requests
    limits = bot_container.resources.limits

    # Verify video tier resource values
    assert requests["cpu"] == "500m", (
        f"Video addon CPU request should be 500m, got {requests['cpu']}"
    )
    assert requests["memory"] == "1Gi", (
        f"Video addon memory request should be 1Gi, got {requests['memory']}"
    )
    assert requests["intel.com/sriov-gpudevice"] == "1", (
        f"Video addon must have intel.com/sriov-gpudevice: 1 in requests, "
        f"got {requests.get('intel.com/sriov-gpudevice')}"
    )
    assert limits["cpu"] == "500m", (
        f"Video addon CPU limit should be 500m, got {limits['cpu']}"
    )
    assert limits["memory"] == "1Gi", (
        f"Video addon memory limit should be 1Gi, got {limits['memory']}"
    )
    assert limits["intel.com/sriov-gpudevice"] == "1", (
        f"Video addon must have intel.com/sriov-gpudevice: 1 in limits, "
        f"got {limits.get('intel.com/sriov-gpudevice')}"
    )

    # Verify NO NVIDIA GPU (that's CUDA-only)
    assert "nvidia.com/gpu" not in requests, (
        "Video addon must NOT have nvidia.com/gpu in requests"
    )
    assert "nvidia.com/gpu" not in limits, (
        "Video addon must NOT have nvidia.com/gpu in limits"
    )


# ---------------------------------------------------------------------------
# Property 8.3: CUDA pod spec correctness
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(tenant_id=tenant_ids, instance_id=tenant_ids)
def test_cuda_pod_spec_has_nvidia_gpu_and_node_affinity(
    tenant_id: uuid.UUID, instance_id: uuid.UUID
):
    """Property 8.3: CUDA pod specs have nvidia.com/gpu: 1 in resource requests
    and node affinity requiring scheduling on gremlin-1.

    **Validates: Requirements 10.5, 11.1**
    """
    # Build pod spec with CUDA enabled
    pod = build_pod_spec(
        instance_id=instance_id,
        tenant_id=tenant_id,
        addons=["video"],
        cuda=True,
    )

    # Find the bot container
    bot_container = next(
        c for c in pod.spec.containers if c.name == "bot"
    )

    requests = bot_container.resources.requests
    limits = bot_container.resources.limits

    # Verify CUDA tier resource values
    assert requests["cpu"] == "500m", (
        f"CUDA CPU request should be 500m, got {requests['cpu']}"
    )
    assert requests["memory"] == "1Gi", (
        f"CUDA memory request should be 1Gi, got {requests['memory']}"
    )
    assert requests["nvidia.com/gpu"] == "1", (
        f"CUDA must have nvidia.com/gpu: 1 in requests, "
        f"got {requests.get('nvidia.com/gpu')}"
    )
    assert limits["nvidia.com/gpu"] == "1", (
        f"CUDA must have nvidia.com/gpu: 1 in limits, "
        f"got {limits.get('nvidia.com/gpu')}"
    )

    # Verify node affinity to gremlin-1
    affinity = pod.spec.affinity
    assert affinity is not None, "CUDA pod must have affinity set"
    assert affinity.node_affinity is not None, "CUDA pod must have node_affinity"

    node_selector = (
        affinity.node_affinity.required_during_scheduling_ignored_during_execution
    )
    assert node_selector is not None, "CUDA pod must have required node selector"

    # Extract node selector terms and verify gremlin-1
    terms = node_selector.node_selector_terms
    assert len(terms) > 0, "CUDA pod must have at least one node selector term"

    # Check that at least one term matches gremlin-1
    found_gremlin_1 = False
    for term in terms:
        for expr in term.match_expressions:
            if (
                expr.key == "kubernetes.io/hostname"
                and expr.operator == "In"
                and "gremlin-1" in expr.values
            ):
                found_gremlin_1 = True
                break

    assert found_gremlin_1, (
        "CUDA pod must have node affinity to gremlin-1, "
        f"got terms: {terms}"
    )

    # Verify NO intel SR-IOV GPU (CUDA uses nvidia.com/gpu instead)
    assert "intel.com/sriov-gpudevice" not in requests, (
        "CUDA pod must NOT have intel.com/sriov-gpudevice in requests"
    )
