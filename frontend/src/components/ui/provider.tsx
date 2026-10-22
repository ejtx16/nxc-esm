import { ChakraProvider } from "@chakra-ui/react";
import type { ReactNode } from "react";
import { system } from "../../theme";
import { Toaster } from "./toaster";

/** Light mode only: the console has one deliberate look, so no color-mode provider. */
export function Provider({ children }: { children: ReactNode }) {
  return (
    <ChakraProvider value={system}>
      {children}
      <Toaster />
    </ChakraProvider>
  );
}
