import { Box, Flex, HStack, Text } from "@chakra-ui/react";
import { useEffect, useRef, useState } from "react";
import type { Decision } from "../api/types";
import { agentLabel } from "./ui";
import { feedText } from "../lib/humanize";

/** The nine agents from files/next-core-ai-agents.md, plus the operator. `partial` = folded into another agent today. */
type NodeId = "you" | "orch" | "alert" | "triage" | "rca" | "sr" | "kb" | "guardrail" | "resolution" | "insights";
const NODES: Record<NodeId, { x: number; y: number; name: string[]; partial?: boolean }> = {
  you: { x: 50, y: 85, name: ["You", "operator"] },
  orch: { x: 540, y: 85, name: ["Operations", "Orchestrator"] },
  alert: { x: 190, y: 235, name: ["Alert", "Correlation"] },
  triage: { x: 365, y: 235, name: ["Incident", "Triage"] },
  rca: { x: 540, y: 235, name: ["Root Cause", "Analysis"], partial: true },
  sr: { x: 715, y: 235, name: ["Service Request", "Fulfillment"] },
  kb: { x: 890, y: 235, name: ["Knowledge", "Curation"] },
  guardrail: { x: 365, y: 400, name: ["Guardrail", "and Risk"] },
  resolution: { x: 540, y: 400, name: ["Resolution", "Automation"], partial: true },
  insights: { x: 715, y: 400, name: ["Insights and", "Improvement"], partial: true },
};

const R = 24;          // icon circle radius
const LABEL_H = 40;    // two-line name pill below the icon
const FONT = 15;
const bottom = (n: NodeId) => NODES[n].y + R + LABEL_H + 4;
const top = (n: NodeId) => NODES[n].y - R;

/** Elbow connector like the reference diagram: down, across, down. */
function elbow(from: NodeId, to: NodeId, midY: number) {
  const a = NODES[from], b = NODES[to];
  return `M${a.x},${bottom(from)} V${midY} H${b.x} V${top(to)}`;
}
const side = (from: NodeId, to: NodeId) => {
  const a = NODES[from], b = NODES[to];
  const dir = Math.sign(b.x - a.x);
  return `M${a.x + dir * R},${a.y} H${b.x - dir * R}`;
};

const EDGES: { from: NodeId; to: NodeId; d: string; dashed?: boolean }[] = [
  { from: "you", to: "orch", d: side("you", "orch") },
  ...(["alert", "triage", "rca", "sr", "kb"] as const).map((n) => ({ from: "orch" as NodeId, to: n, d: elbow("orch", n, 185) })),
  ...(["alert", "triage", "sr", "kb"] as const).map((n) => ({ from: n, to: "guardrail" as NodeId, d: elbow(n, "guardrail", 340) })),
  { from: "triage", to: "rca", d: side("triage", "rca") },
  { from: "rca", to: "resolution", d: `M540,${bottom("rca")} V${top("resolution")}` },
  { from: "guardrail", to: "resolution", d: side("guardrail", "resolution") },
  { from: "resolution", to: "insights", d: side("resolution", "insights") },
  // Held actions go back to the operator for a decision.
  { from: "guardrail", to: "you", d: `M${365 - R},400 H${50} V${bottom("you")}`, dashed: true },
];

const TTL_MS = 2600;

/** Which nodes a decision row lights up. Every agent event is routed by the orchestrator first. */
function nodesFor(d: Decision): NodeId[] {
  if (d.kind === "human" || d.kind === "queued") return ["you"];
  if (d.kind === "guardrail") return d.guardrail_verdict && d.guardrail_verdict !== "Approve" ? ["guardrail", "you"] : ["guardrail"];
  if (d.kind === "execution") {
    const action = (d.proposed_action as { action?: string } | null)?.action;
    return action === "resolve" ? ["resolution", "insights"] : ["insights"];
  }
  const agent: Record<string, NodeId> = {
    orchestrator: "orch", alert_correlation: "alert", incident_triage: "triage", sr_fulfilment: "sr", kb_curation: "kb",
  };
  const n = agent[d.agent];
  if (!n) return [];
  if (n === "orch") return ["orch"];
  // Similar-incident and runbook matching is the RCA work done inside triage today.
  const rca = n === "triage" && /Similar past incidents|Runbook/.test(d.reasoning ?? "");
  return rca ? ["orch", n, "rca"] : ["orch", n];
}

const reducedMotion = () => typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

export default function AgentFlow({ feed }: { feed: Decision[] | null | undefined }) {
  const seen = useRef<number | null>(null);
  const [lit, setLit] = useState<Partial<Record<NodeId, number>>>({});
  const [latest, setLatest] = useState<Decision | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!feed?.length) return;
    const maxId = Math.max(...feed.map((d) => d.decision_id));
    if (seen.current === null) { seen.current = maxId; return; }   // don't replay history on first load
    const fresh = feed.filter((d) => d.decision_id > seen.current!);
    seen.current = Math.max(seen.current, maxId);
    if (!fresh.length) return;
    const t = Date.now();
    setLit((prev) => {
      const next = { ...prev };
      for (const d of fresh) for (const n of nodesFor(d)) next[n] = t;
      return next;
    });
    setLatest(fresh.reduce((a, b) => (b.decision_id > a.decision_id ? b : a)));
  }, [feed]);

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 400);
    return () => clearInterval(id);
  }, []);

  const active = (n: NodeId) => (lit[n] ?? 0) > now - TTL_MS;
  const anyActive = (Object.keys(NODES) as NodeId[]).some(active);
  const motion = !reducedMotion();

  return (
    <Box>
      <Box overflowX="auto">
        <Box minW="680px" px="3" pt="2">
          <svg viewBox="0 0 1000 480" width="100%" role="img"
            aria-label="Agent team: the operator feeds the Operations Orchestrator, which routes work to Alert Correlation, Incident Triage, Root Cause Analysis, Service Request Fulfillment and Knowledge Curation. Their actions pass the Guardrail and Risk agent to Resolution Automation, and outcomes feed Insights and Improvement.">
            <style>{`
              .af-edge { fill: none; stroke: #c6cfd6; stroke-width: 2; transition: stroke .3s; }
              .af-edge.on { stroke: #f2b705; stroke-width: 3; stroke-dasharray: 8 6; }
              .af-edge.held { stroke-dasharray: 4 5; }
              .af-ring { fill: none; stroke: #f2b705; stroke-width: 3; opacity: 0; }
              @media (prefers-reduced-motion: no-preference) {
                .af-edge.on { animation: af-flow .8s linear infinite; }
                .af-ring.on { animation: af-pulse 1.3s ease-out infinite; }
              }
              @keyframes af-flow { to { stroke-dashoffset: -28; } }
              @keyframes af-pulse { 0% { opacity: .9; transform: scale(1); } 100% { opacity: 0; transform: scale(1.7); } }
            `}</style>

            {/* Team boundary, as in the reference: everything except the operator. */}
            <rect x="100" y="16" width="888" height="458" rx="14" fill="none" stroke="#6f7f8b" strokeDasharray="8 7" />
            <g>
              <rect x="460" y="2" width="160" height="28" rx="6" fill="#13222b" />
              <text x="540" y="21" textAnchor="middle" fontSize={FONT} fontWeight="600" fill="#fff">Multi-agent team</text>
            </g>

            {EDGES.map((e) => {
              const on = active(e.from) && active(e.to);
              return (
                <g key={`${e.from}-${e.to}`}>
                  <path d={e.d} className={`af-edge${on ? " on" : ""}${e.dashed && !on ? " held" : ""}`} />
                  {on && motion && (
                    <circle r="5" fill="#f2b705">
                      <animateMotion dur="1.1s" repeatCount="indefinite" path={e.d} />
                    </circle>
                  )}
                </g>
              );
            })}

            {(Object.keys(NODES) as NodeId[]).map((id) => {
              const n = NODES[id], on = active(id);
              const w = id === "you" ? 90 : 162;
              return (
                <g key={id}>
                  <circle cx={n.x} cy={n.y} r={R} className={`af-ring${on ? " on" : ""}`} style={{ transformBox: "fill-box", transformOrigin: "center" }} />
                  <circle cx={n.x} cy={n.y} r={R} fill={on ? "#f2b705" : "#f6f8f9"} stroke="#13222b" strokeWidth="2"
                    strokeDasharray={n.partial ? "5 4" : undefined} style={{ transition: "fill .3s" }} />
                  {/* Person icon: head and shoulders. */}
                  <circle cx={n.x} cy={n.y - 6} r="7" fill="none" stroke="#13222b" strokeWidth="2.5" />
                  <path d={`M${n.x - 12},${n.y + 15} a12,11 0 0 1 24,0`} fill="none" stroke="#13222b" strokeWidth="2.5" strokeLinecap="round" />
                  <rect x={n.x - w / 2} y={n.y + R + 4} width={w} height={LABEL_H} rx="5"
                    fill={on ? "#13222b" : "#47596a"} />
                  {n.name.map((line, i) => (
                    <text key={line} x={n.x} y={n.y + R + 21 + i * 17} textAnchor="middle" fontSize={FONT} fontWeight="600" fill="#fff">{line}</text>
                  ))}
                </g>
              );
            })}
          </svg>
        </Box>
      </Box>
      <Flex px="5" py="3" gap="3" align="center" wrap="wrap" borderTopWidth="1px" borderColor="border.muted">
        <HStack gap="2" flexShrink={0}>
          <Box w="2.5" h="2.5" rounded="full" bg={anyActive ? "barrier" : "border.emphasized"} />
          <Text fontWeight="600">{anyActive ? "Working" : "Idle"}</Text>
        </HStack>
        <Text color="fg.muted" flex="1" minW="0" truncate>
          {latest ? `${agentLabel(latest.kind === "human" ? "human" : latest.kind === "guardrail" ? "guardrail" : latest.agent)}: ${feedText(latest)}`
            : "Start a scenario and each agent lights up as it works."}
        </Text>
        <Text fontSize="sm" color="fg.subtle" flexShrink={0}>Dashed = partly built today</Text>
      </Flex>
    </Box>
  );
}
