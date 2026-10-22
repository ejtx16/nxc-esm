import { Badge, Box, Flex, IconButton, Image, Popover, Portal, Stack, Text } from "@chakra-ui/react";
import { Link as RouterLink, Outlet, useLocation } from "react-router-dom";
import { useShell } from "../api/state";
import type { Overview } from "../api/types";
import { Meter, Separator, sentence } from "./ui";

const NAV = [
  { to: "/", label: "Overview", end: true },
  { to: "/live", label: "Live feed" },
  { to: "/incidents", label: "Incidents", incidents: true },
  { to: "/approvals", label: "Guardrail queue", badge: true },
  { to: "/self-service", label: "Self-service" },
  { to: "/knowledge", label: "Knowledge base" },
  { to: "/kpis", label: "KPIs" },
  { to: "/audit", label: "Audit trail" },
  { to: "/controls", label: "Controls" },
];

const MARK = "data:image/svg+xml;utf8," + encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="6" fill="#f2b705"/><path d="M6 24 L6 8 L9 8 L23 21 L23 8 L26 8 L26 24 L23 24 L9 11 L9 24 Z" fill="#13222B"/></svg>',
);

const BellIcon = () => (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9" /><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0" />
  </svg>
);

/** Urgent items: open P1s, SLA-at-risk incidents, and approvals waiting on a human. */
function urgentItems(o: Overview | null) {
  if (!o) return [];
  const items = [
    { key: "p1", to: "/incidents", count: o.incidents.open_by_priority.P1 ?? 0, text: "open P1 incident", tone: "red" },
    { key: "risk", to: "/incidents", count: o.incidents.at_risk, text: "incident at SLA risk", tone: "orange" },
    { key: "blocked", to: "/approvals", count: o.approvals.blocked, text: "blocked action needs review", tone: "red" },
    { key: "pending", to: "/approvals", count: o.approvals.pending, text: "action awaiting approval", tone: "yellow" },
  ];
  return items.filter((i) => i.count > 0);
}

function UrgentBell({ overview }: { overview: Overview | null }) {
  const items = urgentItems(overview);
  const total = items.reduce((n, i) => n + i.count, 0);
  return (
    <Popover.Root positioning={{ placement: "bottom-end" }} lazyMount unmountOnExit>
      <Popover.Trigger asChild>
        <IconButton variant="ghost" size="sm" position="relative" aria-label={total ? `${total} urgent items` : "No urgent items"} color={total ? "fg" : "fg.muted"}>
          <BellIcon />
          {total > 0 && (
            <Badge position="absolute" top="-1" right="-1" colorPalette="red" variant="solid" rounded="full" px="1.5" minW="4" fontSize="2xs">{total}</Badge>
          )}
        </IconButton>
      </Popover.Trigger>
      <Portal>
        <Popover.Positioner>
          <Popover.Content w="280px">
            <Popover.Body p="3">
              <Text fontWeight="600" fontSize="sm" mb="2">Urgent requests</Text>
              {items.length === 0 ? (
                <Text fontSize="sm" color="fg.muted">Nothing urgent right now.</Text>
              ) : (
                <Stack gap="1">
                  {items.map((i) => (
                    <Flex key={i.key} asChild align="center" gap="3" px="2" py="2" rounded="l1" _hover={{ bg: "bg.muted" }}>
                      <RouterLink to={i.to}>
                        <Badge colorPalette={i.tone} variant="subtle" minW="6" justifyContent="center">{i.count}</Badge>
                        <Text fontSize="sm">{i.text}{i.count > 1 ? "s" : ""}</Text>
                      </RouterLink>
                    </Flex>
                  ))}
                </Stack>
              )}
            </Popover.Body>
          </Popover.Content>
        </Popover.Positioner>
      </Portal>
    </Popover.Root>
  );
}

function NavItem({ to, end, label, count }: { to: string; end?: boolean; label: string; count?: number }) {
  const { pathname } = useLocation();
  const active = end ? pathname === to : pathname.startsWith(to);
  return (
    <Flex asChild justify="space-between" align="center" gap="2" px="3" py="2" rounded="l1" color={active ? "white" : "whiteAlpha.800"}
      bg={active ? "whiteAlpha.200" : "transparent"} borderLeftWidth="3px" borderLeftColor={active ? "barrier" : "transparent"}
      fontWeight={active ? "600" : "400"} _hover={{ bg: "whiteAlpha.100", color: "white" }}>
      <RouterLink to={to} aria-current={active ? "page" : undefined}>
        <span>{label}</span>
        {count !== undefined && <Badge bg={count ? "barrier" : "whiteAlpha.200"} color={count ? "ink" : "whiteAlpha.700"} rounded="full" fontWeight="700" minW="6" justifyContent="center">{count}</Badge>}
      </RouterLink>
    </Flex>
  );
}

export default function Layout() {
  const { overview: o, scenario: s, offline } = useShell();
  const waiting = (o?.approvals.pending ?? 0) + (o?.approvals.blocked ?? 0);
  const killed = o && (o.kill_switch.global || Object.values(o.kill_switch.agents).some(Boolean) || Object.values(o.kill_switch.apps).some(Boolean));
  const run = o?.run;
  const progress = run && run.total_events ? (run.done_events / run.total_events) * 100 : 0;
  const simNow = o?.clock.sim_now ? new Date(o.clock.sim_now) : null;
  const clock = simNow && {
    time: simNow.toLocaleTimeString("en-GB"),
    date: simNow.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" }),
  };
  const state = s?.active ? (s.paused ? "Paused" : `Running at ${Math.round(o?.clock.speed ?? 1)}×`) : sentence(run?.status ?? "");

  return (
    <Flex direction={{ base: "column", md: "row" }} minH="100%">
      <Stack as="nav" aria-label="Main" gap="1" w={{ md: "220px" }} flex="none" bg="ink" color="whiteAlpha.900" px="3" pb="3"
        position={{ md: "sticky" }} top="0" h={{ md: "100vh" }} overflowY={{ md: "auto" }} direction={{ base: "row", md: "column" }} flexWrap="wrap" align={{ base: "center", md: "stretch" }}>
        <Flex align="center" gap="3" px="2" h="64px" mb={{ md: "3" }} mr={{ base: 4, md: 0 }} borderBottomWidth={{ md: "1px" }} borderColor="whiteAlpha.200">
          <Image src={MARK} alt="" boxSize="30px" />
          <Box lineHeight="1.15">
            <Text fontWeight="600" fontSize="lg" color="white">NexaCore</Text>
            <Text fontSize="xs" color="whiteAlpha.600">Ops console</Text>
          </Box>
        </Flex>
        {NAV.map((n) => <NavItem key={n.to} to={n.to} end={n.end} label={n.label} count={n.badge ? (waiting || undefined) : n.incidents ? (o?.incidents.open_total ?? 0) : undefined} />)}
        <Box flex="1" display={{ base: "none", md: "block" }} />
        <Text display={{ base: "none", md: "block" }} fontSize="xs" color="whiteAlpha.600" px="2" pb="2">
          Prototype on mock data. Claude Opus 5.5 behind a deterministic Guardrail.
        </Text>
      </Stack>

      <Box flex="1" minW="0">
        {offline && (
          <Box bg="red.600" color="white" px="7" py="2" fontWeight="600" role="alert">
            The API is not reachable. Start the backend with <b>uvicorn app.api.main:app</b> in the backend folder.
          </Box>
        )}
        <Flex as="header" aria-label="System status" align="center" gap={{ base: 4, md: 6 }} wrap="wrap" minH="64px" px={{ base: 4, md: 8 }} py="2"
          bg="bg.panel" borderBottomWidth="1px" borderColor="border" position="sticky" top="0" zIndex="10">
          <Box title="Simulated time (case-study calendar)">
            <Text fontWeight="700" fontSize="lg" lineHeight="1.2">{clock?.time ?? "--:--:--"}</Text>
            <Text fontSize="xs" color="fg.muted">{clock ? `${clock.date}, simulated` : "Simulated time"}</Text>
          </Box>

          <Separator orientation="vertical" h="8" display={{ base: "none", sm: "block" }} />

          {s?.active || run ? (
            <Box minW="0">
              <Text fontWeight="600" fontSize="sm" lineHeight="1.3" truncate>{run?.title}</Text>
              <Flex align="center" gap="2">
                <Text fontSize="xs" color="fg.muted">{state}</Text>
                <Meter value={progress} w="80px" />
              </Flex>
            </Box>
          ) : (
            <Box>
              <Text fontWeight="600" fontSize="sm" lineHeight="1.3">No scenario running</Text>
              <Text fontSize="xs" color="fg.muted">Start one on the Live feed</Text>
            </Box>
          )}

          <Box flex="1" />

          <Flex align="center" gap="2" wrap="wrap">
            {o?.surge_mode && <Badge colorPalette="yellow" variant="subtle" size="md" title="A P1 is active: the request agent absorbs the queue and triage holds non-urgent reassignments">P1 surge mode</Badge>}
            {killed && <Badge colorPalette="red" variant="subtle" size="md" title="Autonomous actions are disabled; everything needs approval">Kill switch on</Badge>}
            {!!o && o.pending_events > 0 && <Text fontSize="xs" color="fg.muted">{o.pending_events} events queued</Text>}
            <Badge variant="subtle" size="md" title="LLM_MODE in backend/.env">{o?.llm_mode === "live" ? "Claude Opus 5.5" : "Rules only"}</Badge>
            <UrgentBell overview={o ?? null} />
          </Flex>
        </Flex>
        <Box as="main" px={{ base: 4, md: 8 }} py="6" maxW="1280px"><Outlet /></Box>
      </Box>
    </Flex>
  );
}
