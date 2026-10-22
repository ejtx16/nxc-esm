import { BrowserRouter, Route, Routes } from "react-router-dom";
import { Box, Text } from "@chakra-ui/react";
import { ShellProvider } from "./api/state";
import Layout from "./components/Layout";
import Overview from "./pages/Overview";
import LiveFeed from "./pages/LiveFeed";
import Incidents from "./pages/Incidents";
import GuardrailQueue from "./pages/GuardrailQueue";
import SelfService from "./pages/SelfService";
import KnowledgeBase from "./pages/KnowledgeBase";
import Kpis from "./pages/Kpis";
import Controls from "./pages/Controls";
import AuditTrail from "./pages/AuditTrail";

export default function App() {
  return (
    <BrowserRouter>
      <ShellProvider>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<Overview />} />
            <Route path="live" element={<LiveFeed />} />
            <Route path="incidents" element={<Incidents />} />
            <Route path="approvals" element={<GuardrailQueue />} />
            <Route path="self-service" element={<SelfService />} />
            <Route path="knowledge" element={<KnowledgeBase />} />
            <Route path="kpis" element={<Kpis />} />
            <Route path="audit" element={<AuditTrail />} />
            <Route path="controls" element={<Controls />} />
            <Route path="*" element={<Box py="10" textAlign="center"><Text fontWeight="600">Page not found</Text><Text color="fg.muted">Use the navigation on the left.</Text></Box>} />
          </Route>
        </Routes>
      </ShellProvider>
    </BrowserRouter>
  );
}
