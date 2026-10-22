import { Badge, Box, Code, Flex, Grid, HStack, Stack, Table, Text } from "@chakra-ui/react";
import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import { usePoll, useShell } from "../api/state";
import type { AgentMetrics, AuditSpan, TraceDetail, TraceRow } from "../api/types";
import {
  Card, Disclosure, Empty, Page, PageHeader, Seg, SelectField, Toggle, NINE_AGENTS, agentLabel, pct, sentence,
} from "../components/ui";

const TYPE_LABEL: Record<string, string> = {
  node: "Step", llm: "Claude", tool: "Tool", guardrail: "Policy check", human: "Person", orchestrator: "Orchestrator",
};
const TYPE_PALETTE: Record<string, string> = {
  node: "gray", llm: "purple", tool: "blue", guardrail: "orange", human: "green", orchestrator: "red",
};

const ms = (v?: number | null) => (v == null ? "—" : v >= 1000 ? `${(v / 1000).toFixed(2)} s` : v > 0 && v < 0.01 ? "<0.01 ms" : `${v.toFixed(v < 10 ? 2 : 0)} ms`);
const EXPORTS: [string, string][] = [["spans", "Spans"], ["decisions", "Decisions"], ["llm_calls", "Claude calls"], ["accuracy", "Accuracy"]];
const num = (v?: number | null) => (v == null ? "—" : v.toLocaleString("en-GB"));
const usd = (v?: number | null) => (v ? `$${v.toFixed(v < 0.01 ? 4 : 2)}` : "$0");

export default function AuditTrail() {
  const { overview } = useShell();
  const [params, setParams] = useSearchParams();
  const [scope, setScope] = useState<"run" | "all">("run");
  const [agentKey, setAgentKey] = useState("");
  const agent = NINE_AGENTS.find((a) => a.key === agentKey)?.id ?? "";
  const [problems, setProblems] = useState(false);
  const runId = scope === "run" ? overview?.run?.run_id ?? undefined : undefined;
  const entity = params.get("entity") ?? undefined;
  const selected = params.get("trace");

  const metrics = usePoll(() => api.auditMetrics({ run_id: runId, agent: agent || undefined }), 4000, [runId, agent]);
  const traces = usePoll(() => api.auditTraces({ run_id: runId, agent: agent || undefined, entity,
                                                 status: problems ? "error" : undefined, limit: 80 }), 3000,
                         [runId, agent, entity, problems]);
  const select = (id: string | null) => {
    const next = new URLSearchParams(params);
    if (id) next.set("trace", id); else next.delete("trace");
    setParams(next, { replace: true });
  };
  useEffect(() => {
    if (!selected && traces.data?.length) select(traces.data[0].trace_id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [traces.data, selected]);

  const m = metrics.data;
  return (
    <Page>
      <PageHeader title="Audit trail">
        Every step an agent takes, every tool and Claude call it makes, and every policy check and human decision, linked
        into one trace per event. Use it to see how many steps work took, what it cost, how long it took and how often the
        agents were right.
      </PageHeader>

      <Flex gap="4" wrap="wrap" align="flex-end">
        <Seg label="Scope" value={scope} onChange={setScope}
          items={[{ value: "run", label: "Current run" }, { value: "all", label: "Everything" }]} />
        <SelectField label="Agent" value={agentKey} onChange={setAgentKey} w="340px">
          <option value="">All agents</option>
          {NINE_AGENTS.map((a) => (
            <option key={a.key} value={a.key} disabled={!a.id}>{a.label}{a.note ? ` (${a.note})` : ""}</option>
          ))}
        </SelectField>
        <HStack gap="2" pb="1"><Toggle amber on={problems} onChange={setProblems} label="Only traces with problems" /><Text>Only problems</Text></HStack>
        {entity && <Badge size="lg" variant="outline" cursor="pointer" onClick={() => { params.delete("entity"); setParams(params); }}>{entity} ✕</Badge>}
        <Box flex="1" />
        <HStack gap="2" pb="1" wrap="wrap">
          <Text fontSize="sm" color="fg.muted">Export</Text>
          {EXPORTS.map(([t, label]) => (
            <a key={t} href={api.auditExportUrl(t, runId)} style={{ fontSize: 14, fontWeight: 600, textDecoration: "underline" }}>{label}</a>
          ))}
        </HStack>
      </Flex>

      {m ? <Summary t={m.totals} /> : <Text color="fg.muted">Loading audit metrics…</Text>}

      {m && Object.keys(m.agents).length > 0 && <AgentTable agents={m.agents} />}

      <Grid templateColumns={{ base: "1fr", lg: "minmax(0, 5fr) minmax(0, 7fr)" }} gap="6" alignItems="start">
        <TraceList rows={traces.data} selected={selected} onSelect={select} />
        {selected ? <TracePanel id={selected} /> : <Card><Empty title="No trace selected">Pick an event on the left.</Empty></Card>}
      </Grid>

      {m && <AccuracyDetail t={m.totals} batch={m.batch_evals} />}
    </Page>
  );
}

function Tile({ dim, value, unit, note }: { dim: string; value: string | number; unit: string; note: string }) {
  return (
    <Box px="5" py="4" borderColor="border.muted" borderRightWidth="1px" borderBottomWidth="1px">
      <Text fontSize="sm" fontWeight="600" color="fg.muted" textTransform="uppercase" letterSpacing="wide">{dim}</Text>
      <Text fontSize="3xl" fontWeight="bold" lineHeight="1.1" mt="1">{value}</Text>
      <Text fontSize="sm">{unit}</Text>
      <Text fontSize="xs" color="fg.subtle" mt="1">{note}</Text>
    </Box>
  );
}

function Summary({ t }: { t: AgentMetrics }) {
  const node = t.latency.node;
  return (
    <Box bg="bg.panel" borderWidth="1px" borderColor="border" rounded="l1" overflow="hidden">
      <Grid templateColumns={{ base: "1fr 1fr", md: "repeat(3, 1fr)", xl: "repeat(6, 1fr)" }} mr="-1px" mb="-1px">
        <Tile dim="Iteration" value={t.iteration.avg_steps || "—"} unit="steps per event" note={`max ${t.iteration.max_steps} · ${t.iteration.retries} Claude retries`} />
        <Tile dim="Tools" value={num(t.tools.calls)} unit="tool calls" note={`${t.tools.errors} failed · ${t.tools.by_tool.length} distinct tools`} />
        <Tile dim="Tokens" value={num(t.tokens.input + t.tokens.output)} unit={`in + out · ${usd(t.tokens.cost_usd)}`} note={`${num(t.tokens.cache_read)} read from cache · ${t.tokens.calls} calls`} />
        <Tile dim="Accuracy" value={pct(t.accuracy.accuracy_pct, 1)} unit="correct" note={`${t.accuracy.graded} decisions graded against ground truth`} />
        <Tile dim="Activity" value={num(t.activity.decisions)} unit="decisions and actions" note={`${t.activity.human_decisions} by people · ${t.activity.override_rate_pct == null ? "no" : pct(t.activity.override_rate_pct)} overridden`} />
        <Tile dim="Latency" value={ms(node?.p95_ms)} unit="per step, 95th percentile" note={`median ${ms(node?.p50_ms)} · slowest ${ms(node?.max_ms)}`} />
      </Grid>
    </Box>
  );
}

function AgentTable({ agents }: { agents: Record<string, AgentMetrics> }) {
  const rows = Object.entries(agents).sort((a, b) => b[1].activity.decisions - a[1].activity.decisions);
  return (
    <Card title="By agent" flush>
      <Box overflowX="auto">
        <Table.Root size="sm" variant="line">
          <Table.Header><Table.Row>
            {["Agent", "Steps run", "Retries", "Tool calls", "Tokens in / out", "Cost", "Accuracy", "Decisions", "p95 latency"].map((h, i) =>
              <Table.ColumnHeader key={h} textAlign={i ? "end" : "start"}>{h}</Table.ColumnHeader>)}
          </Table.Row></Table.Header>
          <Table.Body>{rows.map(([name, a]) => {
            const lat = a.latency.node ?? a.latency.tool ?? a.latency.guardrail ?? a.latency.human;
            return (
              <Table.Row key={name}>
                <Table.Cell fontWeight="600">{agentLabel(name)}</Table.Cell>
                <Table.Cell textAlign="end">{num(a.iteration.node_runs)}</Table.Cell>
                <Table.Cell textAlign="end">{a.iteration.retries}</Table.Cell>
                <Table.Cell textAlign="end">{num(a.tools.calls)}</Table.Cell>
                <Table.Cell textAlign="end">{num(a.tokens.input)} / {num(a.tokens.output)}</Table.Cell>
                <Table.Cell textAlign="end">{usd(a.tokens.cost_usd)}</Table.Cell>
                <Table.Cell textAlign="end">{a.accuracy.graded ? `${pct(a.accuracy.accuracy_pct, 1)} (${a.accuracy.graded})` : "—"}</Table.Cell>
                <Table.Cell textAlign="end">{num(a.activity.decisions)}</Table.Cell>
                <Table.Cell textAlign="end">{ms(lat?.p95_ms)}</Table.Cell>
              </Table.Row>
            );
          })}</Table.Body>
        </Table.Root>
      </Box>
    </Card>
  );
}

function TraceList({ rows, selected, onSelect }: { rows: TraceRow[] | null; selected: string | null; onSelect: (id: string) => void }) {
  return (
    <Card title="Traces" flush>
      {!rows ? <Text p="5" color="fg.muted">Loading…</Text> : rows.length === 0 ? (
        <Empty title="Nothing recorded yet">Start a scenario on the Live feed or submit a self-service request.</Empty>
      ) : (
        <Stack gap="0" maxH="720px" overflowY="auto">
          {rows.map((r) => (
            <Box as="button" key={r.trace_id} onClick={() => onSelect(r.trace_id)} textAlign="start" px="5" py="3"
              borderBottomWidth="1px" borderColor="border.muted" bg={r.trace_id === selected ? "bg.muted" : undefined}
              borderLeftWidth="3px" borderLeftColor={r.trace_id === selected ? "barrier" : "transparent"} _hover={{ bg: "bg.subtle" }}
              aria-current={r.trace_id === selected ? "true" : undefined}>
              <Flex justify="space-between" gap="2">
                <Text fontWeight="600" truncate>{r.event_type ? sentence(r.event_type.replace(".", " ")) : "Operator action"} · {r.entity_ref ?? "—"}</Text>
                <Text fontSize="sm" color="fg.subtle" flex="none">{r.started_sim?.slice(11, 19)}</Text>
              </Flex>
              <HStack gap="3" fontSize="sm" color="fg.muted" wrap="wrap">
                <span>{r.steps} steps</span><span>{r.tool_calls} tools</span>
                {r.llm_calls > 0 && <span>{r.llm_calls} Claude{r.retries ? ` (${r.retries} retries)` : ""}</span>}
                {r.cost_usd > 0 && <span>{usd(r.cost_usd)}</span>}
                <span>{ms(r.latency_ms)}</span>
                {r.accuracy && <Badge colorPalette={r.accuracy.correct === r.accuracy.n ? "green" : "red"} variant="subtle">{r.accuracy.correct}/{r.accuracy.n} correct</Badge>}
                {r.issues > 0 && <Badge colorPalette="red" variant="subtle">{r.issues} problem{r.issues > 1 ? "s" : ""}</Badge>}
              </HStack>
            </Box>
          ))}
        </Stack>
      )}
    </Card>
  );
}

function depthOf(spans: AuditSpan[]) {
  const byId = new Map(spans.map((s) => [s.span_id, s]));
  return (s: AuditSpan) => { let d = 0; let p = s.parent_span_id; while (p && byId.has(p) && d < 6) { d++; p = byId.get(p)!.parent_span_id; } return d; };
}

function TracePanel({ id }: { id: string }) {
  const { data, error } = usePoll(() => api.auditTrace(id), 5000, [id]);
  if (error) return <Card><Empty title="Trace not available">{error}</Empty></Card>;
  if (!data) return <Card><Text color="fg.muted">Loading trace…</Text></Card>;
  return <TraceBody t={data} />;
}

function TraceBody({ t }: { t: TraceDetail }) {
  const depth = useMemo(() => depthOf(t.spans), [t.spans]);
  const total = Math.max(1, ...t.spans.map((s) => s.offset_ms + s.latency_ms));
  const llmBySpan = new Map(t.llm_calls.map((c) => [c.span_id, c]));
  const decById = new Map(t.decisions.map((d) => [d.decision_id, d]));
  const cost = t.llm_calls.reduce((a, c) => a + (c.cost_usd ?? 0), 0);
  return (
    <Card title={t.event ? `${sentence(t.event.type.replace(".", " "))} · ${t.event.entity_ref}` : "Operator action"} flush>
      <HStack px="5" py="3" gap="5" wrap="wrap" fontSize="sm" color="fg.muted" borderBottomWidth="1px" borderColor="border.muted">
        <span>{t.spans.filter((s) => s.span_type === "node").length} steps</span>
        <span>{t.spans.filter((s) => s.span_type === "tool").length} tool calls</span>
        <span>{t.llm_calls.length} Claude calls · {usd(cost)}</span>
        <span>{t.decisions.length} decisions</span>
        <span>{ms(total)} end to end</span>
        <Code fontSize="xs">{t.trace_id}</Code>
      </HStack>
      <Stack gap="0">
        {t.spans.map((s) => (
          <SpanRow key={s.span_id} s={s} depth={depth(s)} total={total} llm={llmBySpan.get(s.span_id)}
            decision={s.decision_id ? decById.get(s.decision_id) : undefined}
            accuracy={t.accuracy.filter((a) => a.decision_id != null && a.decision_id === s.decision_id)} />
        ))}
      </Stack>
    </Card>
  );
}

function SpanRow({ s, depth, total, llm, decision, accuracy }: {
  s: AuditSpan; depth: number; total: number; llm?: TraceDetail["llm_calls"][number];
  decision?: TraceDetail["decisions"][number]; accuracy: TraceDetail["accuracy"];
}) {
  const [open, setOpen] = useState(false);
  const left = (s.offset_ms / total) * 100;
  const width = Math.max(0.6, (s.latency_ms / total) * 100);
  const bad = s.status !== "ok";
  return (
    <Box borderBottomWidth="1px" borderColor="border.muted" _last={{ borderBottomWidth: 0 }}>
      <Grid as="button" w="full" textAlign="start" onClick={() => setOpen(!open)} aria-expanded={open}
        templateColumns={{ base: "1fr", md: "minmax(0, 1fr) 160px" }} gap="3" px="5" py="2" pl={`${20 + depth * 18}px`} _hover={{ bg: "bg.subtle" }}>
        <Flex gap="2" align="center" minW="0">
          <Text as="span" aria-hidden w="3" color="fg.subtle">{open ? "▾" : "▸"}</Text>
          {s.span_type === "node"
            ? <Badge variant="outline" flex="none">Step {s.iteration}</Badge>
            : <Badge colorPalette={TYPE_PALETTE[s.span_type]} variant="subtle" flex="none">{TYPE_LABEL[s.span_type] ?? s.span_type}</Badge>}
          {s.attempt && s.attempt > 1 && <Badge colorPalette="orange" variant="outline" flex="none">Try {s.attempt}</Badge>}
          <Text fontWeight={s.span_type === "node" ? "600" : "400"} truncate>{s.name}</Text>
          {s.agent && s.span_type === "node" && <Text fontSize="sm" color="fg.subtle" truncate display={{ base: "none", md: "block" }}>{agentLabel(s.agent)}</Text>}
          {llm && <Text fontSize="sm" color="fg.muted" flex="none">{num((llm.input_tokens ?? 0) + (llm.output_tokens ?? 0))} tok · {usd(llm.cost_usd)}</Text>}
          {accuracy.map((a) => <Badge key={a.id} colorPalette={a.correct ? "green" : a.correct === 0 ? "red" : "gray"} variant="solid" flex="none">{a.correct ? "correct" : a.correct === 0 ? "wrong" : a.metric}</Badge>)}
          {bad && <Badge colorPalette="red" variant="solid" flex="none">{s.status}</Badge>}
        </Flex>
        <Flex align="center" gap="2">
          <Box position="relative" flex="1" h="2" bg="bg.muted" rounded="full" aria-hidden>
            <Box position="absolute" left={`${left}%`} w={`${width}%`} h="full" rounded="full" bg={bad ? "red.500" : `${TYPE_PALETTE[s.span_type]}.500`} />
          </Box>
          <Text fontSize="xs" color="fg.muted" w="58px" textAlign="end">{ms(s.latency_ms)}</Text>
        </Flex>
      </Grid>
      {open && (
        <Stack gap="3" px="5" pb="4" pl={`${44 + depth * 18}px`} fontSize="sm">
          {s.error && <Text color="red.600">{s.error}</Text>}
          {decision && (
            <Box>
              <Text fontWeight="600">Reasoning{decision.confidence != null ? ` (confidence ${decision.confidence.toFixed(2)})` : ""}</Text>
              <Text color="fg.muted">{decision.reasoning || decision.input_summary}</Text>
            </Box>
          )}
          {llm?.thinking_summary && (
            <Box><Text fontWeight="600">Claude's thinking (summary)</Text><Text color="fg.muted" whiteSpace="pre-wrap">{llm.thinking_summary}</Text></Box>
          )}
          {llm && (
            <Text color="fg.muted">
              {llm.model} · effort {llm.effort} · in {num(llm.input_tokens)} / out {num(llm.output_tokens)} / cache read {num(llm.cache_read)}
              {llm.stop_reason ? ` · ${llm.stop_reason}` : ""}{llm.error ? ` · ${llm.error}` : ""}
            </Text>
          )}
          {accuracy.map((a) => (
            <Text key={a.id} color="fg.muted">{sentence(a.metric)}: predicted <b>{a.predicted}</b>, expected <b>{a.expected}</b> ({a.source.replace("_", " ")})</Text>
          ))}
          {s.input && <JsonBlock label="Input" value={s.input} />}
          {s.output != null && <JsonBlock label="Output" value={s.output} />}
          <Text color="fg.subtle" fontSize="xs">{s.started_sim} · span {s.span_id}{s.decision_id ? ` · decision #${s.decision_id}` : ""}</Text>
        </Stack>
      )}
    </Box>
  );
}

function JsonBlock({ label, value }: { label: string; value: unknown }) {
  return (
    <Box>
      <Text fontWeight="600">{label}</Text>
      <Code display="block" whiteSpace="pre-wrap" p="2" maxH="200px" overflow="auto" fontSize="xs">{typeof value === "string" ? value : JSON.stringify(value, null, 2)}</Code>
    </Box>
  );
}

function AccuracyDetail({ t, batch }: { t: AgentMetrics; batch: { triage?: { n: number; category_acc_pct: number; priority_within_1_pct: number; modes?: string[] }; alert_classification?: { n: number; accuracy_pct: number } } | null }) {
  const sources: Record<string, string> = {
    true_positive: "Alert classification vs scenario ground truth", golden: "Triage vs hand-labelled golden set",
    human_override: "Proposals a person approved (vs rejected)", kb_rescore: "Knowledge articles above threshold after update",
    batch_eval: "Batch evaluation on the golden set",
  };
  return (
    <Disclosure title="How accuracy is measured">
      <Stack gap="4">
        <Table.Root size="sm" variant="line">
          <Table.Header><Table.Row><Table.ColumnHeader>Source</Table.ColumnHeader><Table.ColumnHeader textAlign="end">Graded</Table.ColumnHeader><Table.ColumnHeader textAlign="end">Correct</Table.ColumnHeader></Table.Row></Table.Header>
          <Table.Body>{Object.entries(t.accuracy.by_source).map(([k, v]) => (
            <Table.Row key={k}><Table.Cell>{sources[k] ?? k}</Table.Cell><Table.Cell textAlign="end">{v.n}</Table.Cell><Table.Cell textAlign="end">{pct(v.accuracy_pct, 1)}</Table.Cell></Table.Row>
          ))}</Table.Body>
        </Table.Root>
        {t.accuracy.calibration.length > 0 && (
          <Box>
            <Text fontWeight="600" mb="1">Calibration: does stated confidence match how often the agent is right?</Text>
            <Table.Root size="sm" variant="line">
              <Table.Header><Table.Row><Table.ColumnHeader>Confidence band</Table.ColumnHeader><Table.ColumnHeader textAlign="end">Decisions</Table.ColumnHeader><Table.ColumnHeader textAlign="end">Avg confidence</Table.ColumnHeader><Table.ColumnHeader textAlign="end">Actually correct</Table.ColumnHeader></Table.Row></Table.Header>
              <Table.Body>{t.accuracy.calibration.map((c) => (
                <Table.Row key={c.bucket}><Table.Cell>{c.bucket}</Table.Cell><Table.Cell textAlign="end">{c.n}</Table.Cell><Table.Cell textAlign="end">{(c.avg_confidence * 100).toFixed(0)}%</Table.Cell><Table.Cell textAlign="end">{pct(c.accuracy_pct, 1)}</Table.Cell></Table.Row>
              ))}</Table.Body>
            </Table.Root>
          </Box>
        )}
        {batch?.triage && (
          <Text color="fg.muted">
            Latest batch evaluation ({(batch.triage.modes ?? ["rules"]).join(", ")}): triage category {pct(batch.triage.category_acc_pct, 1)} and priority within one
            level {pct(batch.triage.priority_within_1_pct, 1)} on {batch.triage.n} golden incidents
            {batch.alert_classification ? `; alert classification ${pct(batch.alert_classification.accuracy_pct, 1)} on ${batch.alert_classification.n} seed alerts` : ""}.
          </Text>
        )}
        <Text color="fg.subtle" fontSize="sm">Ground truth comes from the scenario and the golden set, which are mock data. Treat these figures as a check that the agents behave as designed, not as production accuracy.</Text>
      </Stack>
    </Disclosure>
  );
}
