import { useState } from "react";
import { SmartBrainTab } from "./features/smartbrain/SmartBrainTab";

type Tab = "dashboard" | "smartbrain";

export default function App() {
  const [tab, setTab] = useState<Tab>("dashboard");

  return (
    <main className="min-h-screen bg-slate-950 text-white">
      <header className="flex gap-2 border-b border-slate-800 p-4">
        <button className="rounded bg-slate-800 px-3 py-1" onClick={() => setTab("dashboard")}>Dashboard</button>
        <button className="rounded bg-slate-800 px-3 py-1" onClick={() => setTab("smartbrain")}>SmartBrain</button>
      </header>
      {tab === "dashboard" ? <div className="p-6 text-slate-300">Existing management UI stays here.</div> : <SmartBrainTab />}
    </main>
  );
}
