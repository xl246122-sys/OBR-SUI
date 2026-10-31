"""P5 C-01 Exit Optimization, OCO/Protection Anti-Over-Reservation Coordinator, and Two-Layer Fair Backtester."""

from sui_quant.exit.c01_strategies import (
    C1FixedNetTargetExit,
    C2GridTrailingExit,
    C3AdaptiveHybridExit,
    CoordinatorStepReport,
    ExitFillSlice,
    ExitSignalAction,
    ExitStrategyId,
    OcoProtectionCoordinator,
    TranchePlanResult,
    TrancheSlice,
    compute_fee_covered_breakeven_price,
)
from sui_quant.exit.fair_backtester import (
    BacktestLayer,
    EntryTradeContext,
    ExitPathBar,
    FairExitComparisonBacktester,
    FairExitComparisonReport,
    StrategyPerformanceSummary,
    TradeExitSimulationResult,
)

__all__ = [
    "BacktestLayer",
    "C1FixedNetTargetExit",
    "C2GridTrailingExit",
    "C3AdaptiveHybridExit",
    "CoordinatorStepReport",
    "EntryTradeContext",
    "ExitFillSlice",
    "ExitPathBar",
    "ExitSignalAction",
    "ExitStrategyId",
    "FairExitComparisonBacktester",
    "FairExitComparisonReport",
    "OcoProtectionCoordinator",
    "StrategyPerformanceSummary",
    "TradeExitSimulationResult",
    "TranchePlanResult",
    "TrancheSlice",
    "compute_fee_covered_breakeven_price",
]
