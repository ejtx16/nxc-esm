import type { Approval, Decision } from "../api/types";

/** Plain-language reasons for guardrail rule keys (config/guardrail_policy.yaml). */
const RULE_TEXT: Record<string, string> = {
  allowed_priorities: "it's a high-priority incident",
  max_app_tier_blocked: "it affects a business-critical system",
  min_confidence: "the agent isn't confident enough",
  require_reversible: "it can't be undone",
  respect_freeze_windows: "it falls in a change freeze",
  min_kb_accuracy: "the runbook may be out of date",
  max_blast_radius_users: "it could affect many people",
  max_reopen_rate_pct: "this fix is often reopened",
  block_privileged_access: "it grants privileged access",
  always_human_action: "a person always signs this off",
  irreversible_on_tier_0: "it can't be undone on a critical system",
  agent_failure: "the agent hit an error",
  kill_switch: "the kill switch is on",
  never_suppress_severities: "critical alerts are never hidden",
  unknown_action: "there is no policy for this action",
};

const rule = (r: string) => RULE_TEXT[r] ?? r.replace(/_/g, " ");

export function whyHeld(a: Approval): string {
  const rules = a.failed_rules ?? [];
  if (!rules.length) return "Needs a person to check it.";
  const shown = rules.slice(0, 2).map(rule).join(" and ");
  const more = rules.length > 2 ? `, plus ${rules.length - 2} more reason${rules.length - 2 === 1 ? "" : "s"}` : "";
  return `Needs a person because ${shown}${more}.`;
}

const lowerFirst = (s: string) => s.charAt(0).toLowerCase() + s.slice(1);

export function approvalTitle(a: Approval): string {
  const s = a.summary;
  const auto = s.match(/^Auto-resolve (INC\d+) with runbook \S+ \((.+)\)$/);
  if (auto) return `Fix ${auto[1]} automatically: ${lowerFirst(auto[2])}`;
  const kb = s.match(/^Publish drafted update to (KB\d+)/);
  if (kb) return `Publish the updated ${kb[1]} article`;
  return s;
}

/** One short sentence for the activity feed. The raw text stays available as a tooltip. */
export function feedText(d: Decision): string {
  const raw = (d.reasoning || d.input_summary || "").trim();

  if (d.agent === "alert_correlation") {
    const m = raw.match(/^New cluster (CL-\d+): (?:classified )?(true_positive|false_positive|redundant)/);
    if (m) {
      if (/Proposing suppression/i.test(raw)) return "Looks like noise. Suggested hiding this group of alerts.";
      if (m[2] === "true_positive") return "Looks like a real problem. Kept it visible.";
      return "Probably noise, but not sure enough to hide it. Kept it visible.";
    }
    if (/^duplicate of|correlated with/.test(raw)) return "Added to an existing alert group.";
  }

  if (d.agent === "sr_fulfilment") {
    const intent = raw.match(/Intent: ([^\[;]+)/)?.[1]?.trim().toLowerCase();
    const who = raw.match(/Identity verified: ([^(;]+)/)?.[1]?.trim();
    if (intent && who) {
      if (/Routed to human/i.test(raw)) return `Checked ${who}'s identity and passed the ${intent} request to the service desk.`;
      if (/Eligible/i.test(raw)) return `Checked ${who}'s identity and handled the ${intent} request.`;
      return `Checked ${who}'s identity for a ${intent} request.`;
    }
  }

  if (d.agent === "kb_curation") {
    const m = raw.match(/^(\d+)% -> (archive|update|keep)(?: \((.*)\))?$/);
    if (m) {
      const verb = m[2] === "archive" ? "Suggested archiving" : m[2] === "update" ? "Suggested updating" : "Kept";
      return `${verb} an article scored ${m[1]}%${m[3] ? `: ${m[3].replace(/^./, (c) => c.toLowerCase())}` : ""}.`;
    }
  }

  return raw.replace(/^[A-Z]{2,3}\d+:\s*/, "");
}

/** The triage agent's rationale is a semicolon-joined log line. Turn each part into a short sentence. */
export function rationaleBullets(raw: string | null | undefined): string[] {
  if (!raw) return [];
  const cap = (t: string) => t.replace(/^./, (c) => c.toUpperCase());
  const strip = (t: string) => t.replace(/\.$/, "").trim();
  return raw.split(/;\s+|\.\s+(?=[A-Z])/).map(strip).filter(Boolean).map((part) => {
    let m = part.match(/^Category '([^']+)' from keywords \[(.*)\]$/);
    if (m) return `Categorised as ${m[1]}, because the description mentions ${m[2].replace(/'/g, "")}.`;
    m = part.match(/^priority (P\d) \((.*)\)$/i);
    if (m) return `Priority ${m[1]}: ${m[2].replace(/^./, (c) => c.toLowerCase())}.`;
    m = part.match(/^routed to (.+?) \(skills score \d+, workload (\d+)\/(\d+)\)$/i);
    if (m) return `Sent to ${m[1]}: the best skills match, with ${m[2]} of ${m[3]} tickets open.`;
    m = part.match(/^KB excluded below the (\d+)% accuracy threshold: (.*)$/i);
    if (m) return `Left out runbooks under ${m[1]}% accuracy: ${m[2].replace(/(\d+)\.\d+%/g, "$1%")}.`;
    m = part.match(/^Similar past incidents: (.*)$/i);
    if (m) return `Similar past incidents: ${m[1].replace(/\s*\([\d.]+\)/g, "")}.`;
    return `${cap(part)}.`;
  });
}
