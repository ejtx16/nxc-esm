import {
  Badge, Box, Button, Collapsible, Field, Flex, Grid, Heading, HStack, NativeSelect, Progress, SegmentGroup, Separator, Stack, Switch, Text,
  type ButtonProps,
} from "@chakra-ui/react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

// ---- vocabulary -------------------------------------------------------------------------------

export const AGENTS: Record<string, { label: string; role: string }> = {
  alert_correlation: { label: "Alert Correlation Agent", role: "Groups duplicate alerts, suppresses noise and opens incidents" },
  incident_triage: { label: "Incident Triage Agent", role: "Classifies, prioritises and routes incidents, and explains why" },
  sr_fulfilment: { label: "Service Request Fulfillment Agent", role: "Fulfils password resets and access requests end to end" },
  kb_curation: { label: "Knowledge Curation Agent", role: "Scores article accuracy and drafts updates for approval" },
  guardrail: { label: "Guardrail and Risk Agent", role: "Checks every action against policy and holds risky ones for approval" },
  orchestrator: { label: "Operations Orchestrator Agent", role: "Passes signals between the agents, for example surge mode on a P1" },
  executor: { label: "Executor", role: "Carries out approved actions against the mock systems" },
  human: { label: "You", role: "Operator" },
};
export const agentLabel = (a: string) => AGENTS[a]?.label ?? a;

/** The nine agents in files/esm-ai-agents.md and the audit `agent` id that records each one's work (null = none yet). */
export const NINE_AGENTS: { key: string; label: string; id: string | null; note?: string }[] = [
  { key: "orchestrator", label: "Operations Orchestrator Agent", id: "orchestrator" },
  { key: "alert_correlation", label: "Alert Correlation Agent", id: "alert_correlation" },
  { key: "incident_triage", label: "Incident Triage Agent", id: "incident_triage" },
  { key: "rca", label: "Root Cause Analysis Agent", id: "incident_triage", note: "in triage" },
  { key: "sr_fulfilment", label: "Service Request Fulfillment Agent", id: "sr_fulfilment" },
  { key: "kb_curation", label: "Knowledge Curation Agent", id: "kb_curation" },
  { key: "resolution", label: "Resolution Automation Agent", id: "executor", note: "executor" },
  { key: "guardrail", label: "Guardrail and Risk Agent", id: "guardrail" },
  { key: "insights", label: "Insights and Continuous Improvement Agent", id: null, note: "KPIs page, no traces" },
];

// ---- page structure ---------------------------------------------------------------------------

export function Page({ children, maxW }: { children: ReactNode; maxW?: string }) {
  return <Stack gap="6" maxW={maxW}>{children}</Stack>;
}

export function PageHeader({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <Stack gap="1">
      <Heading as="h1" size="2xl" fontWeight="bold">{title}</Heading>
      {children && <Text color="fg.muted" maxW="70ch">{children}</Text>}
    </Stack>
  );
}

/** A bordered surface. `flush` removes inner padding so lists and tables can run edge to edge. */
export function Card({ title, actions, flush, children }: { title?: string; actions?: ReactNode; flush?: boolean; children: ReactNode }) {
  return (
    <Box bg="bg.panel" borderWidth="1px" borderColor="border" rounded="l1" overflow="hidden">
      {(title || actions) && (
        <Flex justify="space-between" align="center" gap="3" px="5" py="3" borderBottomWidth="1px" borderColor="border.muted">
          {title && <Heading as="h2" size="lg" fontWeight="600">{title}</Heading>}
          {actions}
        </Flex>
      )}
      <Box p={flush ? 0 : 5}>{children}</Box>
    </Box>
  );
}

/** A block inside a card, separated from the one above by a hairline. */
export function Section({ title, first, children }: { title?: string; first?: boolean; children: ReactNode }) {
  return (
    <Box px="6" py="4" borderTopWidth={first ? 0 : "1px"} borderColor="border.muted">
      {title && <Heading as="h3" size="sm" mb="2" fontFamily="body">{title}</Heading>}
      {children}
    </Box>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <Stack gap="1" py="8" px="4" textAlign="center" color="fg.muted">
      <Text fontWeight="600" color="fg">{title}</Text>
      {children && <Text>{children}</Text>}
    </Stack>
  );
}

export function Notice({ children }: { children: ReactNode }) {
  return <Box bg="bg.panel" borderWidth="1px" borderColor="border" borderLeftWidth="3px" borderLeftColor="fg" rounded="l1" px="4" py="3">{children}</Box>;
}

// ---- numbers ----------------------------------------------------------------------------------

export function Stat({ value, label }: { value: ReactNode; label: ReactNode }) {
  return (
    <Box>
      <Heading as="p" fontSize="3xl" fontWeight="bold" lineHeight="1">{value}</Heading>
      <Text color="fg.muted" mt="1.5">{label}</Text>
    </Box>
  );
}

/** Figures in a row, divided by hairlines. */
export function StatRow({ children }: { children: ReactNode[] }) {
  return (
    <Grid templateColumns={{ base: "1fr 1fr", md: `repeat(${children.length}, auto)` }} rowGap="5" justifyContent="start">
      {children.map((c, i) => (
        <Box key={i} px={{ md: "8" }} pl={{ md: i === 0 ? 0 : "8" }} borderLeftWidth={{ base: 0, md: i === 0 ? 0 : "1px" }} borderColor="border.muted">{c}</Box>
      ))}
    </Grid>
  );
}

export function Facts({ items }: { items: [string, ReactNode][] }) {
  return (
    <Grid templateColumns={{ base: "1fr", sm: "1fr 1fr" }} gap="4" columnGap="6">
      {items.map(([k, v]) => (
        <Box key={k}><Text fontSize="sm" color="fg.muted">{k}</Text><Text fontWeight="600">{v}</Text></Box>
      ))}
    </Grid>
  );
}

/** Label and value rows. */
export function Rows({ items }: { items: [ReactNode, ReactNode][] }) {
  return (
    <Grid templateColumns="max-content 1fr" columnGap="5" rowGap="1.5">
      {items.map(([k, v], i) => (
        <Box key={i} display="contents"><Text color="fg.muted">{k}</Text><Box fontWeight="500">{v}</Box></Box>
      ))}
    </Grid>
  );
}

export function Bullets({ items }: { items: ReactNode[] }) {
  return (
    <Stack as="ul" gap="1" pl="5" color="fg.muted" listStyleType="disc">
      {items.map((x, i) => <li key={i}>{x}</li>)}
    </Stack>
  );
}

// ---- controls ---------------------------------------------------------------------------------

/** One button vocabulary: solid ink, outline, destructive outline. */
export function Btn({ tone, small, ...p }: Omit<ButtonProps, "size"> & { tone?: "ghost" | "danger"; small?: boolean }) {
  const look: ButtonProps = tone === "ghost" ? { variant: "outline" } : tone === "danger" ? { variant: "outline", colorPalette: "red" } : { variant: "solid" };
  return <Button size={small ? "xs" : "sm"} fontWeight="600" {...look} {...p} />;
}

export function SelectField({ label, value, onChange, children, disabled, w }: { label?: string; value: string | number; onChange: (v: string) => void; children: ReactNode; disabled?: boolean; w?: string }) {
  return (
    <Field.Root w={w} disabled={disabled}>
      {label && <Field.Label fontWeight="600">{label}</Field.Label>}
      <NativeSelect.Root bg="white" rounded="l1">
        <NativeSelect.Field value={value} onChange={(e) => onChange(e.target.value)} aria-label={label}>{children}</NativeSelect.Field>
        <NativeSelect.Indicator />
      </NativeSelect.Root>
    </Field.Root>
  );
}

export function Seg<T extends string>({ value, onChange, items, label }: { value: T; onChange: (v: T) => void; items: { value: T; label: string }[]; label: string }) {
  return (
    <SegmentGroup.Root value={value} onValueChange={(e) => e.value && onChange(e.value as T)} aria-label={label} size="sm" bg="bg.muted" w="fit-content">
      <SegmentGroup.Indicator bg="fg" />
      {items.map((it) => (
        <SegmentGroup.Item key={it.value} value={it.value} _checked={{ color: "white" }}>
          <SegmentGroup.ItemText>{it.label}</SegmentGroup.ItemText>
          <SegmentGroup.ItemHiddenInput />
        </SegmentGroup.Item>
      ))}
    </SegmentGroup.Root>
  );
}

export function Toggle({ on, onChange, label, amber }: { on: boolean; onChange: (v: boolean) => void; label: string; amber?: boolean }) {
  return (
    <Switch.Root checked={on} onCheckedChange={(e) => onChange(e.checked)} colorPalette={amber ? "yellow" : "red"} size="lg">
      <Switch.HiddenInput aria-label={label} />
      <Switch.Control />
    </Switch.Root>
  );
}

export function Meter({ value, tone, w }: { value: number | null; tone?: "ok" | "warn" | "bad"; w?: string }) {
  const palette = tone === "bad" ? "red" : tone === "warn" ? "orange" : "green";
  return (
    <Progress.Root value={Math.max(0, Math.min(100, value ?? 0))} size="xs" colorPalette={palette} w={w}>
      <Progress.Track><Progress.Range /></Progress.Track>
    </Progress.Root>
  );
}

/** Secondary detail that stays out of the way until asked for. */
export function Disclosure({ title, children, card = true, defaultOpen }: { title: string; children: ReactNode; card?: boolean; defaultOpen?: boolean }) {
  return (
    <Collapsible.Root defaultOpen={defaultOpen}
      {...(card ? { bg: "bg.panel", borderWidth: "1px", borderColor: "border", rounded: "l1" } : {})}>
      <Collapsible.Trigger asChild>
        <Button variant="plain" w="full" justifyContent="flex-start" px={card ? "5" : "0"} py="4" h="auto" fontWeight="600" color="fg">
          <Collapsible.Context>{(c) => <Text as="span" aria-hidden w="3">{c.open ? "▾" : "▸"}</Text>}</Collapsible.Context>
          {title}
        </Button>
      </Collapsible.Trigger>
      <Collapsible.Content>
        <Box px={card ? "5" : "0"} pb="5" color="fg">{children}</Box>
      </Collapsible.Content>
    </Collapsible.Root>
  );
}

// ---- status words -----------------------------------------------------------------------------

export function Prio({ p }: { p: string | null | undefined }) {
  if (!p) return null;
  const palette = p === "P1" ? "red" : p === "P2" ? "orange" : p === "P3" ? "teal" : "gray";
  return <Badge colorPalette={palette} variant="solid" rounded="l1" fontWeight="700">{p}</Badge>;
}

export const Late = ({ children }: { children: ReactNode }) => <Text as="span" color="red.600" fontWeight="600">{children}</Text>;
export const Good = ({ children }: { children: ReactNode }) => <Text as="span" color="green.700" fontWeight="600">{children}</Text>;

export const IncidentLink = ({ id }: { id: string }) => (
  <Link to={`/incidents?id=${id}`} style={{ fontWeight: 600, textDecoration: "underline" }}>{id}</Link>
);

export { Separator, HStack };

// ---- formatting -------------------------------------------------------------------------------

export function fmtClock(iso?: string | null) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-GB", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
export const fmtTime = (iso?: string | null) => (iso ? iso.slice(11, 16) : "");
export function fmtHours(h?: number | null) {
  if (h == null) return "—";
  if (h < 1 / 60) return `${Math.round(h * 3600)} s`;
  if (h < 1) return `${Math.round(h * 60)} min`;
  return `${h.toFixed(1)} h`;
}
export const pct = (v?: number | null, d = 0) => (v == null ? "—" : `${v.toFixed(d)}%`);
export const sentence = (s: string) => s.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

export const agentRole = (a: string) => AGENTS[a]?.role ?? "";
