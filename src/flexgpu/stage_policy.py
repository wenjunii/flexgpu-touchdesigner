"""Deterministic TouchDesigner stage and AI-frame bridge role policy.

The TouchDesigner project embeds the same small policy so the saved ``.toe``
remains self-contained.  This importable reference keeps the role matrix easy
to test without TouchDesigner and gives launch/diagnostic tooling one canonical
description of what each process is expected to cook.
"""

from __future__ import annotations

from dataclasses import dataclass


_ROLES = {"standalone", "ai", "world"}
_TOPOLOGIES = {"single", "dual_local", "dual_network"}
_EXPERIENCES = {"installation", "vr", "combined"}


@dataclass(frozen=True)
class StagePolicy:
    """Resolved ownership and transport direction for one TD process."""

    role: str
    topology: str
    source_active: bool
    world_active: bool
    installation_active: bool
    vr_active: bool
    sender_active: bool
    receiver_active: bool
    bridge_mode: str
    route_index: int
    atlas_route_index: int

    @property
    def reconstruction_active(self) -> bool:
        return self.world_active

    @property
    def sensor_active(self) -> bool:
        return self.world_active


def _normal(value: object) -> str:
    return str(value).strip().lower().replace("-", "_")


def resolve_stage_policy(
    role: object,
    topology: object,
    experience: object,
    transport_type: object = "",
) -> StagePolicy:
    """Resolve a process role into cooking gates and bridge direction.

    ``render`` is accepted as the configuration-facing alias of the TD
    ``world`` process.  Topology is authoritative for the built-in bridge:
    ``dual_local`` defaults to loopback Touch TCP. Shared Mem is an explicit
    advanced choice requiring producer frame-state metadata outside this stage
    policy. ``dual_network`` always uses Touch In/Out TCP TOPs. The transport
    type is checked when supplied so a typo cannot silently activate the wrong
    endpoints.
    """

    normalized_role = _normal(role)
    if normalized_role == "render":
        normalized_role = "world"
    normalized_topology = _normal(topology)
    normalized_experience = _normal(experience)
    normalized_transport = _normal(transport_type)

    if normalized_role not in _ROLES:
        raise ValueError("role must be standalone, ai, world, or render")
    if normalized_topology not in _TOPOLOGIES:
        raise ValueError("topology must be single, dual_local, or dual_network")
    if normalized_experience not in _EXPERIENCES:
        raise ValueError("experience must be installation, vr, or combined")

    shared_types = {"shared_memory", "sharedmem"}
    touch_types = {"", "touch_tcp", "touch", "touch_in_out", "tcp"}
    if normalized_topology == "dual_local" and normalized_transport not in (
        shared_types | touch_types
    ):
        raise ValueError("dual_local bridge requires shared_memory or touch_tcp transport")
    if normalized_topology == "dual_network" and normalized_transport not in (
        touch_types
    ):
        raise ValueError("dual_network built-in bridge requires touch_tcp transport")

    bridge_transport = (
        "shared"
        if normalized_topology == "dual_local" and normalized_transport in shared_types
        else "tcp"
    )

    source_active = normalized_role in {"standalone", "ai"} or (
        normalized_role == "world" and normalized_topology == "single"
    )
    world_active = normalized_role in {"standalone", "world"}
    split_role = normalized_topology != "single" and normalized_role in {"ai", "world"}
    sender_active = split_role and normalized_role == "ai"
    receiver_active = split_role and normalized_role == "world"

    if sender_active:
        bridge_mode = "send_%s" % bridge_transport
        route_index = 0
    elif receiver_active:
        bridge_mode = "receive_%s" % bridge_transport
        route_index = 1
    else:
        bridge_mode = "local"
        route_index = 0

    return StagePolicy(
        role=normalized_role,
        topology=normalized_topology,
        source_active=source_active,
        world_active=world_active,
        installation_active=world_active
        and normalized_experience in {"installation", "combined"},
        vr_active=world_active and normalized_experience in {"vr", "combined"},
        sender_active=sender_active,
        receiver_active=receiver_active,
        bridge_mode=bridge_mode,
        route_index=route_index,
        atlas_route_index=1 if bridge_transport == "tcp" else 0,
    )
