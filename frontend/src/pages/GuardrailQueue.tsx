import { Box, Flex, Heading, Stack, Table, Text } from "@chakra-ui/react";
import { useState } from "react";
import { api } from "../api/client";
import { usePoll, useAction } from "../api/state";
import type { Approval, SettingsPayload } from "../api/types";
import { Btn, Card, Disclosure, Empty, IncidentLink, Page, PageHeader, Rows, Seg, agentLabel, fmtTime, sentence } from "../components/ui";
import { approvalTitle, whyHeld } from "../lib/humanize";

const PAGE = 20;

type Group = "critical" | "p1p2" | "kb" | "access" | "other";
const GROUPS: { key: Group; label: string; hint: string }[] = [
  { key: "critical", label: "Critical", hint: "Business-critical systems, or actions policy only lets you reject." },
  { key: "p1p2", label: "P1 and P2 incidents", hint: "Agent fixes for high-priority incidents." },
  { key: "kb", label: "Knowledge base updates", hint: "Drafted runbook and article changes waiting to be published." },
  { key: "access", label: "Access and requests", hint: "Service request actions such as access grants and password resets." },
  { key: "other", label: "Other", hint: "Everything else that was held back." },
];

function groupOf(a: Approval): Group {
  const ctx = (a.context ?? {}) as Record<string, unknown>;
  if (a.verdict === "Block" || Number(ctx.app_tier) === 0 || ctx.severity === "Critical") return "critical";
  if (a.action === "update_kb_article") return "kb";
  if (["P1", "P2"].includes(String(ctx.priority))) return "p1p2";
  if (["grant_access", "reset_password"].includes(a.action)) return "access";
  return "other";
}

export default function GuardrailQueue() {
  const act = useAction();
  const [tab, setTab] = useState<"open" | "done">("open");
  const [group, setGroup] = useState<"all" | Group>("all");
  const [shown, setShown] = useState<Record<string, number>>({});
  const open = usePoll(() => api.approvals("open"), 1500);
  const all = usePoll(() => api.approvals(), 3000);
  const settings = usePoll(api.settings, 10000);
  const done = (all.data ?? []).filter((a) => !["pending", "blocked"].includes(a.status));
  const waiting = open.data ?? [];
  const counts = Object.fromEntries(GROUPS.map((g) => [g.key, waiting.filter((a) => groupOf(a) === g.key).length])) as Record<Group, number>;
  const visible = GROUPS.filter((g) => counts[g.key] && (group === "all" || group === g.key));

  return (
    <Page>
      <PageHeader title="Guardrail queue">Actions the agents wanted to take but the safety rules held back. Approve to let the agent go ahead, or reject to stop it.</PageHeader>

      <Seg label="Queue" value={tab} onChange={setTab} items={[{ value: "open", label: `Waiting ${waiting.length}` }, { value: "done", label: `Decided ${done.length}` }]} />

      {tab === "open" && (!waiting.length ? (
        <Card><Empty title="Nothing is waiting for approval">Held actions appear here. Run the LicenseCore outage to see a P1 fix get stopped.</Empty></Card>
      ) : (
        <Stack gap="5">
          <Seg label="Group" value={group} onChange={setGroup}
            items={[{ value: "all", label: `All ${waiting.length}` }, ...GROUPS.filter((g) => counts[g.key]).map((g) => ({ value: g.key, label: `${g.label} ${counts[g.key]}` }))]} />
          {visible.map((g) => {
            const items = waiting.filter((a) => groupOf(a) === g.key);
            const n = shown[g.key] ?? PAGE;
            return (
              <Box key={g.key}>
                <Heading as="h2" size="lg" fontFamily="body" fontWeight="600">{g.label} <Text as="span" color="fg.muted" fontWeight="400">{items.length}</Text></Heading>
                <Text color="fg.muted" mb="2">{g.hint}</Text>
                <Card flush>
                  {items.slice(0, n).map((a) => <Item key={a.approval_id} a={a} act={act} />)}
                  {items.length > n && (
                    <Box px="5" py="3" borderTopWidth="1px" borderColor="border.muted">
                      <Btn tone="ghost" onClick={() => setShown({ ...shown, [g.key]: n + PAGE })}>Show {Math.min(PAGE, items.length - n)} more ({items.length - n} left)</Btn>
                    </Box>
                  )}
                </Card>
              </Box>
            );
          })}
        </Stack>
      ))}

      {tab === "done" && (!done.length ? <Card><Empty title="No decisions yet" /></Card> : (
        <Card flush>
          <Table.ScrollArea>
            <Table.Root size="md">
              <Table.Header><Table.Row><Table.ColumnHeader>Action</Table.ColumnHeader><Table.ColumnHeader>Outcome</Table.ColumnHeader><Table.ColumnHeader>By</Table.ColumnHeader><Table.ColumnHeader>Time</Table.ColumnHeader></Table.Row></Table.Header>
              <Table.Body>
                {done.map((a) => (
                  <Table.Row key={a.approval_id}>
                    <Table.Cell><Text fontWeight="600">{approvalTitle(a)}</Text><Text fontSize="sm" color="fg.subtle">{a.entity_ref}</Text></Table.Cell>
                    <Table.Cell>{sentence(a.status)}<Text fontSize="sm" color="fg.subtle">{a.result?.message}</Text></Table.Cell>
                    <Table.Cell>{a.decided_by}</Table.Cell>
                    <Table.Cell>{fmtTime(a.decided_ts)}</Table.Cell>
                  </Table.Row>
                ))}
              </Table.Body>
            </Table.Root>
          </Table.ScrollArea>
        </Card>
      ))}

      <Policy settings={settings.data} />
    </Page>
  );
}

function Item({ a, act }: { a: Approval; act: ReturnType<typeof useAction> }) {
  const blocked = a.verdict === "Block";
  const ctx = (a.context ?? {}) as Record<string, unknown>;
  const facts: [string, string][] = [];
  if (ctx.priority) facts.push(["Priority", String(ctx.priority)]);
  if (ctx.app_id) facts.push(["System", `${ctx.app_id}${ctx.app_tier != null ? ` (Tier ${ctx.app_tier})` : ""}`]);
  if (ctx.kb_accuracy != null) facts.push(["Runbook accuracy", `${ctx.kb_article} at ${Number(ctx.kb_accuracy).toFixed(0)}%`]);
  if (ctx.blast_radius_users != null) facts.push(["People affected", Number(ctx.blast_radius_users).toLocaleString()]);
  if (ctx.reversible != null) facts.push(["Can be undone", ctx.reversible ? "Yes" : "No"]);
  facts.push(["Agent confidence", `${Math.round((a.proposal?.confidence ?? 0) * 100)}%`]);

  return (
    <Flex gap="4" px="5" py="3.5" borderBottomWidth="1px" borderColor="border.muted" direction={{ base: "column", md: "row" }} justify="space-between">
      <Box minW="0">
        <Heading as="h3" size="md" fontFamily="body" fontWeight="600">{approvalTitle(a)}</Heading>
        <Text color="fg.muted">
          {agentLabel(a.agent)}, {a.entity_ref.startsWith("INC") ? <IncidentLink id={a.entity_ref} /> : a.entity_ref}, {fmtTime(a.created_ts)}
        </Text>
        <Text mt="1.5">{blocked ? "Blocked by policy: this can only be rejected." : whyHeld(a)}</Text>
        <Disclosure card={false} title="Details">
          <Stack gap="3" pl="3">
            <Stack as="ul" gap="1" pl="4" color="fg.muted">{a.reasons.map((r, i) => <li key={i}>{r}</li>)}</Stack>
            <Rows items={facts} />
          </Stack>
        </Disclosure>
      </Box>
      <Flex gap="2" flex="none" align="start">
        <Btn tone="ghost" small onClick={() => act(() => api.decide(a.approval_id, false), "Rejected")}>Reject</Btn>
        <Btn small disabled={blocked} title={blocked ? "Policy only allows rejecting this" : undefined}
          onClick={() => act(() => api.decide(a.approval_id, true), (r) => r.result.message ?? "Approved")}>Approve</Btn>
      </Flex>
    </Flex>
  );
}

function Policy({ settings }: { settings: SettingsPayload | null }) {
  const pol = settings?.policy as { approve_only_if?: Record<string, unknown>; freeze_windows?: { name: string; days_of_month: number[] }[] } | undefined;
  const r = pol?.approve_only_if;
  if (!r) return null;
  return (
    <Disclosure title="How the rules work">
      <Text color="fg.muted" mb="3" maxW="70ch">An agent can act on its own only when every rule below passes. Otherwise the action waits here. The rules live in a config file, not in code.</Text>
      <Rows items={[
        ["Confidence", `at least ${String(r.min_confidence)}`],
        ["Priority", `${(r.allowed_priorities as string[]).join(" or ")} only`],
        ["System tier", `Tier ${Number(r.max_app_tier_blocked) + 1} or lower`],
        ["Can be undone", r.require_reversible ? "required" : "not required"],
        ["Runbook accuracy", `at least ${String(r.min_kb_accuracy)}%`],
        ["People affected", `under ${String(r.max_blast_radius_users)}`],
        ["Reopened fixes", `under ${String(r.max_reopen_rate_pct)}%`],
        ["Privileged access", "always a person"],
        ["Change freezes", (pol?.freeze_windows ?? []).map((w) => `${w.name.replace(/_/g, " ")} (days ${w.days_of_month.join(", ")})`).join("; ")],
      ]} />
    </Disclosure>
  );
}
