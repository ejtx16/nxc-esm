import { Flex, Table, Text } from "@chakra-ui/react";
import { Fragment, useState } from "react";
import { api } from "../api/client";
import { usePoll } from "../api/state";
import type { Kpi } from "../api/types";
import { Card, Empty, Good, Page, PageHeader, Rows, Stat } from "../components/ui";

function show(k: Kpi, v: number, display?: string | null) {
  if (display) return display;
  if (k.unit === "min") return v < 1 ? `${(v * 60).toFixed(1)} s` : `${v.toFixed(1)} min`;
  if (k.unit === "hrs") return `${v.toFixed(1)} hrs`;
  return `${v.toFixed(1).replace(/\.0$/, "")}%`;
}

const target = (t: string) => t.replace(/^\s*</, "under ").replace(/^\s*>/, "over ");
const label = (k: string) => k.charAt(0).toUpperCase() + k.slice(1).replace(/_/g, " ");

export default function Kpis() {
  const kp = usePoll(api.kpiRun, 2500);
  const [open, setOpen] = useState<string | null>(null);
  const run = kp.data?.run;
  const kpis = kp.data?.kpis ?? [];
  const met = kpis.filter((k) => k.target_met).length;

  return (
    <Page>
      <PageHeader title="KPIs">Measured on the latest scenario run and compared with the case-study baseline. Updates while the run plays.</PageHeader>

      {!kpis.length ? (
        <Card><Empty title={run ? "Waiting for data" : "No run yet"}>{run ? "The numbers appear as the agents handle the first alerts." : "Start a scenario on the Live feed to measure the KPIs."}</Empty></Card>
      ) : (
        <>
          <Flex justify="space-between" align="end" wrap="wrap" gap="4">
            <Stat value={`${met} of ${kpis.length}`} label="targets met in this run" />
            <Text color="fg.muted">{run?.title} · {run?.done_events} of {run?.total_events} events · {run?.status}</Text>
          </Flex>

          <Card flush>
            <Table.ScrollArea>
              <Table.Root size="lg" interactive>
                <Table.Header>
                  <Table.Row><Table.ColumnHeader>Measure</Table.ColumnHeader><Table.ColumnHeader textAlign="end">Before</Table.ColumnHeader><Table.ColumnHeader textAlign="end">This run</Table.ColumnHeader><Table.ColumnHeader>Target</Table.ColumnHeader></Table.Row>
                </Table.Header>
                <Table.Body>
                  {kpis.map((k) => (
                    <Fragment key={k.key}>
                      <Table.Row cursor="pointer" onClick={() => setOpen(open === k.key ? null : k.key)} aria-expanded={open === k.key}>
                        <Table.Cell><Text fontWeight="600">{k.label}</Text><Text fontSize="sm" color="fg.subtle">{k.method}</Text></Table.Cell>
                        <Table.Cell textAlign="end" color="fg.muted">{show(k, k.baseline)}</Table.Cell>
                        <Table.Cell textAlign="end"><Text fontSize="lg" fontWeight="bold">{show(k, k.after, k.after_display)}</Text></Table.Cell>
                        <Table.Cell>{k.target_met ? <Good>Met</Good> : <Text as="span" fontWeight="600" color="fg.muted">Not met</Text>}<Text fontSize="sm" color="fg.subtle">{target(k.target_text)}</Text></Table.Cell>
                      </Table.Row>
                      {open === k.key && (
                        <Table.Row><Table.Cell colSpan={4} bg="bg">
                          <Rows items={Object.entries(k.detail).map(([key, v]) => [label(key), String(v ?? "–")])} />
                        </Table.Cell></Table.Row>
                      )}
                    </Fragment>
                  ))}
                </Table.Body>
              </Table.Root>
            </Table.ScrollArea>
            <Text fontSize="sm" color="fg.subtle" px="5" py="3" borderTopWidth="1px" borderColor="border.muted">"Before" is the case-study baseline. Select a row to see the counts behind it.</Text>
          </Card>
        </>
      )}
    </Page>
  );
}
