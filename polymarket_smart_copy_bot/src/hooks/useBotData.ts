import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  closePosition,
  fetchLeaderboard,
  fetchOpenOrders,
  fetchPortfolioHistory,
  fetchPositions,
  fetchStatus,
  fetchTrades,
} from "@/lib/api";

export function useBotStatus(refreshMs = 5000) {
  return useQuery({
    queryKey: ["bot-status"],
    queryFn: fetchStatus,
    refetchInterval: Math.max(refreshMs, 10000),
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: 1,
  });
}

export function useBotTrades(refreshMs = 5000) {
  return useQuery({
    queryKey: ["bot-trades"],
    queryFn: () => fetchTrades(15),
    refetchInterval: Math.max(refreshMs, 10000),
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: 1,
  });
}

export function useBotPositions(refreshMs = 5000) {
  return useQuery({
    queryKey: ["bot-positions"],
    queryFn: () => fetchPositions(50),
    refetchInterval: Math.max(refreshMs, 10000),
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: 1,
  });
}

export function useBotOpenOrders(refreshMs = 5000) {
  return useQuery({
    queryKey: ["bot-open-orders"],
    queryFn: () => fetchOpenOrders(25),
    refetchInterval: Math.max(refreshMs, 10000),
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: 1,
  });
}

export function usePortfolioHistory(hours = 168, refreshMs = 30000) {
  return useQuery({
    queryKey: ["bot-portfolio-history", hours],
    queryFn: () => fetchPortfolioHistory(hours),
    refetchInterval: Math.max(refreshMs, 60000),
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: 1,
  });
}

export function useLeaderboard(refreshMs = 15000) {
  return useQuery({
    queryKey: ["leaderboard"],
    queryFn: () => fetchLeaderboard(50),
    refetchInterval: Math.max(refreshMs, 60000),
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: 1,
  });
}

export function useClosePosition() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (positionId: number) => closePosition(positionId),
    onSuccess: () => {
      toast.success("Position close requested successfully");
      queryClient.invalidateQueries({ queryKey: ["bot-positions"] });
      queryClient.invalidateQueries({ queryKey: ["bot-portfolio-history"] });
      queryClient.invalidateQueries({ queryKey: ["bot-status"] });
    },
    onError: (error) => {
      toast.error(`Failed to close position: ${error.message}`);
    },
  });
}
