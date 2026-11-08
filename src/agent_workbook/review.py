"""Approver attestations bound to immutable artifact content, with revocation."""

from .coordination import append_event, authorize_capability, current_principal


def review_artifact(workbook, ref, *, note="", decision="approve", principal=None):
    principal = principal or current_principal()
    authorize_capability(principal, "approve")
    if decision not in {"approve", "revoke"}:
        raise ValueError("Review decision must be approve or revoke")
    if not isinstance(note, str) or len(note) > 1000:
        raise ValueError("Approval note exceeds 1000 characters")
    with workbook.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = workbook._get(db, ref)
        state = workbook._status(db, row["artifact_id"])
        if decision == "approve" and (not state["is_latest"] or state["stale_inputs"]):
            raise ValueError(
                "Approval requires current inputs and current artifact version"
            )
        event_id = append_event(
            db,
            principal,
            "approval.accepted" if decision == "approve" else "approval.revoked",
            row["artifact_id"],
            {"sha256": row["digest"], "note": note},
        )
    return {
        "artifact_id": row["artifact_id"],
        "sha256": row["digest"],
        "event_id": event_id,
        "approver": principal.identity,
        "decision": decision,
    }
