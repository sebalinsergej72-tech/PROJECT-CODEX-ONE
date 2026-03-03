import { useMemo } from "react";
import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { usePortfolioHistory } from "@/hooks/useBotData";

export function YieldChart() {
    const { data: history, isLoading, isError } = usePortfolioHistory();

    const chartData = useMemo(() => {
        if (!history || history.length === 0) return [];

        // Sort chronologically (oldest to newest)
        const sorted = [...history].sort((a, b) =>
            new Date(a.taken_at).getTime() - new Date(b.taken_at).getTime()
        );

        return sorted.map((snap) => {
            const date = new Date(snap.taken_at);
            return {
                timestamp: date.getTime(),
                timeLabel: date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
                dateLabel: date.toLocaleDateString([], { month: 'short', day: 'numeric' }),
                equity: snap.total_equity_usd,
            };
        });
    }, [history]);

    if (isLoading) {
        return (
            <div className="flex h-[300px] w-full items-center justify-center rounded-xl border border-border bg-card/50">
                <span className="text-sm text-muted-foreground animate-pulse">Loading yield history...</span>
            </div>
        );
    }

    if (isError || chartData.length === 0) {
        return (
            <div className="flex h-[300px] w-full items-center justify-center rounded-xl border border-border bg-card/50">
                <span className="text-sm text-muted-foreground">No portfolio history available yet.</span>
            </div>
        );
    }

    const minEquity = Math.min(...chartData.map(d => d.equity));
    const maxEquity = Math.max(...chartData.map(d => d.equity));
    const buffer = (maxEquity - minEquity) * 0.1 || maxEquity * 0.05;

    return (
        <div className="rounded-xl border border-border bg-card/50 p-6 flex flex-col gap-4">
            <div className="flex justify-between items-center">
                <div>
                    <h2 className="text-lg font-semibold tracking-tight">Portfolio Yield</h2>
                    <p className="text-sm text-muted-foreground">Total equity over time</p>
                </div>
                <div className="text-right">
                    <div className="text-2xl font-bold tracking-tight text-primary">
                        ${chartData[chartData.length - 1].equity.toFixed(2)}
                    </div>
                    <div className="text-xs text-muted-foreground">Current Equity</div>
                </div>
            </div>

            <div className="h-[260px] w-full mt-2">
                <ResponsiveContainer width="100%" height="100%">
                    <AreaChart data={chartData} margin={{ top: 10, right: 0, left: -20, bottom: 0 }}>
                        <defs>
                            <linearGradient id="colorEquity" x1="0" y1="0" x2="0" y2="1">
                                <stop offset="5%" stopColor="hsl(var(--primary))" stopOpacity={0.3} />
                                <stop offset="95%" stopColor="hsl(var(--primary))" stopOpacity={0} />
                            </linearGradient>
                        </defs>
                        <XAxis
                            dataKey="timeLabel"
                            axisLine={false}
                            tickLine={false}
                            tick={{ fontSize: 11, fill: 'hsl(var(--muted-foreground))' }}
                            minTickGap={30}
                        />
                        <YAxis
                            domain={[minEquity - buffer, maxEquity + buffer]}
                            axisLine={false}
                            tickLine={false}
                            tick={{ fontSize: 11, fill: 'hsl(var(--muted-foreground))' }}
                            tickFormatter={(value) => `$${value.toFixed(0)}`}
                        />
                        <Tooltip
                            content={({ active, payload }) => {
                                if (active && payload && payload.length) {
                                    const data = payload[0].payload;
                                    return (
                                        <div className="rounded-lg border border-border bg-popover/95 px-3 py-2 shadow-xl backdrop-blur-md">
                                            <div className="text-xs text-muted-foreground mb-1">
                                                {data.dateLabel} {data.timeLabel}
                                            </div>
                                            <div className="font-mono text-sm font-semibold text-primary">
                                                ${data.equity.toFixed(2)}
                                            </div>
                                        </div>
                                    );
                                }
                                return null;
                            }}
                        />
                        <Area
                            type="monotone"
                            dataKey="equity"
                            stroke="hsl(var(--primary))"
                            strokeWidth={2}
                            fillOpacity={1}
                            fill="url(#colorEquity)"
                            animationDuration={1500}
                        />
                    </AreaChart>
                </ResponsiveContainer>
            </div>
        </div>
    );
}
