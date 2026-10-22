import { Box, Flex, Grid, Stack, Table, Text } from "@chakra-ui/react";
import { useState } from "react";
import { api } from "../api/client";
import { usePoll, useAction, useShell } from "../api/state";
import type { Cluster, SR } from "../api/types";
import { Btn, Card, Empty, IncidentLink, Meter, Page, PageHeader, SelectField, Stat, StatRow, fmtTime, sentence } from "../components/ui";

const SPEEDS = [1, 10, 22, 60, 120, 400];

/** What happened to a group of alerts, in words a duty manager would use. */
function outcome(c: Cluster): { headline: string; detail: string } {
  const sure = `${Math.round(c.confidence * 100)}% sure`;
  const kind = c.classification === "true_positive" ? "a real problem" : c.classification === "false_positive" ? "noise" : "a duplicate";
  const detail = `${sure} this is ${kind}`;
  if (c.incident_id) {
    const ack = c.ack_latency_s != null ? `, acknowledged in ${c.ack_latency_s < 1 ? "under 1" : Math.round(c.ack_latency_s)} s` : "";
    return { headline: `Incident opened${ack}`, detail };
  }
  if (c.status === "suppressed") return { headline: "Hidden as noise", detail };
  if (c.status === "surfaced") return { headline: "Kept visible", detail };
  return { headline: c.status, detail };
}

const alertStatus = (s: string) => (s === "suppressed" ? "hidden as noise" : s === "surfaced" ? "kept visible" : s.replace(/_/g, " "));

/** What happened to a self-service request: the Guardrail's risk call and where the request ended up. */
function srOutcome(r: SR): { headline: string; detail: string } {
  const lines = r.trace.map((t) => t.text);
  const risk = lines.find((t) => t.startsWith("Guardrail:"));
  const kb = lines.find((t) => t.startsWith("Knowledge:"));
  const detail = [risk?.replace(/\s*\(.*\)$/, ""), kb?.replace(/^Knowledge: /, "from ")].filter(Boolean).join(", ");
  if (r.status === "Completed") return { headline: (r.fulfilled_by ?? "").startsWith("agent:") ? "Completed by the agent" : "Completed after approval", detail };
  if (r.status === "Pending Approval") return { headline: "Waiting for a human approver", detail };
  if (r.status === "Rejected") return { headline: "Rejected", detail: r.outcome ?? detail };
  return { headline: sentence(r.status), detail };
}

// The picker offers only these two; the other scenarios stay in the API for the KPI checks and tests.
const PICKABLE = ["json_alerts", "normal_day"];

export default function LiveFeed() {
  const act = useAction();
  const { scenario: s, overview: o } = useShell();
  const list = usePoll(api.scenarios, 5000);
  const live = usePoll(api.liveFeed, 1200);
  const [pick, setPick] = useState("json_alerts");
  const [speed, setSpeed] = useState<number | "default">("default");

  const info = list.data?.scenarios.find((x) => x.name === pick);
  const active = !!s?.active;
  const paused = !!s?.paused;
  const emitted = s?.emitted ?? 0;
  const total = s?.total ?? 0;
  const lf = live.data;
  const clusters = [...(lf?.clusters ?? [])].sort((a, b) => Number(!!b.incident_id) - Number(!!a.incident_id) || b.alert_count - a.alert_count);
  const incidents = clusters.filter((c) => c.incident_id).length;
  const suppressed = lf?.status_counts["suppressed"] ?? 0;
  const raw = lf?.raw_alerts ?? 0;
  const srs = lf?.service_requests ?? [];
  const biggest = clusters.reduce<Cluster | null>((m, c) => (!m || c.alert_count > m.alert_count ? c : m), null);
  const showProgress = active || (o?.run && total > 0);
  const duration = info ? (info.duration_s >= 86400 ? `${Math.round(info.duration_s / 86400)} days` : `${Math.max(1, Math.round(info.duration_s / 60))} minutes`) : "";

  function changeSpeed(v: string) {
    const n = v === "default" ? "default" : Number(v);
    setSpeed(n);
    if (active && n !== "default") act(() => api.setSpeed(n), `Speed set to ${n}×`);
  }

  return (
    <Page>
      <PageHeader title="Live feed">Replay a scenario and watch duplicate alerts collapse into groups, noise get hidden, and real problems open incidents.</PageHeader>

      <Card title="Scenario">
        <Grid templateColumns={{ base: "1fr", sm: "1fr 1fr", lg: "minmax(220px,1.4fr) minmax(160px,1fr) auto" }} gap="4" alignItems="end">
          <SelectField label="Scenario" value={pick} onChange={setPick} disabled={active}>
            {list.data?.scenarios.filter((x) => PICKABLE.includes(x.name)).map((x) => <option key={x.name} value={x.name}>{x.title}</option>)}
          </SelectField>
          <SelectField label="Replay speed" value={speed} onChange={changeSpeed}>
            <option value="default">Usual ({info ? Math.round(info.default_speed) : "—"}×)</option>
            {SPEEDS.map((v) => <option key={v} value={v}>{v}×</option>)}
          </SelectField>
          <Flex gap="2">
            {!active && <Btn onClick={() => act(() => api.startScenario(pick, speed === "default" ? undefined : speed), "Scenario started")}>Start scenario</Btn>}
            {active && !paused && <Btn tone="ghost" onClick={() => act(api.pauseScenario, "Paused")}>Pause</Btn>}
            {active && paused && <Btn onClick={() => act(api.resumeScenario, "Resumed")}>Resume</Btn>}
            {active && <Btn tone="ghost" onClick={() => act(api.stopScenario, "Stopped")}>Stop</Btn>}
          </Flex>
        </Grid>
        {info && <Text color="fg.muted" mt="4" maxW="80ch">{info.description} It covers {duration} and {info.events.toLocaleString()} events.</Text>}
        {showProgress && (
          <Stack gap="1.5" mt="4" pt="4" borderTopWidth="1px" borderColor="border.muted">
            <Meter value={total ? (emitted / total) * 100 : 0} />
            <Text color="fg.muted">{emitted.toLocaleString()} of {total.toLocaleString()} events replayed, {active ? (paused ? "paused" : "running") : "finished"}</Text>
          </Stack>
        )}
      </Card>

      {srs.length > 0 && (
        <Card title="Service requests" flush>
          <Table.ScrollArea>
            <Table.Root size="md" variant="line">
              <Table.Header>
                <Table.Row><Table.ColumnHeader>Request</Table.ColumnHeader><Table.ColumnHeader>Who</Table.ColumnHeader><Table.ColumnHeader>What happened</Table.ColumnHeader></Table.Row>
              </Table.Header>
              <Table.Body>
                {srs.map((r) => {
                  const out = srOutcome(r);
                  return (
                    <Table.Row key={r.sr_id} title={r.text ?? ""}>
                      <Table.Cell><Text fontWeight="600">{r.request_type ?? "Request"}{r.target_system ? ` on ${r.target_system} (${r.access_level})` : ""}</Text><Text color="fg.muted">{r.sr_id}, via the {r.channel}</Text></Table.Cell>
                      <Table.Cell><Text>{r.requestor_id}</Text><Text fontSize="sm" color="fg.subtle">{fmtTime(r.submitted_ts)}</Text></Table.Cell>
                      <Table.Cell><Text>{out.headline}</Text>{out.detail && <Text fontSize="sm" color="fg.subtle">{out.detail}</Text>}{r.outcome && <Text fontSize="sm" color="fg.subtle">Result: {r.outcome}</Text>}</Table.Cell>
                    </Table.Row>
                  );
                })}
              </Table.Body>
            </Table.Root>
          </Table.ScrollArea>
          <Text color="fg.muted" px="5" py="3">Held requests can be released on the Guardrail queue. Every step is in the Audit trail.</Text>
        </Card>
      )}

      {raw === 0 ? (
        srs.length === 0 && <Card><Empty title="Nothing has arrived yet">Start the alert log scenario to watch 10 alerts collapse into two incidents: one auto-resolved from the KB, one sent to a person.</Empty></Card>
      ) : (
        <Card title="Result">
          <StatRow>
            {[
              <Stat key="a" value={raw} label="alerts came in" />,
              <Stat key="b" value={lf?.cluster_count ?? 0} label="groups after merging duplicates" />,
              <Stat key="c" value={suppressed} label="alerts hidden as noise" />,
              <Stat key="d" value={incidents} label={incidents === 1 ? "incident opened" : "incidents opened"} />,
            ]}
          </StatRow>
          {biggest && biggest.alert_count > 5 && (
            <Text color="fg.muted" mt="4" pt="4" borderTopWidth="1px" borderColor="border.muted">
              The biggest group has {biggest.alert_count} alerts: {biggest.signature.replace("|", ", ")}
              {biggest.systems.length > 1 ? `, plus related alerts from ${biggest.systems.filter((x) => x !== biggest.primary_system).join(", ")}` : ""}.
            </Text>
          )}
        </Card>
      )}

      <Grid templateColumns={{ base: "1fr", xl: "minmax(0,1.6fr) minmax(300px,1fr)" }} gap="5" alignItems="start">
        <Card title="Alert groups" flush>
          {!clusters.length ? <Empty title="No groups yet">Groups appear as alerts arrive.</Empty> : (
            <Table.ScrollArea>
              <Table.Root size="md" variant="line">
                <Table.Header>
                  <Table.Row><Table.ColumnHeader>Alert</Table.ColumnHeader><Table.ColumnHeader textAlign="end">Alerts</Table.ColumnHeader><Table.ColumnHeader>What happened</Table.ColumnHeader></Table.Row>
                </Table.Header>
                <Table.Body>
                  {clusters.slice(0, 40).map((c) => {
                    const out = outcome(c);
                    return (
                      <Table.Row key={c.cluster_id} title={c.rationale ?? ""}>
                        <Table.Cell><Text fontWeight="600">{c.primary_system}</Text><Text color="fg.muted">{c.signature.split("|")[1]}</Text>{c.incident_id && <IncidentLink id={c.incident_id} />}</Table.Cell>
                        <Table.Cell textAlign="end"><Text fontWeight="600">{c.alert_count}</Text><Text fontSize="sm" color="fg.subtle">{c.max_severity}</Text></Table.Cell>
                        <Table.Cell><Text>{out.headline}</Text><Text fontSize="sm" color="fg.subtle">{out.detail}</Text></Table.Cell>
                      </Table.Row>
                    );
                  })}
                </Table.Body>
              </Table.Root>
            </Table.ScrollArea>
          )}
        </Card>

        <Card title="Incoming alerts" flush>
          {!lf?.alerts.length ? <Empty title="Waiting for alerts" /> : (
            <Stack gap="0" maxH="640px" overflowY="auto">
              {lf.alerts.map((a) => (
                <Box key={a.alert_id} px="5" py="3" borderBottomWidth="1px" borderColor="border.muted">
                  <Flex gap="2"><Text fontWeight="600">{a.source_system}</Text><Text ml="auto" fontSize="sm" color="fg.subtle">{fmtTime(a.alert_ts)}</Text></Flex>
                  <Text>{a.alert_name}</Text>
                  <Text fontSize="sm" color="fg.subtle">{a.severity} severity, {alertStatus(a.status)}</Text>
                </Box>
              ))}
            </Stack>
          )}
        </Card>
      </Grid>
    </Page>
  );
}
