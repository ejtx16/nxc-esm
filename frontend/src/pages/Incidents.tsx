import { Box, Flex, Grid, Heading, HStack, Stack, Text } from "@chakra-ui/react";
import { useEffect, useState } from "react";
import { Link as RouterLink, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import { usePoll, useAction } from "../api/state";
import { rationaleBullets } from "../lib/humanize";
import type { Decision, Sla } from "../api/types";
import { Btn, Bullets, Card, Disclosure, Empty, Facts, Late, Meter, Page, PageHeader, Prio, Rows, Section, Seg, agentLabel, fmtHours, fmtTime, sentence } from "../components/ui";

/** The deadline in words. The raw risk ratio can be far above 100% once an incident is overdue. */
function deadline(sla: Sla): string {
  if (sla.remaining_h == null) return `Resolved in ${fmtHours(sla.elapsed_h)}`;
  return sla.remaining_h > 0 ? `${fmtHours(sla.remaining_h)} left` : "Overdue";
}
const overdue = (sla: Sla) => sla.remaining_h != null && sla.remaining_h <= 0;

export default function Incidents() {
  const act = useAction();
  const [params, setParams] = useSearchParams();
  const [filter, setFilter] = useState<"open" | "all">("open");
  const list = usePoll(() => api.incidents(filter === "open" ? "open" : undefined), 2000, [filter]);
  const sel = params.get("id");
  const detail = usePoll(() => (sel ? api.incident(sel) : Promise.resolve(null)), 2000, [sel]);
  const items = list.data ?? [];

  useEffect(() => { if (!sel && items.length) setParams({ id: items[0].incident_id }, { replace: true }); }, [sel, items, setParams]);
  const d = detail.data;
  const risk = d?.sla.risk ?? 0;
  const reasons = rationaleBullets(d?.rationale);
  const steps = d?.decisions.filter((x) => (x.kind !== "execution" || x.reasoning) && x.kind !== "queued") ?? [];

  return (
    <Page>
      <PageHeader title="Incidents">Incidents the Incident Triage Agent has handled. Pick one to see where it was sent and why.</PageHeader>

      <Flex justify="space-between" wrap="wrap" gap="3">
        <Seg label="Filter" value={filter} onChange={setFilter} items={[{ value: "open", label: "Open" }, { value: "all", label: "All" }]} />
        <Btn tone="ghost" small title="Move the simulated clock forward to watch deadline escalation"
          onClick={() => act(() => api.advance(30), (r) => `Clock moved 30 min. ${r.sla_checks_queued} deadline check${r.sla_checks_queued === 1 ? "" : "s"} queued.`)}>Fast-forward 30 min</Btn>
      </Flex>

      {!items.length ? (
        <Card><Empty title="No incidents yet">Start the LicenseCore outage on the Live feed. Critical alerts open incidents here automatically.</Empty></Card>
      ) : (
        <Grid templateColumns={{ base: "1fr", lg: "400px minmax(0,1fr)" }} gap="5" alignItems="start">
          <Card flush>
            <Stack gap="0" maxH={{ base: "360px", lg: "calc(100vh - 260px)" }} overflowY="auto">
              {items.map((i) => {
                const on = sel === i.incident_id;
                return (
                  <Stack key={i.incident_id} asChild gap="1" px="5" py="4" textAlign="start" w="full" borderBottomWidth="1px" borderColor="border.muted"
                    bg={on ? "bg" : "transparent"} boxShadow={on ? "inset 3px 0 0 {colors.fg}" : undefined} _hover={{ bg: "bg" }} cursor="pointer">
                    <button onClick={() => setParams({ id: i.incident_id })} aria-current={on || undefined}>
                      <Text fontWeight="600" lineClamp="2">{i.short_description}</Text>
                      <Flex justify="space-between" gap="3" fontSize="sm" color="fg.muted">
                        <Text truncate>{i.incident_id}, {i.app_id ?? "No system"}</Text>
                        {i.pending_approvals > 0 && <Text flex="none">Approval pending</Text>}
                      </Flex>
                      <Flex justify="space-between" align="center" gap="3" mt="1">
                        <HStack gap="2" minW="0"><Prio p={i.priority} /><Text fontSize="sm" truncate>{i.assigned_team ?? "Not routed yet"}</Text></HStack>
                        {overdue(i.sla) ? <Late>{deadline(i.sla)}</Late> : <Text fontSize="sm" color="fg.muted" flex="none">{deadline(i.sla)}</Text>}
                      </Flex>
                    </button>
                  </Stack>
                );
              })}
            </Stack>
          </Card>

          {!d ? <Card><Empty title="Select an incident" /></Card> : (
            <Card flush>
              <Box px="6" pt="5" pb="4">
                <HStack gap="3" color="fg.muted"><Prio p={d.priority} /><Text>{d.incident_id}</Text><Text>{sentence(d.status)}{d.escalated && !/escalated/i.test(d.status) ? ", escalated" : ""}</Text></HStack>
                <Heading as="h2" size="xl" mt="2" fontWeight="bold">{d.short_description}</Heading>
              </Box>

              <Section>
                <Facts items={[
                  ["Category", d.category ?? "Not set"],
                  ["Sent to", d.assigned_team ?? (d.held ? "On hold during the P1" : "Not routed yet")],
                  ["System", `${d.app_id ?? "Unknown"}${d.tier != null ? ` (Tier ${d.tier})` : ""}`],
                  ["Agent confidence", d.confidence != null ? `${Math.round(d.confidence * 100)}%` : "Not set"],
                ]} />
              </Section>

              <Section>
                <Flex justify="space-between"><Text fontWeight="600">Deadline</Text>{overdue(d.sla) ? <Late>{deadline(d.sla)}</Late> : <Text color="fg.muted">{deadline(d.sla)}</Text>}</Flex>
                <Box my="2"><Meter value={Math.min(100, risk * 100)} tone={risk >= 1 ? "bad" : risk >= 0.5 ? "warn" : "ok"} /></Box>
                <Text fontSize="sm" color="fg.muted">{d.sla.window_h} h deadline. The agent escalates at the halfway point.</Text>
              </Section>

              {reasons.length > 0 && <Section title="Why the agent decided this"><Bullets items={reasons} /></Section>}

              <Section title="Related">
                <Rows items={[
                  ["Runbooks", d.kb.length ? d.kb.map((k) => `${k.article_id} (${k.score.toFixed(0)}%)`).join(", ") : "None were accurate enough to use"],
                  ...(d.kb_excluded.length ? [["Skipped", d.kb_excluded.map((k) => `${k.article_id} (${k.score != null ? `${k.score.toFixed(0)}%` : "not scored"})`).join(", ")] as [string, string]] : []),
                  ["Past incidents", d.similar.length ? <Stack gap="0.5">{d.similar.map((s) => <Text key={s.incident_id}><b>{s.incident_id}</b> {s.short_description}</Text>)}</Stack> : "No similar incidents found"],
                  ...(d.cluster ? [["Source alerts", `${d.cluster.alert_count} alerts, ${d.cluster.signature.replace("|", ", ")}${d.cluster.ack_latency_s != null ? `. Acknowledged ${Math.round(d.cluster.ack_latency_s)} s after the first one.` : ""}`] as [string, string]] : []),
                ]} />
              </Section>

              <Box px="6" borderTopWidth="1px" borderColor="border.muted">
                <Disclosure card={false} title={`Decision trail (${steps.length} steps)`}>
                  <Stack gap="3">{steps.map((x) => <Step key={x.decision_id} d={x} />)}</Stack>
                  <Text mt="3" fontSize="sm"><RouterLink to={`/audit?entity=${d.incident_id}`} style={{ fontWeight: 600, textDecoration: "underline" }}>Open the full audit trail for {d.incident_id}</RouterLink></Text>
                </Disclosure>
              </Box>
            </Card>
          )}
        </Grid>
      )}
    </Page>
  );
}

function Step({ d }: { d: Decision }) {
  const who = d.kind === "human" ? "You" : d.kind === "guardrail" ? agentLabel("guardrail") : agentLabel(d.agent);
  const result = d.kind === "guardrail" ? d.guardrail_verdict : d.kind === "human" ? d.human_decision : d.kind === "execution" ? "Done" : null;
  return (
    <Box>
      <Flex gap="2"><Text fontWeight="600">{who}</Text>{result && <Text color="fg.muted">{result === "RequireApproval" ? "held for approval" : result.toLowerCase()}</Text>}<Text ml="auto" fontSize="sm" color="fg.subtle">{fmtTime(d.timestamp)}</Text></Flex>
      <Text color="fg.muted">{d.reasoning || d.input_summary}</Text>
    </Box>
  );
}
