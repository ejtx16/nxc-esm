import { Box, Field, Flex, Grid, Stack, Table, Text, Textarea } from "@chakra-ui/react";
import { useEffect, useState } from "react";
import { api } from "../api/client";
import { usePoll, useAction, useShell } from "../api/state";
import type { Approval, SR } from "../api/types";
import { Btn, Card, Disclosure, Empty, Notice, Page, PageHeader, Section, Seg, SelectField, fmtHours, fmtTime, sentence } from "../components/ui";

const TYPES = ["Password Reset", "Access Provisioning", "Software Installation", "Report Request"];
const SYSTEMS = ["LicenseCore", "BillingEngine", "CustomerPortal", "AuthService", "APIGateway", "IntegrationHub", "WorkflowEngine", "DataWarehouse", "ReportingPlatform", "NotificationService"];

export default function SelfService() {
  const act = useAction();
  const { overview: o } = useShell();
  const emps = usePoll(api.employees, 60000);
  const recent = usePoll(() => api.serviceRequests(25), 2000);
  const summary = usePoll(api.srSummary, 4000);
  const [emp, setEmp] = useState("EMP1002");
  const [mode, setMode] = useState<"form" | "text">("form");
  const [rtype, setRtype] = useState("Password Reset");
  const [system, setSystem] = useState("LicenseCore");
  const [level, setLevel] = useState("standard");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ sr: SR; approvals: Approval[]; surge_mode: boolean } | null>(null);
  useEffect(() => { if (!emps.data?.length) return; if (!emps.data.find((e) => e.employee_id === emp)) setEmp(emps.data[0].employee_id); }, [emps.data, emp]);

  async function submit(body?: Record<string, unknown>) {
    setBusy(true);
    const payload = body ?? (mode === "form"
      ? { employee_id: emp, request_type: rtype, ...(rtype === "Access Provisioning" ? { target_system: system, access_level: level } : {}) }
      : { employee_id: emp, text });
    const r = await act(() => api.selfService(payload));
    setBusy(false);
    if (r) setResult(r);
  }

  const sum = summary.data;

  return (
    <Page>
      <PageHeader title="Self-service">Employees ask for a password reset or standard access. The agent checks who they are and what they're allowed, then does it. Anything privileged or unusual goes to a person.</PageHeader>

      {o?.surge_mode && <Notice><b>Surge mode is on.</b> Requests that normally wait for the service desk are being handled automatically so people can stay on the incident.</Notice>}

      <Grid templateColumns={{ base: "1fr", lg: "minmax(0,1fr) 380px" }} gap="5" alignItems="start">
        <Stack gap="5">
          <Card title="New request">
            <Stack gap="4">
              <SelectField label="Employee" value={emp} onChange={setEmp}>
                {emps.data?.map((e) => <option key={e.employee_id} value={e.employee_id}>{e.name} ({e.role}{e.status !== "active" ? `, ${e.status}` : ""})</option>)}
              </SelectField>
              <Box><Seg label="Request style" value={mode} onChange={setMode} items={[{ value: "form", label: "Pick a request" }, { value: "text", label: "Describe it" }]} /></Box>
              {mode === "form" ? (
                <Grid templateColumns="repeat(auto-fit, minmax(200px, 1fr))" gap="4">
                  <SelectField label="What do you need?" value={rtype} onChange={setRtype}>{TYPES.map((t) => <option key={t}>{t}</option>)}</SelectField>
                  {rtype === "Access Provisioning" && <>
                    <SelectField label="System" value={system} onChange={setSystem}>{SYSTEMS.map((t) => <option key={t}>{t}</option>)}</SelectField>
                    <SelectField label="Access level" value={level} onChange={setLevel}><option value="standard">Standard</option><option value="read">Read only</option><option value="admin">Admin</option></SelectField>
                  </>}
                </Grid>
              ) : (
                <Field.Root>
                  <Field.Label fontWeight="600">Describe your request</Field.Label>
                  <Textarea bg="white" rows={4} value={text} onChange={(e) => setText(e.target.value)} placeholder="I'm locked out of my account, please reset my password" />
                </Field.Root>
              )}
              <Box><Btn disabled={busy || (mode === "text" && !text.trim())} onClick={() => submit()}>{busy ? "Working…" : "Submit request"}</Btn></Box>
            </Stack>
            <Flex gap="2" align="center" wrap="wrap" mt="4" pt="4" borderTopWidth="1px" borderColor="border.muted">
              <Text color="fg.subtle">Try an example:</Text>
              <Btn tone="ghost" small onClick={() => submit({ employee_id: "EMP1002", request_type: "Password Reset" })}>Password reset</Btn>
              <Btn tone="ghost" small onClick={() => submit({ employee_id: "EMP1003", request_type: "Access Provisioning", target_system: "BillingEngine", access_level: "admin" })}>Admin access</Btn>
              <Btn tone="ghost" small onClick={() => submit({ employee_id: "EMP9999", request_type: "Password Reset" })}>Former employee</Btn>
            </Flex>
          </Card>

          {result && (
            <Card flush>
              <Box px="6" pt="5" pb="4">
                <Text color="fg.muted">{result.sr.sr_id}, {sentence(result.sr.status)}{result.sr.fulfillment_time_hrs != null ? `, done in ${fmtHours(result.sr.fulfillment_time_hrs)}` : ""}</Text>
                <Text fontSize="xl" fontWeight="bold">{result.sr.request_type ?? "Request"}{result.sr.target_system ? ` on ${result.sr.target_system} (${result.sr.access_level})` : ""}</Text>
              </Box>
              <Section title="What happened">
                <Stack gap="3">
                  {result.sr.trace.map((t, i) => <Flex key={i} gap="3"><Text w="12" flex="none" fontSize="sm" color="fg.subtle" pt="0.5">{fmtTime(t.ts)}</Text><Text>{t.text}</Text></Flex>)}
                  {result.approvals.map((a) => <Text key={a.approval_id}><b>Held for a person.</b> <Text as="span" color="fg.muted">{a.reasons.join(" ")}</Text></Text>)}
                </Stack>
                {result.sr.outcome && <Text mt="3"><b>Result:</b> {result.sr.outcome}</Text>}
                {result.approvals.length > 0 && <Text color="fg.muted" mt="1.5">An approver can release it from the Guardrail queue.</Text>}
              </Section>
            </Card>
          )}
        </Stack>

        <Stack gap="5">
          <Card title="Recent requests" flush>
            {!recent.data?.length ? <Empty title="No requests yet">Submit one on the left, or run a scenario.</Empty> : (
              <Stack gap="0">
                {recent.data.slice(0, 12).map((r) => (
                  <Box key={r.sr_id} px="5" py="3" borderBottomWidth="1px" borderColor="border.muted">
                    <Text fontWeight="600">{r.request_type ?? "Request"}{r.target_system ? ` on ${r.target_system}` : ""}</Text>
                    <Text color="fg.muted">{r.sr_id}, {r.requestor_id}, {r.channel}</Text>
                    <Text color="fg.muted">{sentence(r.status)}</Text>
                  </Box>
                ))}
              </Stack>
            )}
          </Card>
          {sum && (
            <Disclosure title="What could be automated">
              <Text color="fg.muted" mb="2">{sum.auto} of {sum.completed} completed requests were handled by the agent in this session.</Text>
              <Table.Root size="sm" variant="line">
                <Table.Header><Table.Row><Table.ColumnHeader>Request type</Table.ColumnHeader><Table.ColumnHeader textAlign="end">Per month</Table.ColumnHeader><Table.ColumnHeader>Potential</Table.ColumnHeader></Table.Row></Table.Header>
                <Table.Body>{sum.opportunity.slice(0, 6).map((r) => (
                  <Table.Row key={r.request_type}><Table.Cell>{r.request_type}</Table.Cell><Table.Cell textAlign="end">{r.avg_volume_per_month}</Table.Cell><Table.Cell>{r.automation_potential}</Table.Cell></Table.Row>
                ))}</Table.Body>
              </Table.Root>
            </Disclosure>
          )}
        </Stack>
      </Grid>
    </Page>
  );
}
