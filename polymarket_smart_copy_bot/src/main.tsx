import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Toaster } from "sonner";

import App from "./App";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 2500,
      refetchOnWindowFocus: true,
    },
  },
});

// Auto-login interceptor for Telegram / External Links
const params = new URLSearchParams(window.location.search);
const token = params.get("token");
if (token) {
  localStorage.setItem("dashboard_write_token", token);
  // Clean URL without reloading the page
  window.history.replaceState({}, document.title, window.location.pathname);
}

// Telegram WebApp Initialization
if (window.Telegram?.WebApp) {
  window.Telegram.WebApp.ready();
  window.Telegram.WebApp.expand();
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
      <Toaster richColors theme="dark" position="top-right" />
    </QueryClientProvider>
  </React.StrictMode>,
);
