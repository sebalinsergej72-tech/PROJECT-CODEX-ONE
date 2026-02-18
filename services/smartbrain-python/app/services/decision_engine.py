from app.models.schemas import TradeDecision
from app.services.llm_reasoner import llm_reasoner
from app.services.predictors import ensemble_predictor
from app.services.rl_agent import rl_trading_agent


class DecisionEngine:
    def make_decision(self, state: dict, max_leverage: int = 3) -> TradeDecision:
        ensemble = ensemble_predictor.predict(state)
        rl = rl_trading_agent.infer(state)
        llm = llm_reasoner.second_opinion(state=state, ensemble_confidence=ensemble.confidence)

        adjusted_confidence = max(0.0, min(1.0, (ensemble.confidence + rl.confidence) / 2 + llm["confidence_adjustment"]))

        action = rl.action
        if ensemble.direction_score > 0.2 and adjusted_confidence > 0.6:
            action = "LONG"
        elif ensemble.direction_score < -0.2 and adjusted_confidence > 0.6:
            action = "SHORT"

        leverage = min(max(1, rl.leverage), max_leverage)

        return TradeDecision(
            action=action,
            size_pct=max(0.0, min(0.2, rl.size_pct)),
            leverage=leverage,
            confidence=adjusted_confidence,
            sl_price=None,
            tp_price=None,
            reason=llm["rationale"],
        )


decision_engine = DecisionEngine()
