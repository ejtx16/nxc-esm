import { Box, Code, Dialog, Flex, HStack, List, Portal, Stack, Text } from "@chakra-ui/react";
import { useState, type ReactNode } from "react";
import { api } from "../api/client";
import { usePoll, useAction, useShell } from "../api/state";
import { Btn, Card, Disclosure, Good, Late, Page, PageHeader, Toggle, agentLabel, agentRole } from "../components/ui";
import { Table } from "@chakra-ui/react";

function ResetDialog() {
  const act = useAction();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const reset = async () => {
    setBusy(true);
    await act(api.reset, "Demo data reset. Everything is back to the start.");
    setBusy(false);
    setOpen(false);
  };
  return (
    <Dialog.Root role="alertdialog" open={open} onOpenChange={(e) => !busy && setOpen(e.open)} placement="center">
      <Dialog.Trigger asChild><Btn tone="danger">Reset</Btn></Dialog.Trigger>
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content mx="4">
            <Dialog.Header><Dialog.Title>Reset all demo data?</Dialog.Title></Dialog.Header>
            <Dialog.Body>
              <Text mb="3">This puts the demo back to where it started. It clears:</Text>
              <List.Root gap="1" ps="5" color="fg.muted">
                <List.Item>Every alert, incident and request from scenario runs</List.Item>
                <List.Item>Approvals, notifications and the whole audit trail</List.Item>
                <List.Item>Claude usage, KB drafts and any setting you changed</List.Item>
              </List.Root>
              <Text mt="3">Any running scenario and agent work stop first. The spreadsheet data is reloaded and the simulated clock starts again. This can't be undone.</Text>
            </Dialog.Body>
            <Dialog.Footer>
              <Dialog.ActionTrigger asChild><Btn tone="ghost" disabled={busy}>Cancel</Btn></Dialog.ActionTrigger>
              <Btn colorPalette="red" loading={busy} loadingText="Resetting…" onClick={reset}>Reset everything</Btn>
            </Dialog.Footer>
          </Dialog.Content>
        </Dialog.Positioner>
      </Portal>
    </Dialog.Root>
  );
}

const AGENT_KEYS = ["alert_correlation", "incident_triage", "sr_fulfilment", "kb_curation"];

function Setting({ title, desc, children, extra }: { title: string; desc: string; children: ReactNode; extra?: ReactNode }) {
  return (
    <Flex gap="6" px="6" py="5" align="center" justify="space-between" direction={{ base: "column", md: "row" }} alignItems={{ base: "stretch", md: "center" }}
      borderBottomWidth="1px" borderColor="border.muted" _last={{ borderBottomWidth: 0 }}>
      <Box>
        <Text fontWeight="600">{title}</Text>
        <Text color="fg.muted" mt="1" maxW="62ch">{desc}</Text>
        {extra}
      </Box>
      <Flex flex="none" justify={{ base: "flex-start", md: "flex-end" }}>{children}</Flex>
    </Flex>
  );
}

export default function Controls() {
  const act = useAction();
  const { overview: o } = useShell();
  const settings = usePoll(api.settings, 3000);
  const usage = usePoll(api.llmUsage, 4000);
  const graph = usePoll(api.graph, 60000);
  const [health, setHealth] = useState<{ ok: boolean; text?: string; error?: string } | null>(null);
  const [checking, setChecking] = useState(false);
  const s = settings.data;
  if (!s || !o) return <Text color="fg.muted">Loading controls…</Text>;
  const ks = s.kill_switch;
  const tier0 = s.apps.filter((a) => a.tier <= 1);

  return (
    <Page maxW="860px">
      <PageHeader title="Controls">Switches for the person running the demo. The kill switch overrides everything, so nothing runs unattended until it's turned off.</PageHeader>

      <Card flush>
        <Setting
          title={ks.global ? "Kill switch is on" : "Kill switch"}
          desc={ks.global ? "Every action the agents propose now waits in the Guardrail queue for a person." : "Agents act within the safety rules. Turn this on to require approval for every action."}>
          <Toggle on={ks.global} label="Global kill switch" onChange={(v) => act(() => api.killSwitch({ global: v }), v ? "Kill switch on: all actions need approval" : "Kill switch off")} />
        </Setting>

        <Setting
          title="Claude"
          desc="Claude makes every agent decision: it classifies each alert, triages each incident, reads each request and scores and drafts articles. Each step is in the Audit trail with what the agent collected, what Claude decided, and where the Guardrail sent it. The safety checks stay rule-based."
          extra={<>
            {health && <HStack mt="3">{health.ok ? <Good>Connected ({health.text})</Good> : <Late>Not connected</Late>}</HStack>}
            {health && !health.ok && <Text color="fg.muted" mt="1.5">{health.error}. Add ANTHROPIC_API_KEY to backend/.env and restart the API.</Text>}
          </>}>
          <Btn tone="ghost" small disabled={checking} onClick={async () => { setChecking(true); const r = await act(api.llmHealth); setHealth(r ?? null); setChecking(false); }}>{checking ? "Testing…" : "Test connection"}</Btn>
        </Setting>

        <Setting title="Simulated clock" desc="Jump ahead to watch unresolved incidents escalate before they miss their deadline.">
          <HStack gap="2">
            {[15, 30, 60, 240].map((m) => <Btn key={m} tone="ghost" small onClick={() => act(() => api.advance(m), (r) => `Clock moved ${m} min. ${r.sla_checks_queued} deadline check(s) queued.`)}>+{m >= 60 ? `${m / 60} h` : `${m} min`}</Btn>)}
          </HStack>
        </Setting>

        <Setting title="Reset demo data" desc="Stops any running scenario and agent work, rebuilds the database from the spreadsheets, and resets the simulated clock. Everything else is cleared.">
          <ResetDialog />
        </Setting>
      </Card>

      <Disclosure title="Turn off one agent or application">
        <Text color="fg.muted" mb="3">Actions from these need approval while their switch is on.</Text>
        <Stack gap="3">
          {AGENT_KEYS.map((k) => (
            <Flex key={k} justify="space-between" align="center"><Stack gap="0"><Text>{agentLabel(k)}</Text><Text fontSize="sm" color="fg.subtle">{agentRole(k)}</Text></Stack>
              <Toggle on={!!ks.agents[k]} label={`Disable ${agentLabel(k)}`} onChange={(v) => act(() => api.killSwitch({ agent: k, on: v }), v ? `${agentLabel(k)} now needs approval` : `${agentLabel(k)} restored`)} /></Flex>
          ))}
          {tier0.map((a) => (
            <Flex key={a.app_id} justify="space-between" align="center"><Stack gap="0"><Text>{a.app_id}</Text><Text fontSize="sm" color="fg.subtle">Application, Tier {a.tier}</Text></Stack>
              <Toggle on={!!ks.apps[a.app_id]} label={`Disable automation for ${a.app_id}`} onChange={(v) => act(() => api.killSwitch({ app: a.app_id, on: v }), v ? `${a.app_id} now needs approval` : `${a.app_id} restored`)} /></Flex>
          ))}
        </Stack>
      </Disclosure>

      {usage.data && usage.data.by_agent.length > 0 && (
        <Disclosure title="Language model usage">
          <Table.Root size="sm" variant="line">
            <Table.Header><Table.Row><Table.ColumnHeader>Agent</Table.ColumnHeader><Table.ColumnHeader textAlign="end">Calls</Table.ColumnHeader><Table.ColumnHeader textAlign="end">Tokens in / out</Table.ColumnHeader><Table.ColumnHeader textAlign="end">Avg ms</Table.ColumnHeader><Table.ColumnHeader textAlign="end">Errors</Table.ColumnHeader></Table.Row></Table.Header>
            <Table.Body>{usage.data.by_agent.map((u) => (
              <Table.Row key={u.agent}><Table.Cell>{agentLabel(u.agent)}</Table.Cell><Table.Cell textAlign="end">{u.calls}</Table.Cell><Table.Cell textAlign="end">{u.input_tokens ?? 0} / {u.output_tokens ?? 0}</Table.Cell><Table.Cell textAlign="end">{u.avg_ms}</Table.Cell><Table.Cell textAlign="end">{u.errors}</Table.Cell></Table.Row>
            ))}</Table.Body>
          </Table.Root>
        </Disclosure>
      )}

      <Disclosure title="Advanced: agent graph and policy file">
        <Text fontWeight="600" mb="1">Agent graph (Mermaid)</Text>
        <Code display="block" whiteSpace="pre-wrap" p="3" mb="4" maxH="260px" overflow="auto">{graph.data?.mermaid ?? "Loading…"}</Code>
        <Text fontWeight="600" mb="1">Active policy (backend/config/guardrail_policy.yaml)</Text>
        <Code display="block" whiteSpace="pre-wrap" p="3" maxH="260px" overflow="auto">{JSON.stringify(s.policy, null, 2)}</Code>
      </Disclosure>
    </Page>
  );
}
