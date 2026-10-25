"""In-database integrity checks (ADR-0008 §3 step 7, §5; A2-T15: HARD rules as SQL).

:func:`revision_findings` runs inside the publishing transaction for the revision being written; tests run it for
every revision of loaded fixtures, plus :func:`history_findings`. Each returns readable findings ("RULE: message");
an empty list means the check passed. All queries are limited to one organization.
"""

# ruff: noqa: S608 - every statement below is a module constant; values are always bound parameters

from __future__ import annotations

from typing import Any

import psycopg

from atlas.persistence.canonical.repository import OrgScope

Connection = psycopg.Connection[Any]
MAX_CONTAINMENT_DEPTH = 64  # bound for the cycle walk only; real depths are <= 8 (ADR-0001)

_AT = "{t}.org_id = %(org)s AND {t}.revision_from <= %(rev)s AND ({t}.revision_to IS NULL OR {t}.revision_to > %(rev)s)"
PRESENT_ENTITIES = (
    f"SELECT ev.entity_id FROM atlas.entity_version ev WHERE {_AT.format(t='ev')} AND ev.record_state = 'present'"
)
PRESENT_RELATIONSHIPS = (
    "SELECT r.id, r.rel_type, r.from_id, r.to_id FROM atlas.relationship_version rv "
    f"JOIN atlas.relationship r ON r.org_id = rv.org_id AND r.id = rv.relationship_id "
    f"WHERE {_AT.format(t='rv')} AND rv.record_state = 'present'"
)

REVISION_CHECKS: dict[str, str] = {
    "H-10 relationship with an endpoint that is not present": f"""
        WITH e AS ({PRESENT_ENTITIES}), r AS ({PRESENT_RELATIONSHIPS})
        SELECT r.id::text FROM r
        WHERE r.from_id NOT IN (SELECT entity_id FROM e) OR r.to_id NOT IN (SELECT entity_id FROM e)
    """,
    "H-14 entity with more than one containment parent": f"""
        WITH r AS ({PRESENT_RELATIONSHIPS})
        SELECT r.to_id::text FROM r WHERE r.rel_type = 'CONTAINS' GROUP BY r.to_id HAVING count(*) > 1
    """,
    "H-14 containment cycle": f"""
        WITH RECURSIVE r AS ({PRESENT_RELATIONSHIPS}),
        c AS (SELECT from_id AS parent, to_id AS child FROM r WHERE rel_type = 'CONTAINS'),
        walk(start, node, depth) AS (
            SELECT child, parent, 1 FROM c
            UNION ALL
            SELECT w.start, c.parent, w.depth + 1 FROM walk w JOIN c ON c.child = w.node
            WHERE w.node <> w.start AND w.depth < {MAX_CONTAINMENT_DEPTH}
        )
        SELECT DISTINCT start::text FROM walk WHERE node = start
    """,
    "D-09 container_id differs from the CONTAINS parent": f"""
        WITH r AS ({PRESENT_RELATIONSHIPS}),
        p AS (SELECT to_id AS child, min(from_id::text) AS parent FROM r WHERE rel_type = 'CONTAINS' GROUP BY to_id)
        SELECT ev.entity_id::text FROM atlas.entity_version ev LEFT JOIN p ON p.child = ev.entity_id
        WHERE {_AT.format(t="ev")} AND ev.record_state = 'present' AND ev.container_id::text IS DISTINCT FROM p.parent
    """,
    "H-06 fact without exactly one current version": """
        SELECT e.id::text FROM atlas.entity e WHERE e.org_id = %(org)s AND e.created_revision <= %(rev)s
          AND (SELECT count(*) FROM atlas.entity_version v WHERE v.org_id = e.org_id AND v.entity_id = e.id
               AND v.revision_from <= %(rev)s AND (v.revision_to IS NULL OR v.revision_to > %(rev)s)) <> 1
        UNION ALL
        SELECT r.id::text FROM atlas.relationship r WHERE r.org_id = %(org)s AND r.created_revision <= %(rev)s
          AND (SELECT count(*) FROM atlas.relationship_version v WHERE v.org_id = r.org_id AND v.relationship_id = r.id
               AND v.revision_from <= %(rev)s AND (v.revision_to IS NULL OR v.revision_to > %(rev)s)) <> 1
    """,
    "H-12 assertion whose evidence was recorded later": """
        SELECT a.id::text FROM atlas.fact_assertion a
        JOIN atlas.evidence e ON e.org_id = a.org_id AND e.id = a.evidence_id
        WHERE a.org_id = %(org)s AND a.revision_from = %(rev)s AND e.change_revision > a.revision_from
    """,
}

HISTORY_CHECKS: dict[str, str] = {
    "H-05 revisions are not gap-free from 1": """
        SELECT revision::text FROM (SELECT revision, row_number() OVER (ORDER BY revision) AS n
                                    FROM atlas.change_set WHERE org_id = %(org)s) s WHERE revision <> n
    """,
    "H-05 revision counter differs from the last change set": """
        SELECT o.last::text FROM atlas.org_revision o WHERE o.org_id = %(org)s
          AND o.last <> COALESCE((SELECT max(revision) FROM atlas.change_set WHERE org_id = %(org)s), 0)
    """,
    "H-06 versions overlap or leave a gap": """
        SELECT entity_id::text FROM (SELECT entity_id, revision_from,
                 lag(revision_to) OVER (PARTITION BY entity_id ORDER BY revision_from) AS prev_to,
                 row_number() OVER (PARTITION BY entity_id ORDER BY revision_from) AS n
               FROM atlas.entity_version WHERE org_id = %(org)s) v
        WHERE n > 1 AND prev_to IS DISTINCT FROM revision_from
        UNION ALL
        SELECT relationship_id::text FROM (SELECT relationship_id, revision_from,
                 lag(revision_to) OVER (PARTITION BY relationship_id ORDER BY revision_from) AS prev_to,
                 row_number() OVER (PARTITION BY relationship_id ORDER BY revision_from) AS n
               FROM atlas.relationship_version WHERE org_id = %(org)s) v
        WHERE n > 1 AND prev_to IS DISTINCT FROM revision_from
    """,
    "H-05 created_revision differs from the first version": """
        SELECT e.id::text FROM atlas.entity e WHERE e.org_id = %(org)s
          AND e.created_revision <> (SELECT min(revision_from) FROM atlas.entity_version v
                                     WHERE v.org_id = e.org_id AND v.entity_id = e.id)
        UNION ALL
        SELECT r.id::text FROM atlas.relationship r WHERE r.org_id = %(org)s
          AND r.created_revision <> (SELECT min(revision_from) FROM atlas.relationship_version v
                                     WHERE v.org_id = r.org_id AND v.relationship_id = r.id)
    """,
}


def _run(conn: Connection, checks: dict[str, str], params: dict[str, Any], limit: int = 5) -> list[str]:
    findings: list[str] = []
    for label, sql in checks.items():
        rows = conn.execute(sql, params).fetchall()
        if rows:
            sample = ", ".join(str(r[0]) for r in rows[:limit])
            findings.append(f"{label} (r{params.get('rev', '-')}: {len(rows)}; {sample})")
    return findings


def revision_findings(conn: Connection, scope: OrgScope, revision: int) -> list[str]:
    return _run(conn, REVISION_CHECKS, {"org": scope.org_id, "rev": revision})


def history_findings(conn: Connection, scope: OrgScope) -> list[str]:
    return _run(conn, HISTORY_CHECKS, {"org": scope.org_id})
