"""Mock runbook library. Each runbook is a deterministic, scripted remediation (PRD: deterministic tools first)."""
from __future__ import annotations

from app.data.audit_ctx import audited_tool

RUNBOOKS: dict[str, dict] = {
    "RB-LC-01": {
        "name": "Restart LicenseCore licence worker pool", "systems": ["LicenseCore"], "kb_article": "KB4022",
        "signatures": ["health check failed", "api response time", "error rate", "latency", "unavailable", "500"],
        "reversible": True, "rollback": "Restore the previous worker-pool configuration snapshot",
        "steps": ["Drain the licence worker pool", "Restart workers in the LicenseCore v3 operations console",
                  "Clear the stale token cache", "Run the renewal smoke test"],
    },
    "RB-LC-02": {
        "name": "Fail LicenseCore database over to the secondary region", "systems": ["LicenseCore"],
        "kb_article": "KB4018", "signatures": ["database", "db connection"], "reversible": False,
        "rollback": "None - failback requires a maintenance window",
        "steps": ["Quiesce writers", "Promote the secondary region", "Repoint the connection string"],
    },
    "RB-BE-01": {
        "name": "Replay failed BillingEngine invoice batch", "systems": ["BillingEngine"], "kb_article": "KB4003",
        "signatures": ["queue depth", "batch job failure", "error rate", "duplicate transaction"],
        "reversible": True, "rollback": "Void the replayed invoices",
        "steps": ["Pause the invoice scheduler", "Replay the failed batch window", "Reconcile totals"],
    },
    "RB-NS-02": {                    # before RB-NS-01: its "notification" signature matches any NotificationService text
        "name": "Recycle the NotificationService database connection pool", "systems": ["NotificationService"],
        "kb_article": "KB4018", "signatures": ["api response time", "db connection pool"], "reversible": True,
        "rollback": "Restore the previous pool size from the config snapshot",
        "steps": ["Snapshot the pool configuration", "Drain and recycle idle connections",
                  "Check API response time is back under 5s"],
    },
    "RB-NS-01": {
        "name": "Flush the push notification queue", "systems": ["NotificationService"], "kb_article": "KB4016",
        "signatures": ["push notification", "email notification", "notification", "queue depth", "delayed"],
        "reversible": True, "rollback": "Re-enqueue from the dead-letter queue",
        "steps": ["Snapshot the queue", "Flush stalled partitions", "Resume consumers", "Send a canary notification"],
    },
    "RB-AU-01": {
        "name": "Re-sync the SSO signing certificate", "systems": ["AuthService"], "kb_article": "KB4011",
        "signatures": ["sso", "certificate", "login", "failed login"], "reversible": True,
        "rollback": "Re-import the previous certificate", "steps": ["Fetch the new signing certificate",
                                                                   "Update the relying-party trust", "Test an SSO login"],
    },
    "RB-RP-01": {
        "name": "Re-run failed report job for the affected window", "systems": ["ReportingPlatform", "DataWarehouse"],
        "kb_article": "KB4014", "signatures": ["report generation", "timing out", "pdf generation", "cache hit rate"],
        "reversible": True, "rollback": "Delete the regenerated output",
        "steps": ["Identify the failed step", "Re-run for the failed date range only", "Notify the requester"],
    },
    "RB-IH-01": {
        "name": "Clear the stuck integration queue and retry", "systems": ["IntegrationHub", "APIGateway"],
        "kb_article": "KB4006", "signatures": ["integration", "timing out", "error rate", "queue depth", "api"],
        "reversible": True, "rollback": "Replay the cleared messages from the audit topic",
        "steps": ["Pause the route", "Move poison messages aside", "Resume with exponential back-off"],
    },
    "RB-AU-02": {
        "name": "Reset the SSO session timeout policy to 30 minutes", "systems": ["AuthService"], "kb_article": "KB4011",
        "signatures": ["session timeout"], "reversible": True, "rollback": "Re-apply the previous session policy export",
        "steps": ["Export the current session policy", "Apply the 30-minute session policy", "Test an idle session"],
    },
    "RB-AU-03": {
        "name": "Re-register the MFA token for VPN-connected agents", "systems": ["AuthService"], "kb_article": "KB4011",
        "signatures": ["two-factor", "2fa", "mfa"], "reversible": True, "rollback": "Restore the previous token registration",
        "steps": ["Clear the stale MFA registration", "Re-sync the RADIUS shared secret", "Test a VPN login with MFA"],
    },
    "RB-AU-04": {
        "name": "Re-queue undelivered password-reset emails", "systems": ["AuthService", "NotificationService"],
        "kb_article": "KB4016", "signatures": ["emails not being delivered", "reset emails"], "reversible": True,
        "rollback": "Purge the re-queued messages from the outbound queue",
        "steps": ["Find messages stuck in the outbound queue", "Re-queue them", "Send a canary reset email"],
    },
    "RB-CP-01": {
        "name": "Restart the CustomerPortal mobile API pods", "systems": ["CustomerPortal"], "kb_article": "KB4012",
        "signatures": ["portal down", "mobile"], "reversible": True, "rollback": "Redeploy the previous pod revision",
        "steps": ["Drain the mobile API pods", "Restart with the last good revision", "Run the mobile smoke test"],
    },
    "RB-WF-01": {
        "name": "Re-run the workflow auto-close job", "systems": ["WorkflowEngine"], "kb_article": "KB4017",
        "signatures": ["auto-closing", "auto-close"], "reversible": True, "rollback": "Reopen the affected tasks",
        "steps": ["Find resolved tasks still open", "Re-run the auto-close job", "Reconcile the task counts"],
    },
    "RB-IH-02": {
        "name": "Re-encode and re-import the failed broker batch", "systems": ["IntegrationHub"], "kb_article": "KB4006",
        "signatures": ["bulk import", "encoding"], "reversible": True, "rollback": "Delete the re-imported batch",
        "steps": ["Convert the file to UTF-8", "Re-import the failed batch", "Reconcile record counts"],
    },
    "RB-DW-01": {
        "name": "Rebuild KPI dashboards from the last good snapshot", "systems": ["DataWarehouse"], "kb_article": "KB4028",
        "signatures": ["dashboard", "incorrect figures"], "reversible": True, "rollback": "Switch back to the current snapshot",
        "steps": ["Pin the last good snapshot", "Rebuild the dashboard datasets", "Compare key totals"],
    },
}


@audited_tool
def match(app_id: str | None, text: str) -> dict | None:
    """Return the first runbook whose system and signature both match (id included), else None."""
    if not app_id:
        return None
    t = text.lower()
    for rid, rb in RUNBOOKS.items():
        if app_id in rb["systems"] and any(sig in t for sig in rb["signatures"]):
            return {"id": rid, **rb}
    return None


def run(runbook_id: str) -> dict:
    rb = RUNBOOKS[runbook_id]
    return {"ok": True, "runbook_id": runbook_id, "name": rb["name"], "steps_run": rb["steps"],
            "reversible": rb["reversible"], "rollback": rb["rollback"]}
