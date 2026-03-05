import { AlertCircle, Medal, TrendingUp, TrendingDown, Users, Activity } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useLeaderboard, useBotStatus } from "@/hooks/useBotData";

export function LeaderboardTable() {
    const { data: leaderboard, isLoading, isError, error } = useLeaderboard();
    const { data: botData } = useBotStatus();

    if (isError) {
        return (
            <Alert variant="destructive">
                <AlertCircle className="h-4 w-4" />
                <AlertTitle>Error</AlertTitle>
                <AlertDescription>{error?.message || "Failed to load leaderboard"}</AlertDescription>
            </Alert>
        );
    }

    return (
        <div className="space-y-4">
            {botData?.last_discovery_stats && (
                <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                    <Card className="border-border bg-card/50">
                        <CardHeader className="pb-2">
                            <CardTitle className="text-sm font-medium text-muted-foreground">Scanned Candidates</CardTitle>
                        </CardHeader>
                        <CardContent>
                            <div className="text-2xl font-bold font-mono">{botData.last_discovery_stats.total_candidates}</div>
                        </CardContent>
                    </Card>
                    <Card className="border-border bg-card/50">
                        <CardHeader className="pb-2">
                            <CardTitle className="text-sm font-medium text-muted-foreground">Passed All Filters</CardTitle>
                        </CardHeader>
                        <CardContent>
                            <div className="text-2xl font-bold font-mono text-polymarket-blue">{botData.last_discovery_stats.passed_all_filters}</div>
                        </CardContent>
                    </Card>
                    <Card className="border-border bg-card/50">
                        <CardHeader className="pb-2">
                            <CardTitle className="text-sm font-medium text-muted-foreground">Failed Win Rate</CardTitle>
                        </CardHeader>
                        <CardContent>
                            <div className="text-2xl font-bold font-mono text-muted-foreground">
                                {botData.last_discovery_stats.total_candidates - botData.last_discovery_stats.passed_win_rate}
                            </div>
                        </CardContent>
                    </Card>
                    <Card className="border-border bg-card/50">
                        <CardHeader className="pb-2">
                            <CardTitle className="text-sm font-medium text-muted-foreground">Failed Profit Factor</CardTitle>
                        </CardHeader>
                        <CardContent>
                            <div className="text-2xl font-bold font-mono text-muted-foreground">
                                {botData.last_discovery_stats.total_candidates - botData.last_discovery_stats.passed_profit_factor}
                            </div>
                        </CardContent>
                    </Card>
                </div>
            )}

            <Card className="border-border">
                <CardHeader className="flex flex-row items-center justify-between pb-2">
                    <div className="space-y-1">
                        <CardTitle className="text-xl font-bold flex items-center gap-2">
                            <Medal className="h-5 w-5 text-polymarket-blue" />
                            Top Wallets Leaderboard
                        </CardTitle>
                        <CardDescription>Highest scoring wallets discovered by the bot</CardDescription>
                    </div>
                    <Users className="h-5 w-5 text-muted-foreground" />
                </CardHeader>
                <CardContent>
                    <div className="rounded-md border border-border overflow-x-auto">
                        <Table>
                            <TableHeader>
                                <TableRow className="bg-muted/50">
                                    <TableHead className="w-[80px]">Rank</TableHead>
                                    <TableHead>Wallet</TableHead>
                                    <TableHead className="text-right">Score</TableHead>
                                    <TableHead className="text-right">Win Rate</TableHead>
                                    <TableHead className="text-right">30d ROI</TableHead>
                                    <TableHead className="text-right">Profit Factor</TableHead>
                                    <TableHead className="text-right">Avg Size</TableHead>
                                </TableRow>
                            </TableHeader>
                            <TableBody>
                                {isLoading ? (
                                    <TableRow>
                                        <TableCell colSpan={7} className="text-center py-8 text-muted-foreground">
                                            <div className="flex items-center justify-center gap-2">
                                                <Activity className="h-4 w-4 animate-spin" />
                                                Loading leaderboard rankings...
                                            </div>
                                        </TableCell>
                                    </TableRow>
                                ) : leaderboard?.length === 0 ? (
                                    <TableRow>
                                        <TableCell colSpan={7} className="text-center py-8 text-muted-foreground">
                                            No top performing wallets found yet.
                                        </TableCell>
                                    </TableRow>
                                ) : (
                                    leaderboard?.map((wallet, index) => (
                                        <TableRow key={wallet.wallet_address} className={index < 3 ? "bg-polymarket-blue/5" : ""}>
                                            <TableCell className="font-medium">
                                                {index === 0 ? "🥇 1" : index === 1 ? "🥈 2" : index === 2 ? "🥉 3" : index + 1}
                                            </TableCell>
                                            <TableCell>
                                                <div className="flex flex-col">
                                                    {wallet.label && <span className="text-sm font-semibold">{wallet.label}</span>}
                                                    <span className={`font-mono text-xs ${wallet.label ? "text-muted-foreground" : ""}`}>
                                                        {wallet.wallet_address.substring(0, 6)}...{wallet.wallet_address.substring(38)}
                                                    </span>
                                                </div>
                                            </TableCell>
                                            <TableCell className="text-right font-medium">
                                                {wallet.score.toFixed(1)}
                                            </TableCell>
                                            <TableCell className="text-right">
                                                {(wallet.win_rate * 100).toFixed(1)}%
                                            </TableCell>
                                            <TableCell className="text-right">
                                                <div className={`flex items-center justify-end gap-1 ${wallet.roi_30d > 0 ? "text-green-500" : wallet.roi_30d < 0 ? "text-red-500" : ""}`}>
                                                    {wallet.roi_30d > 0 ? <TrendingUp className="h-3 w-3" /> : wallet.roi_30d < 0 ? <TrendingDown className="h-3 w-3" /> : null}
                                                    {(wallet.roi_30d * 100).toFixed(1)}%
                                                </div>
                                            </TableCell>
                                            <TableCell className="text-right">
                                                {wallet.profit_factor.toFixed(2)}x
                                            </TableCell>
                                            <TableCell className="text-right">
                                                ${wallet.avg_position_size.toFixed(0)}
                                            </TableCell>
                                        </TableRow>
                                    ))
                                )}
                            </TableBody>
                        </Table>
                    </div>
                </CardContent>
            </Card>
        </div>
    );
}
