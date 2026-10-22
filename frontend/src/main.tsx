import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@fontsource-variable/inter";
import App from "./App";
import { Provider } from "./components/ui/provider";

createRoot(document.getElementById("root")!).render(<StrictMode><Provider><App /></Provider></StrictMode>);
