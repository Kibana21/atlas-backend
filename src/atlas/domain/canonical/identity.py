"""External identity normalization (ADR-0003, ADR-0019): the approved rules only.

``arm-1``, ``guid-1``, ``apim-api-1``, ``preserve-1`` (validation only, case preserved), ``k8s-uid-1``, ``org-key-1``
and ``source-1``. ``planned`` and ``reserved`` namespaces are rejected. Every rule validates and rejects; none
repairs a malformed key. The golden fixture's identities are the shared test oracle with atlas-specs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from atlas.domain.canonical.taxonomy import Taxonomy

GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
SOURCE_NAMESPACE_RE = re.compile(r"^source\.[a-z0-9][a-z0-9_-]*$")
APIM_API_RE = re.compile(r"^(?P<arm>/[^#]+)#api:(?P<api>[^;#\s]+);rev=(?P<rev>[^;#\s]+)$")
MAX_ARM_LENGTH = 2048


class NormalizationError(ValueError):
    """The key does not follow its namespace's approved format (H-08)."""


@dataclass(frozen=True)
class NormalizedKey:
    normalized_key: str
    key_parts: dict[str, Any]
    normalization_version: str


def normalize_arm(original: str) -> tuple[str, dict[str, Any]]:
    """Rule ``arm-1``: trim; drop one trailing '/'; lowercase; validate the segment structure."""
    s = original.strip()
    s = s.removesuffix("/")
    if len(s) > MAX_ARM_LENGTH:
        raise NormalizationError("ARM ID longer than 2048 characters")
    if any(c.isspace() for c in s) or "?" in s or "#" in s:
        raise NormalizationError("ARM ID contains whitespace, a query string or a fragment")
    if not s.startswith("/"):
        raise NormalizationError("ARM ID must start with '/'")
    segments = s[1:].split("/")
    if any(seg == "" for seg in segments):
        raise NormalizationError("ARM ID has an empty segment ('//')")
    if any(seg in (".", "..") for seg in segments):
        raise NormalizationError("ARM ID has a '.' or '..' segment")
    low = [seg.lower() for seg in segments]
    parts: dict[str, Any] = {
        "subscription_id": None,
        "resource_group": None,
        "provider_namespace": None,
        "type_path": None,
        "name_path": None,
    }
    i = 0
    if low[0] == "subscriptions":
        if len(low) < 2 or not GUID_RE.match(low[1]):
            raise NormalizationError("ARM ID must start with /subscriptions/{guid}")
        parts["subscription_id"] = low[1]
        i = 2
        if i < len(low) and low[i] == "resourcegroups":
            if i + 1 >= len(low):
                raise NormalizationError("ARM ID names resourceGroups without a name")
            parts["resource_group"] = low[i + 1]
            i += 2
    elif low[0] != "providers":
        raise NormalizationError("ARM ID must start with /subscriptions/ or /providers/")
    if i < len(low):
        if low[i] != "providers" or i + 1 >= len(low):
            raise NormalizationError("ARM ID: expected /providers/{namespace}")
        rest = low[i + 2 :]
        if not rest or len(rest) % 2:
            raise NormalizationError("ARM ID: provider part must alternate type and name segments")
        parts["provider_namespace"] = low[i + 1]
        parts["type_path"] = "/".join(rest[0::2])
        parts["name_path"] = "/".join(rest[1::2])
    return "/" + "/".join(low), parts


def normalize(tax: Taxonomy, namespace: str, original: str) -> NormalizedKey:
    """Normalize ``original`` under ``namespace``, or raise :class:`NormalizationError`."""
    ns = tax.namespaces.get(namespace)
    if ns is None and namespace.startswith("source."):
        if not SOURCE_NAMESPACE_RE.match(namespace):
            raise NormalizationError(f"invalid source namespace {namespace!r}")
        ns = tax.namespaces["source.*"]
    if ns is None:
        raise NormalizationError(f"unknown identity namespace {namespace!r}")
    if ns["status"] in ("planned", "reserved"):
        raise NormalizationError(f"namespace {namespace} is {ns['status']}: no approved normalization rule yet")
    rule: str = ns["normalization_rule"]
    s = original.strip()
    if rule == "arm-1":
        key, parts = normalize_arm(original)
        return NormalizedKey(key, parts, rule)
    if rule == "guid-1":
        if not GUID_RE.match(s):
            raise NormalizationError("not a canonical GUID")
        return NormalizedKey(s.lower(), {}, rule)
    if rule == "apim-api-1":
        m = APIM_API_RE.match(s)
        if not m:
            raise NormalizationError("apim.api key must be {arm}#api:{apiId};rev={revision}")
        arm, _ = normalize_arm(m["arm"])
        return NormalizedKey(
            f"{arm}#api:{m['api']};rev={m['rev']}",
            {"apim_service": arm, "api_id": m["api"], "revision": m["rev"]},
            rule,
        )
    if rule in ("preserve-1", "source-1"):
        if not re.match(ns["pattern"], s):
            raise NormalizationError(f"{namespace} key does not match its approved format {ns['format']}")
        return NormalizedKey(s, {}, rule)
    if rule == "k8s-uid-1":
        cluster, sep, uid = s.rpartition("#")
        cluster_ns, sep2, cluster_key = cluster.partition(":")
        if not sep or not sep2 or not GUID_RE.match(uid):
            raise NormalizationError("k8s.uid key must be {cluster namespace}:{cluster key}#{uid}")
        if cluster_ns == "k8s.uid":
            raise NormalizationError("a cluster key cannot itself be a k8s.uid")
        inner = normalize(tax, cluster_ns, cluster_key)
        return NormalizedKey(
            f"{cluster_ns}:{inner.normalized_key}#{uid.lower()}",
            {"cluster_namespace": cluster_ns, "cluster_key": inner.normalized_key},
            rule,
        )
    if rule == "org-key-1":
        if not s:
            raise NormalizationError("empty organization key")
        return NormalizedKey(s.lower(), {}, rule)
    raise NormalizationError(f"no implementation for rule {rule}")
