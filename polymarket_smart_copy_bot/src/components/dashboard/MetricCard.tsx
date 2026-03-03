import { motion } from "framer-motion";

interface Props {
  label: string;
  value: string;
  tone?: "good" | "bad" | "warn" | "neutral";
  delay?: number;
}

export function MetricCard({ label, value, tone = "neutral", delay = 0 }: Props) {
  const toneClass =
    tone === "good" ? "status-good" : tone === "bad" ? "status-bad" : tone === "warn" ? "status-warn" : "text-foreground";

  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: delay * 0.05 }}
      className="trading-card flex flex-col gap-1"
    >
      <span className="text-[11px] font-medium uppercase tracking-widest text-muted-foreground">{label}</span>
      <span className={`font-mono text-xl font-bold ${toneClass}`}>{value}</span>
    </motion.div>
  );
}
