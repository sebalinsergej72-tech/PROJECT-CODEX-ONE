import { useQuery } from "@tanstack/react-query";

import { fetchPositions, fetchStatus, fetchTrades } from "@/lib/api";

export function useBotStatus(refreshMs = 5000) {
  return useQuery({
    queryKey: ["bot-status"],
    queryFn: fetchStatus,
    refetchInterval: refreshMs,
    retry: 1,
  });
}

export function useBotTrades(refreshMs = 5000) {
  return useQuery({
    queryKey: ["bot-trades"],
    queryFn: () => fetchTrades(15),
    refetchInterval: refreshMs,
    retry: 1,
  });
}

export function useBotPositions(refreshMs = 5000) {
  return useQuery({
    queryKey: ["bot-positions"],
    queryFn: () => fetchPositions(25),
    refetchInterval: refreshMs,
    retry: 1,
  });
}
