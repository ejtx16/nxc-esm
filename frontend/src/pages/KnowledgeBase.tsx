import { Box, Flex, Grid, Heading, HStack, Stack, Text } from "@chakra-ui/react";
import { useState } from "react";
import { api } from "../api/client";
import { usePoll, useAction } from "../api/state";
import { Btn, Bullets, Card, Disclosure, Empty, Late, Meter, Page, PageHeader, Rows, Section, Seg, Stat, StatRow } from "../components/ui";

type Filter = "all" | "below" | "update" | "archive";
const TH = 90;
const ACTION: Record<string, string> = { keep: "Keep", update: "Needs update", archive: "Consider archiving" };

export default function KnowledgeBase() {
  const act = useAction();
  const list = usePoll(api.kbArticles, 2500);
  const [sel, setSel] = useState<string | null>("KB4022");
  const [filter, setFilter] = useState<Filter>("all");
  const detail = usePoll(() => (sel ? api.kbArticle(sel) : Promise.resolve(null)), 2500, [sel]);
  const arts = list.data ?? [];
  const shown = arts.filter((a) => filter === "all" || (filter === "below" && a.below_threshold) || a.recommendation === filter);
  const mean = arts.length ? arts.reduce((x, a) => x + (a.score ?? 0), 0) / arts.length : null;
  const d = detail.data;
  const draft = d?.drafts.find((x) => x.status === "pending") ?? d?.drafts[0];
  const low = d != null && (d.score ?? 100) < TH;
  const waiting = arts.filter((a) => a.pending_draft_id).length;

  return (
    <Page>
      <PageHeader title="Knowledge base">Each article gets an accuracy score. Articles under {TH}% are not suggested to the Incident Triage Agent until someone approves a fix.</PageHeader>

      <Flex justify="space-between" align="center" wrap="wrap" gap="4">
        <StatRow>
          {[
            <Stat key="a" value={`${mean != null ? Math.round(mean) : "–"}%`} label="average accuracy" />,
            <Stat key="b" value={`${arts.filter((a) => a.below_threshold).length} of ${arts.length}`} label="need fixing" />,
            <Stat key="c" value={waiting} label={waiting === 1 ? "update to approve" : "updates to approve"} />,
          ]}
        </StatRow>
        <Btn tone="ghost" onClick={() => act(api.kbReview, "Re-scoring all articles")}>Re-score all articles</Btn>
      </Flex>

      <Box><Seg label="Filter" value={filter} onChange={setFilter} items={[
        { value: "all", label: "All" }, { value: "below", label: `Under ${TH}%` }, { value: "update", label: "Needs update" }, { value: "archive", label: "Archive" },
      ]} /></Box>

      <Grid templateColumns={{ base: "1fr", lg: "380px minmax(0,1fr)" }} gap="5" alignItems="start">
        <Card flush>
          <Stack gap="0" maxH={{ base: "360px", lg: "calc(100vh - 300px)" }} overflowY="auto">
            {shown.map((a) => {
              const on = sel === a.article_id;
              return (
                <Stack key={a.article_id} asChild gap="1" px="5" py="3.5" textAlign="start" w="full" borderBottomWidth="1px" borderColor="border.muted"
                  bg={on ? "bg" : "transparent"} boxShadow={on ? "inset 3px 0 0 {colors.fg}" : undefined} _hover={{ bg: "bg" }} cursor="pointer">
                  <button onClick={() => setSel(a.article_id)} aria-current={on || undefined}>
                    <Text fontWeight="600">{a.title}</Text>
                    <Text color="fg.muted">{a.article_id}, {a.category}</Text>
                    <HStack gap="3" color="fg.muted"><Text fontWeight="700" color="fg">{a.score?.toFixed(0)}%</Text><Text>{a.recommendation ? ACTION[a.recommendation] : ""}</Text>{a.pending_draft_id ? <Text>Update ready</Text> : null}</HStack>
                  </button>
                </Stack>
              );
            })}
            {!shown.length && <Empty title="No articles match this filter" />}
          </Stack>
        </Card>

        {!d ? <Card><Empty title="Select an article" /></Card> : (
          <Card flush>
            <Box px="6" pt="5" pb="4">
              <Text color="fg.muted">{d.article_id}, {d.category}</Text>
              <Heading as="h2" size="xl" fontWeight="bold" mt="1">{d.title}</Heading>
            </Box>

            <Section>
              <Flex justify="space-between"><Text fontWeight="600">Accuracy</Text>{low ? <Late>{d.score?.toFixed(0)}% (needs {TH}%)</Late> : <Text color="fg.muted">{d.score?.toFixed(0)}% (needs {TH}%)</Text>}</Flex>
              <Box my="2"><Meter value={d.score} tone={low ? "bad" : "ok"} /></Box>
              <Text color="fg.muted">Used {d.usage_count_90days} times in 90 days, and {d.escalation_despite_kb} of those still ended in an escalation.</Text>
            </Section>

            {d.issues.length > 0 && <Section title="What needs fixing"><Bullets items={d.issues} /></Section>}

            {draft && (
              <Section>
                <Flex justify="space-between"><Heading as="h3" size="sm">Suggested update</Heading><Text color="fg.muted">{draft.status === "pending" ? "Waiting for you" : draft.status}</Text></Flex>
                <Text color="fg.muted" mt="1">{draft.change_summary}</Text>
                <Box mt="3" bg="white" borderWidth="1px" borderColor="border" rounded="l1" maxH="320px" overflow="auto" fontSize="sm" aria-label="Changes">
                  {draft.diff.map((l, i) => (
                    <Text key={i} px="3" py="0.5" whiteSpace="pre-wrap" bg={l.op === "+" ? "green.subtle" : l.op === "-" ? "red.subtle" : undefined} textDecoration={l.op === "-" ? "line-through" : undefined}>
                      {l.op === "+" ? "+ " : l.op === "-" ? "− " : "  "}{l.text}
                    </Text>
                  ))}
                </Box>
                {draft.status === "pending" && draft.approval && (
                  <Flex gap="2" mt="3">
                    <Btn onClick={() => act(() => api.decide(draft.approval!.approval_id, true, "Approved by knowledge manager"), (r) => r.result.message ?? "Published")}>Approve and publish</Btn>
                    <Btn tone="ghost" onClick={() => act(() => api.decide(draft.approval!.approval_id, false), "Draft rejected")}>Reject</Btn>
                  </Flex>
                )}
                <Text fontSize="sm" color="fg.subtle" mt="2">A person always approves changes to articles. Agents can't publish on their own.</Text>
              </Section>
            )}

            {!draft && d.recommendation === "update" && (
              <Section><Btn onClick={() => act(() => api.kbDraft(d.article_id), "Drafting an update from recent resolutions")}>Draft an update</Btn></Section>
            )}

            <Box px="6" borderTopWidth="1px" borderColor="border.muted">
              <Disclosure card={false} title="How the score is worked out">
                {d.score_parts?.freshness != null && <Rows items={[  /* unscored articles come back as [] */
                  [`Not updated for ${d.score_parts.days_since_update} days`, `−${d.score_parts.freshness.toFixed(1)}`],
                  ["Still escalates", `−${d.score_parts.escalation.toFixed(1)}`],
                  ["Mentions retired systems", `−${d.score_parts.deprecated.toFixed(1)}`],
                ]} />}
                {d.seed_accuracy_pct != null && <Text fontSize="sm" color="fg.subtle" mt="2">The spreadsheet listed {d.seed_accuracy_pct}%. That value is random dummy data, so it's ignored.</Text>}
              </Disclosure>
            </Box>
            <Box px="6" borderTopWidth="1px" borderColor="border.muted">
              <Disclosure card={false} title="Current article text"><Text whiteSpace="pre-wrap">{d.body}</Text></Disclosure>
            </Box>
          </Card>
        )}
      </Grid>
    </Page>
  );
}
