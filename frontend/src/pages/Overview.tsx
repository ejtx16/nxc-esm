import { Box, Flex, Heading, HStack, SimpleGrid, Stack, Text } from "@chakra-ui/react";
import type { ReactNode } from "react";
import { Link as RouterLink } from "react-router-dom";
import { api } from "../api/client";
import { usePoll, useShell } from "../api/state";
import type { Decision } from "../api/types";
import AgentFlow from "../components/AgentFlow";
import { Card, Empty, IncidentLink, Page, PageHeader, Prio, agentLabel, fmtTime, pct } from "../components/ui";
import { approvalTitle, feedText, whyHeld } from "../lib/humanize";

/** Hide routine noise (duplicates folded into a cluster, pass-through approvals, execution and queue echoes). */
const notable = (d: Decision) =>
  d.kind !== "execution" && d.kind !== "queued" &&
  !(d.kind === "guardrail" && d.guardrail_verdict === "Approve") &&
  !(d.agent === "alert_correlation" && /^(duplicate of|.* alert correlated with) CL-/.test(d.reasoning ?? ""));

type Stat = [label: string, value: ReactNode];

/** One operational domain: the number that matters, then label/value rows that line up across the four cards. */
function Lane({ to, title, value, unit, aside, stats, extra }: { to: string; title: string; value: ReactNode; unit: string; aside?: ReactNode; stats: Stat[]; extra?: ReactNode }) {
  return (
    <Stack asChild gap="4" p={{ base: 4, md: 5 }} bg="bg.panel" borderWidth="1px" borderColor="border" rounded="l1" h="100%" transition="border-color 0.15s, box-shadow 0.15s"
      _hover={{ borderColor: "border.emphasized", boxShadow: "sm" }}>
      <RouterLink to={to} aria-label={`${title}: open details`}>
        <Text fontWeight="600" color="fg.muted" fontSize="sm" textTransform="uppercase" letterSpacing="0.04em">{title}</Text>
        <Flex justify="space-between" align="flex-end" gap="3">
          <Box>
            <Heading as="div" fontSize={{ base: "3xl", md: "4xl" }} fontWeight="bold" lineHeight="1.05">{value}</Heading>
            <Text color="fg.muted" fontSize="sm" mt="1">{unit}</Text>
          </Box>
          {aside}
        </Flex>
        <Stack gap="2" pt="3" mt="auto" borderTopWidth="1px" borderColor="border.muted" fontSize="sm">
          {stats.map(([k, v]) => (
            <Flex key={k} justify="space-between" align="baseline" gap="3"><Text color="fg.muted">{k}</Text><Text fontWeight="600" textAlign="end">{v}</Text></Flex>
          ))}
          {extra}
        </Stack>
      </RouterLink>
    </Stack>
  );
}

export default function Overview() {
  const { overview: o } = useShell();
  const feed = usePoll(() => api.feed(150), 2000);
  const approvals = usePoll(() => api.approvals("open"), 2000);

  if (!o) return <Text color="fg.muted">Loading the current state of operations…</Text>;
  const p1 = o.incidents.open_by_priority["P1"] ?? 0;
  const headline = o.surge_mode
    ? "A critical incident (P1) is open. The agents are clearing the request queue so people can focus on it."
    : p1 > 0 ? `${p1} critical incident${p1 === 1 ? " is" : "s are"} open.`
    : o.run ? "Everything is running normally. The agents are handling the routine work."
    : "Nothing is running yet. Start a scenario on the Live feed to see the agents work.";
  const waiting = approvals.data ?? [];

  return (
    <Page>
      <PageHeader title="Overview">{headline}</PageHeader>

      <SimpleGrid templateColumns="repeat(auto-fit, minmax(min(100%, 240px), 1fr))" gap="4" aria-label="Operational domains">
        <Lane to="/live" title="Alerts" value={o.alerts.clusters} unit={`alert group${o.alerts.clusters === 1 ? "" : "s"} from ${o.alerts.raw} alerts`}
          stats={[["Hidden as noise", o.alerts.suppressed_clusters], ["Noise left", `${pct(o.alerts.surfaced_noise_pct)} (was ${pct(o.alerts.raw_noise_pct)})`]]} />
        <Lane to="/incidents" title="Incidents" value={o.incidents.open_total} unit="open"
          aside={
            <Box textAlign="end">
              <Heading as="div" fontSize={{ base: "2xl", md: "3xl" }} fontWeight="bold" lineHeight="1.05">{o.incidents.resolved}</Heading>
              <Text color="fg.muted" fontSize="sm" mt="1">resolved</Text>
            </Box>
          }
          stats={[["Near their deadline", o.incidents.at_risk], ["High risk, with a person", o.incidents.with_person],
                  ["Auto resolved", o.incidents.auto_resolved], ["Manual resolved", o.incidents.resolved - o.incidents.auto_resolved]]}
          extra={
            <HStack gap="3" wrap="wrap" pt="1">
              {(["P1", "P2", "P3", "P4"] as const).map((p) => o.incidents.open_by_priority[p] ? <HStack key={p} gap="1"><Prio p={p} /><Text>{o.incidents.open_by_priority[p]}</Text></HStack> : null)}
              {o.incidents.open_total === 0 && <Text color="fg.subtle">None open</Text>}
              {o.triage_hold && <Text color="fg.muted">Reassignments paused</Text>}
            </HStack>
          } />
        <Lane to="/self-service" title="Service Requests" value={o.sr.auto_fulfilled} unit="handled automatically"
          aside={
            <Box textAlign="end">
              <Heading as="div" fontSize={{ base: "2xl", md: "3xl" }} fontWeight="bold" lineHeight="1.05">{o.sr.completed}</Heading>
              <Text color="fg.muted" fontSize="sm" mt="1">resolved</Text>
            </Box>
          }
          stats={[["Need approval", o.sr.pending_approval],
                  ["Auto resolved", o.sr.auto_fulfilled], ["Manual resolved", o.sr.completed - o.sr.auto_fulfilled]]}
          extra={o.surge_mode ? <Text color="fg.muted">Surge mode is on, so ticket requests are handled automatically.</Text> : undefined} />
        <Lane to="/knowledge" title="KB Quality" value={pct(o.kb.mean_score)} unit="average article accuracy"
          stats={[["Average article completeness", pct(o.kb.mean_completeness)], ["Need fixing", `${o.kb.below_threshold} of ${o.kb.articles}`],["Updates to approve", o.kb.pending_drafts]]} />
      </SimpleGrid>

      <SimpleGrid columns={{ base: 1, lg: 3 }} gap="5" alignItems="start">
        <Stack gap="5" gridColumn={{ lg: "span 2" }}>
          <Card title="Agent team" flush><AgentFlow feed={feed.data} /></Card>
          <Card title="Recent activity" flush actions={<RouterLink to="/live"><Text textDecoration="underline">See all</Text></RouterLink>}>
            {!feed.data?.length ? <Empty title="Nothing has happened yet">Start a scenario and the agents' work will show up here.</Empty> : (
              <Stack gap="0" maxH="min(640px, calc(100vh - 380px))" minH="320px" overflowY="auto">
                {feed.data.filter(notable).slice(0, 30).map((d) => (
                  <Box key={d.decision_id} px="5" py="3" borderBottomWidth="1px" borderColor="border.muted" title={`${d.entity_ref}: ${d.reasoning || d.input_summary || ""}`}>
                    <Flex gap="2" align="center">
                      <Text fontWeight="600">{agentLabel(d.kind === "human" ? "human" : d.kind === "guardrail" ? "guardrail" : d.agent)}</Text>
                      {d.kind === "guardrail" && d.guardrail_verdict && d.guardrail_verdict !== "Approve" && <Text color="fg.muted">held for a person</Text>}
                      {d.kind === "human" && <Text color="fg.muted">{d.human_decision}</Text>}
                      <Text ml="auto" fontSize="sm" color="fg.subtle">{fmtTime(d.timestamp)}</Text>
                      {d.trace_id && <RouterLink to={`/audit?trace=${d.trace_id}`} aria-label={`Audit trace for ${d.entity_ref}`}><Text fontSize="sm" textDecoration="underline" color="fg.muted">trace</Text></RouterLink>}
                    </Flex>
                    <Text mt="0.5">{feedText(d)}</Text>
                  </Box>
                ))}
              </Stack>
            )}
          </Card>
        </Stack>

        <Box position={{ lg: "sticky" }} top="4">
          <Card title="Needs your decision" flush actions={<RouterLink to="/approvals"><Text textDecoration="underline">Review all</Text></RouterLink>}>
            {!waiting.length ? <Text p="5" color="fg.muted">All clear. Anything the agents can't decide alone will appear here.</Text> : (
              <Stack gap="0">
                {waiting.slice(0, 6).map((a) => (
                  <Box key={a.approval_id} px="5" py="3" borderBottomWidth="1px" borderColor="border.muted">
                    <Text fontWeight="600">{approvalTitle(a)}</Text>
                    <Text color="fg.muted" mt="0.5">{whyHeld(a)}</Text>
                    {a.entity_ref.startsWith("INC") && <IncidentLink id={a.entity_ref} />}
                  </Box>
                ))}
                {waiting.length > 6 && <Text px="5" py="3" color="fg.muted">{waiting.length - 6} more in the queue</Text>}
              </Stack>
            )}
          </Card>
        </Box>
      </SimpleGrid>
    </Page>
  );
}
