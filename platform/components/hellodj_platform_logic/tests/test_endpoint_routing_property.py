"""Property-based test for cross-stage endpoint routing (task 5.3).

Feature: hellodj-nix-native-delivery, Property 9

Property 9 (A request routes only to the stage whose endpoint it targets):
*for any* set of stage endpoints with distinct hostnames on the single shared
GPU host, ``route_endpoint`` SHALL return exactly the endpoint whose hostname
equals the requested hostname -- and only that stage's endpoint -- when the
hostname matches one of the endpoints, and SHALL return ``None`` (routing
nowhere) when the hostname matches no endpoint. This encodes cross-stage
routing isolation on the shared host: a request targeting one Stage_Endpoint
reaches only that stage's workload, never another's (R8.7).

Validates: Requirements 8.7
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from hellodj_platform_logic.endpoint_routing import route_endpoint
from hellodj_platform_logic.types import DeploymentStage, StageEndpoint

# Hostname labels for the generated endpoints. A lowercase-alnum charset keeps
# datasets cheap; routing reasons purely over exact string equality of the
# hostname, so any non-empty distinct label exercises the logic.
_HOSTNAME = st.text(
    alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("z")),
    min_size=1,
    max_size=20,
)

# Namespace / port fields are carried through untouched by the router; small
# generators keep the endpoints realistic without affecting the decision.
_NAMESPACE = st.text(
    alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("z")),
    min_size=1,
    max_size=12,
)
_PORT = st.integers(min_value=1, max_value=65535)
_STAGES: list[DeploymentStage] = list(DeploymentStage)


@st.composite
def endpoints_with_distinct_hostnames(
    draw: st.DrawFn,
) -> list[StageEndpoint]:
    """Generate a non-empty set of endpoints with pairwise-distinct hostnames.

    Each stage owns ``<stage>.<region>.hellodj.bot``; the design guarantees the
    endpoints have distinct hostnames, so the generator draws a set of unique
    hostname labels and pairs each with a distinct stage. This spans the
    single-endpoint case up through the three Beta/Staging/Production endpoints.
    """
    hostnames = draw(
        st.lists(_HOSTNAME, min_size=1, max_size=len(_STAGES), unique=True)
    )
    endpoints: list[StageEndpoint] = []
    for stage, hostname in zip(_STAGES, hostnames):
        endpoints.append(
            StageEndpoint(
                stage=stage,
                namespace=draw(_NAMESPACE),
                port=draw(_PORT),
                hostname=hostname,
            )
        )
    return endpoints


@settings(max_examples=200)
@given(
    endpoints=endpoints_with_distinct_hostnames(),
    index=st.integers(min_value=0, max_value=len(_STAGES) - 1),
)
def test_known_hostname_routes_only_to_that_stage(
    endpoints: list[StageEndpoint],
    index: int,
) -> None:
    """Feature: hellodj-nix-native-delivery, Property 9.

    Validates: Requirements 8.7
    """
    # Pick one of the generated endpoints as the request target.
    target = endpoints[index % len(endpoints)]

    result = route_endpoint(target.hostname, endpoints)

    # --- Exact-stage match (R8.7) -----------------------------------------
    # The request routes to exactly the endpoint whose hostname it targets...
    assert result is not None
    assert result == target
    assert result.hostname == target.hostname
    assert result.stage is target.stage

    # ...and to no other stage's workload: because hostnames are distinct, the
    # routed endpoint is the unique one carrying the target hostname.
    matches = [e for e in endpoints if e.hostname == target.hostname]
    assert matches == [target]


@st.composite
def endpoints_and_absent_hostname(
    draw: st.DrawFn,
) -> tuple[list[StageEndpoint], str]:
    """Generate an endpoint set plus a hostname matching none of them.

    Returns ``(endpoints, absent_hostname)`` where ``absent_hostname`` is
    guaranteed not to equal any endpoint's hostname, so ``route_endpoint`` must
    route nowhere (``None``) -- a request that targets no Stage_Endpoint reaches
    no stage.
    """
    endpoints = draw(endpoints_with_distinct_hostnames())
    existing = {e.hostname for e in endpoints}
    absent = draw(_HOSTNAME.filter(lambda h: h not in existing))
    return endpoints, absent


@settings(max_examples=200)
@given(scenario=endpoints_and_absent_hostname())
def test_absent_hostname_routes_nowhere(
    scenario: tuple[list[StageEndpoint], str],
) -> None:
    """Feature: hellodj-nix-native-delivery, Property 9.

    Validates: Requirements 8.7
    """
    endpoints, absent_hostname = scenario

    # A hostname matching no endpoint routes nowhere (None), preserving
    # cross-stage isolation -- it is never mis-routed to some stage's workload.
    assert route_endpoint(absent_hostname, endpoints) is None
