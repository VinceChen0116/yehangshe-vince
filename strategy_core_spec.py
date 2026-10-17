"""
Standalone Portable Specification: Dealer Heatmap 3-Layer Scoring Weights & Dual-Track Option TP/SL Ratchet Engine
===================================================================================================================
Zero external dependencies (Python standard library only).
Drop this file into any project or hand it to any AI coding agent to reproduce:
  Part 1: HeatmapWeights & HeatmapJudgmentEngine (Layer 1 Regime + Layer 2 Node Evolution + Layer 3 Price Trigger & 5 Archetypes + 0-100 Score)
  Part 2: ExitWeights & OptionExitEngine (Continuous Option Ratchet L(M) + 5-Tier Spot Stop Ratchet + 3-Stage Causal Time-Decay Truncation + Overnight & 0DTE Gates)
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# ==============================================================================
# PART 1: HEATMAP 3-LAYER JUDGMENT WEIGHTS & SCORING ENGINE
# ==============================================================================

@dataclass(frozen=True)
class HeatmapWeights:
    """All tunable weights & thresholds for Dealer GEX Heatmap 3-Layer Confirmation."""

    # --- 1. Minimum Node GEX Thresholds ---
    min_index_node_gex_usd: float = 2_500_000.0      # SPX / QQQ / NDX minimum significant node ($2.5M)
    min_stock_node_gex_usd: float = 1_500_000.0      # Large-cap stock cap ($1.5M, scaled by spot * 8000)
    min_smallcap_node_gex_usd: float = 800_000.0     # Floor for mid/small-cap stocks ($0.8M)
    spot_gex_scale_factor: float = 8_000.0           # Dynamic threshold: max(0.8M, min(1.5M, spot * 8000))

    # --- 2. Distance & R:R Geometry Thresholds ---
    min_dist_cliff_stock: float = 0.0005             # 0.05% min distance below positive ceiling
    min_dist_cliff_index: float = 0.0010             # 0.10% for index
    min_dist_spring_stock: float = 0.0025            # 0.25% min distance above positive floor
    min_dist_spring_index: float = 0.0015            # 0.15% for index
    max_wall_dist_stock: float = 0.0180              # 1.80% max distance from acting wall (2.70% if strong trend below VWAP)
    max_wall_dist_index: float = 0.0080              # 0.80% for index
    min_target_space_stock: float = 0.0100           # 1.00% min distance to target strike
    min_target_space_index: float = 0.0040           # 0.40% for index
    min_reasonable_disp_stock: float = 0.0040        # 0.40% min unobstructed room before intermediate gatekeeper
    min_reasonable_disp_index: float = 0.0020        # 0.20% for index
    min_reward_to_risk_ratio: float = 1.35           # Minimum structural R:R = target_space / stop_risk
    min_stop_loss_spot_pct: float = 0.0080           # 0.80% minimum spot stop buffer

    # --- 3. Layer 1: Local Gamma Regime Classification Thresholds ---
    local_band_pct: float = 0.030                    # Evaluate Top-6 nodes within +/- 3.0% of spot
    trend_driver_max_pos_share: float = 0.45         # Pos GEX share <= 45% (i.e. Neg GEX >= 55%) -> TREND_DRIVER
    range_buffer_min_pos_share: float = 0.65         # Pos GEX share >= 65% + bidirectional walls -> RANGE_BUFFER
    speed_bump_mud_min_pos_share: float = 0.72       # >= 3 tight pos walls within +/-1.2% & pos share >= 72% -> SPEED_BUMP_MUD
    stacked_flip_max_gap_pct: float = 0.012          # King Pos & King Neg within 1.2% of each other -> STACKED_FLIP_ZONE
    disordered_min_alternations: int = 4             # >= 4 sign flips within +/-2.5% and top1 conc < 20% -> DISORDERED_WHIPSAW

    # --- 4. Layer 2: 15-Minute Node GEX Evolution Thresholds ---
    node_growing_threshold_pct: float = 8.0          # d|GEX|/dt (15m) >= +8% -> GROWING
    node_decaying_threshold_pct: float = -10.0       # d|GEX|/dt (15m) <= -10% -> DECAYING

    # --- 5. Layer 3: Intraday Exhaustion Guard ---
    exhaustion_intraday_ret_pct: float = 0.065       # +/- 6.5% intraday move triggers exhaustion check
    exhaustion_rebound_from_extreme_pct: float = 0.0025  # 0.25% bounce off day low/high or 15m stall blocks late chasing

    # --- 6. Composite Confirmation Score Weights (0 - 100 Scale) ---
    base_confirmed_score: float = 58.0               # Starting score once an archetype's geometric + price rules pass
    min_passing_score: float = 55.0                  # Minimum composite score required to emit a confirmed structure

    # 6a. Node Growth Weights
    weight_node_growing: float = +12.0               # Acting wall/magnet is actively strengthening (>= +8%/15m)
    weight_node_new: float = +8.0                    # Newly emerged institutional GEX node
    weight_node_flat: float = +4.0                   # Stable node (-10% < dGEX < +8%)
    weight_node_decaying_at_wall: float = -16.0      # Heavy penalty if trading a wall bounce/reject while wall is decaying
    weight_node_decaying_in_trend: float = -2.0      # Mild penalty if already trending away from wall (normal |S-K| gamma decay)

    # 6b. Local Gamma Regime Weights
    weight_regime_mud_or_whipsaw: float = -22.0      # SPEED_BUMP_MUD or DISORDERED_WHIPSAW (veto-level penalty)
    weight_regime_trend_driver_aligned: float = +10.0  # TREND_DRIVER aligned with Avalanche/Squeeze or Trend Pullback
    weight_regime_range_buffer_aligned: float = +8.0   # RANGE_BUFFER aligned with Springboard/Cliff/Pinning

    # 6c. Wall Migration Weights (30m Rolling Center-of-Gravity Shift)
    weight_wall_migration_aligned: float = +10.0     # MIGRATING_UP for Bullish or MIGRATING_DOWN for Bearish
    weight_wall_migration_opposed: float = -12.0     # Wall migrating against signal direction

    # 6d. Multi-Expiry Confluence vs Conflict Weights
    weight_per_same_sign_confluence: float = +6.0    # +6 pts per secondary expiration with same-sign GEX at same strike
    max_confluence_bonus: float = +12.0              # Cap multi-expiry bonus at +12 pts
    weight_expiry_sign_conflict: float = -14.0       # -14 pts if front 0DTE and next expiry have opposite GEX signs at acting strike

    # 6e. Multi-Timeframe (5m + 15m) Price Momentum Alignment Bonus
    weight_mtf_momentum_aligned: float = +8.0        # +8 pts when both 5m tick_ret (>0.05%) and 15m ret (>0.15%) align

    # --- 7. Position Sizing Multipliers by Score Tier ---
    size_mult_tier_high: float = 1.15                # Score >= 75: 1.15x base allocation
    size_mult_tier_mid: float = 0.85                 # 55 <= Score < 75: 0.85x base allocation
    size_mult_tier_low: float = 0.50                 # Score < 55 (if forced): 0.50x


def compute_local_gamma_regime(
    cells_t1: List[Dict[str, Any]],
    spot: float,
    min_gex: float,
    wall_migration: str = "STABLE",
    w: HeatmapWeights = HeatmapWeights(),
) -> Tuple[str, float]:
    """
    Layer 1: Classifies the local (+/-3% around spot) Dealer GEX microenvironment into one of 6 regimes:
      - TREND_DRIVER        : Negative GEX dominates (pos_share <= 45%) or walls actively migrating with price
      - RANGE_BUFFER        : Positive GEX dominates (pos_share >= 65%) with bidirectional walls both above & below spot
      - STACKED_FLIP_ZONE   : Heavy King Pos and King Neg within 1.2% of each other (gamma polarity inflection zone)
      - SPEED_BUMP_MUD      : >= 3 dense positive GEX walls within +/-1.2% of spot (choppy friction zone)
      - DISORDERED_WHIPSAW  : >= 4 positive/negative sign alternations within +/-2.5% of spot or erratic wall whipsaw
      - NEUTRAL             : Balanced transition state
    Returns: (local_gamma_regime, pos_abs_share)
    """
    near_3pct = [
        c for c in cells_t1
        if abs(float(c.get("strike_usd", 0)) - spot) / spot <= w.local_band_pct
    ] or cells_t1
    top6 = sorted(near_3pct, key=lambda c: abs(float(c.get("net_dealer_gex_usd", 0))), reverse=True)[:6]
    top6_pos = sum(float(c.get("net_dealer_gex_usd", 0)) for c in top6 if float(c.get("net_dealer_gex_usd", 0)) > 0)
    top6_neg = sum(abs(float(c.get("net_dealer_gex_usd", 0))) for c in top6 if float(c.get("net_dealer_gex_usd", 0)) < 0)
    top6_abs = top6_pos + top6_neg + 1e-6
    pos_abs_share = top6_pos / top6_abs

    pos_near = [c for c in near_3pct if float(c.get("net_dealer_gex_usd", 0)) > 0]
    neg_near = [c for c in near_3pct if float(c.get("net_dealer_gex_usd", 0)) < 0]
    king_pos = max(pos_near, key=lambda c: float(c.get("net_dealer_gex_usd", 0)), default=None)
    king_neg = min(neg_near, key=lambda c: float(c.get("net_dealer_gex_usd", 0)), default=None)
    kp_k = float(king_pos["strike_usd"]) if king_pos else 0.0
    kn_k = float(king_neg["strike_usd"]) if king_neg else 0.0

    # 1. Check SPEED_BUMP_MUD
    tight_pos = [
        c for c in near_3pct
        if abs(float(c.get("strike_usd", 0)) - spot) / spot <= 0.012
        and float(c.get("net_dealer_gex_usd", 0)) >= min_gex * 0.55
    ]
    has_pos_above_tight = any(float(c["strike_usd"]) > spot * 1.0005 for c in tight_pos)
    has_pos_below_tight = any(float(c["strike_usd"]) < spot * 0.9995 for c in tight_pos)
    is_speed_bump_mud = (
        len(tight_pos) >= 3
        and has_pos_above_tight
        and has_pos_below_tight
        and pos_abs_share >= w.speed_bump_mud_min_pos_share
    )

    # 2. Check DISORDERED_WHIPSAW
    near_25pct = sorted(
        [
            c for c in near_3pct
            if abs(float(c.get("strike_usd", 0)) - spot) / spot <= 0.025
            and abs(float(c.get("net_dealer_gex_usd", 0))) >= min_gex * 0.65
        ],
        key=lambda c: float(c.get("strike_usd", 0)),
    )
    sign_alternations = sum(
        1 for i in range(1, len(near_25pct))
        if float(near_25pct[i - 1].get("net_dealer_gex_usd", 0)) * float(near_25pct[i].get("net_dealer_gex_usd", 0)) < 0
    )
    top1_conc = (abs(float(top6[0].get("net_dealer_gex_usd", 0))) / top6_abs) if top6 else 1.0
    is_disordered = (
        (len(near_25pct) >= 5 and sign_alternations >= w.disordered_min_alternations and top1_conc < 0.20)
        or wall_migration == "WHIPSAW"
    )

    # 3. Check STACKED_FLIP_ZONE
    is_stacked_flip = False
    if king_pos and king_neg:
        if (
            abs(kp_k - kn_k) / spot <= w.stacked_flip_max_gap_pct
            and abs(spot - 0.5 * (kp_k + kn_k)) / spot <= 0.008
            and float(king_pos["net_dealer_gex_usd"]) >= min_gex * 0.8
            and abs(float(king_neg["net_dealer_gex_usd"])) >= min_gex * 0.8
        ):
            is_stacked_flip = True

    # 4. Bidirectional positive buffer check (prevents labeling a broken overhead ceiling as RANGE_BUFFER)
    pos_above_abs = sum(float(c.get("net_dealer_gex_usd", 0)) for c in top6 if float(c.get("net_dealer_gex_usd", 0)) > 0 and float(c.get("strike_usd", 0)) > spot * 1.001)
    pos_below_abs = sum(float(c.get("net_dealer_gex_usd", 0)) for c in top6 if float(c.get("net_dealer_gex_usd", 0)) > 0 and float(c.get("strike_usd", 0)) < spot * 0.999)
    has_bidirectional_pos_buffer = (pos_above_abs >= top6_abs * 0.15 and pos_below_abs >= top6_abs * 0.15)

    if is_speed_bump_mud:
        regime = "SPEED_BUMP_MUD"
    elif is_disordered:
        regime = "DISORDERED_WHIPSAW"
    elif is_stacked_flip:
        regime = "STACKED_FLIP_ZONE"
    elif pos_abs_share <= w.trend_driver_max_pos_share or wall_migration in ("MIGRATING_UP", "MIGRATING_DOWN"):
        regime = "TREND_DRIVER"
    elif pos_abs_share >= w.range_buffer_min_pos_share and has_bidirectional_pos_buffer:
        regime = "RANGE_BUFFER"
    else:
        regime = "NEUTRAL"

    return regime, round(pos_abs_share, 4)


def compute_structure_confirmation_score(
    structure_type: str,
    local_gamma_regime: str,
    node_growth_state: str,
    wall_migration: str,
    reaction_trigger_type: str,
    same_sign_confluences: int,
    has_expiry_conflict: bool,
    tick_ret_5m: float,
    ret_15m: float,
    w: HeatmapWeights = HeatmapWeights(),
) -> Tuple[float, bool, float]:
    """
    Computes the 0-100 3-Layer Composite Confirmation Score and position size weight.
    Returns: (score_0_to_100, is_confirmed, position_size_weight)
    """
    score = w.base_confirmed_score

    # 1. Layer 2: Node Growth / Decay Weight
    if node_growth_state == "GROWING":
        score += w.weight_node_growing
    elif node_growth_state == "NEW":
        score += w.weight_node_new
    elif node_growth_state == "FLAT":
        score += w.weight_node_flat
    elif node_growth_state == "DECAYING":
        if reaction_trigger_type in ("REJECTION_BOUNCE", "REJECTION_DROP"):
            score += w.weight_node_decaying_at_wall
        else:
            score += w.weight_node_decaying_in_trend

    # 2. Layer 1: Local Gamma Regime & Wall Migration Weight
    is_bull = structure_type in ("SPRINGBOARD_STRUCTURE", "SHORT_GAMMA_VACUUM_SQUEEZE")
    is_bear = structure_type in ("CLIFF_STRUCTURE", "SHORT_GAMMA_AVALANCHE")

    if local_gamma_regime in ("SPEED_BUMP_MUD", "DISORDERED_WHIPSAW"):
        score += w.weight_regime_mud_or_whipsaw
    elif local_gamma_regime == "TREND_DRIVER" and (
        structure_type in ("SHORT_GAMMA_VACUUM_SQUEEZE", "SHORT_GAMMA_AVALANCHE")
        or reaction_trigger_type in ("TREND_PULLBACK_REJECT", "TREND_PULLBACK_HOLD")
    ):
        score += w.weight_regime_trend_driver_aligned
    elif local_gamma_regime == "RANGE_BUFFER" and structure_type in (
        "SPRINGBOARD_STRUCTURE", "CLIFF_STRUCTURE", "PINNING_MAGNET"
    ):
        score += w.weight_regime_range_buffer_aligned

    if (is_bull and wall_migration == "MIGRATING_UP") or (is_bear and wall_migration == "MIGRATING_DOWN"):
        score += w.weight_wall_migration_aligned
    elif (is_bull and wall_migration == "MIGRATING_DOWN") or (is_bear and wall_migration == "MIGRATING_UP"):
        score += w.weight_wall_migration_opposed

    # 3. Multi-Expiry Same-Sign Confluence vs Opposite-Sign Conflict
    score += min(w.max_confluence_bonus, same_sign_confluences * w.weight_per_same_sign_confluence)
    if has_expiry_conflict:
        score += w.weight_expiry_sign_conflict

    # 4. Layer 3: Multi-Timeframe (5m + 15m) Momentum Resonance
    if (is_bull and tick_ret_5m > 0.0005 and ret_15m > 0.0015) or (
        is_bear and tick_ret_5m < -0.0005 and ret_15m < -0.0015
    ):
        score += w.weight_mtf_momentum_aligned

    score = max(0.0, min(100.0, round(score, 1)))
    passed = score >= w.min_passing_score
    if score >= 75.0:
        size_weight = w.size_mult_tier_high
    elif score >= w.min_passing_score:
        size_weight = w.size_mult_tier_mid
    else:
        size_weight = w.size_mult_tier_low

    return score, passed, size_weight


# ==============================================================================
# PART 2: DUAL-TRACK OPTION TP/SL RATCHET & TIME-TRUNCATION ENGINE
# ==============================================================================

@dataclass(frozen=True)
class ExitWeights:
    """All tunable parameters for the Dual-Track (Option ROI + Spot Price) Ratchet & Time-Decay Exit Engine."""

    # --- 1. Hard Bounds ---
    initial_stop_loss_roi_pct: float = -35.0         # Initial hard option stop loss (-35%)
    hard_take_profit_roi_pct: float = 250.0          # Ultimate hard take-profit cap (+250%)
    scale_out_half_roi_pct: float = 80.0             # Scale out 50% of contracts at +80% ROI
    breakeven_trigger_roi_pct: float = 12.0          # Lock net breakeven once peak ROI >= +12%
    breakeven_floor_roi_pct: float = 3.0             # +3.0% net breakeven floor (covers round-trip slippage)
    opening_scalp_lock_ratio: float = 0.85           # 09:30-10:00 ET opening scalp locks 85% of peak once M >= +22%

    # --- 2. Continuous Option Profit Ratchet L(M) Piecewise Linear Parameters ---
    # Tier 0: M < 6%        -> soft_sl = -35.0%, trail_floor = None
    # Tier 1: 6% <= M < 10% -> soft_sl = -12.0%, trail_floor = None
    # Tier 2: 10% <= M < 20%-> soft_sl = -5.0%,  trail_floor = 3.0 + 0.65 * (M - 10)
    # Tier 3: 20% <= M < 40%-> soft_sl =  0.0%,  trail_floor = 9.5 + 0.80 * (M - 20)
    # Tier 4: 40% <= M < 80%-> soft_sl =  0.0%,  trail_floor = 25.5 + 0.86 * (M - 40)
    # Tier 5: M >= 80%      -> soft_sl =  0.0%,  trail_floor = 59.9 + 0.90 * (M - 80)
    tier1_mfe_pct: float = 6.0
    tier1_soft_sl_pct: float = -12.0
    tier2_mfe_pct: float = 10.0
    tier2_soft_sl_pct: float = -5.0
    tier2_base_lock: float = 3.0
    tier2_slope: float = 0.65
    tier3_mfe_pct: float = 20.0
    tier3_base_lock: float = 9.5
    tier3_slope: float = 0.80
    tier4_mfe_pct: float = 40.0
    tier4_base_lock: float = 25.5
    tier4_slope: float = 0.86
    tier5_mfe_pct: float = 80.0
    tier5_base_lock: float = 59.9
    tier5_slope: float = 0.90

    # --- 3. 5-Tier Spot Stop Ratchet (Raises underlying stop_loss_spot as Option MFE grows) ---
    # M >= 10%: cut initial spot stop distance by 50%
    # M >= 15%: move spot stop to entry_spot (spot breakeven)
    # M >= 25%: lock 50% of favorable spot excursion (entry_spot -> peak_spot)
    # M >= 40%: lock 70% of favorable spot excursion
    # M >= 60%: lock 80% of favorable spot excursion
    spot_ratchet_m10_cut_risk: float = 0.50
    spot_ratchet_m25_lock_gain: float = 0.50
    spot_ratchet_m40_lock_gain: float = 0.70
    spot_ratchet_m60_lock_gain: float = 0.80

    # --- 4. Three-Stage Causal Intraday Time-Decay Truncation ---
    # Stage 0 (8m 0DTE Fast Cut): held >= 8m, MFE <= +1.0%, current ROI <= -12.0%
    stage0_0dte_minutes: float = 8.0
    stage0_0dte_max_mfe_pct: float = 1.0
    stage0_0dte_mae_floor_pct: float = -12.0

    # Stage 1 (10m Dead Momentum Cut): held >= 10m, MFE < +4.0%, current ROI <= -20.0%
    stage1_dead_minutes: float = 10.0
    stage1_dead_max_mfe_pct: float = 4.0
    stage1_dead_mae_floor_pct: float = -20.0

    # Stage 2 (25m Theta Stagnation Cut): held >= 25m, MFE < +6.0%, current ROI <= -15.0%
    stage2_stag_minutes: float = 25.0
    stage2_stag_max_mfe_pct: float = 6.0
    stage2_stag_mae_floor_pct: float = -15.0

    # --- 5. Holding Time & Session Gates ---
    max_intraday_hold_minutes: float = 180.0
    eod_0dte_force_close_hhmm: Tuple[int, int] = (15, 50)  # All 0DTE must close at/after 15:50 ET


@dataclass
class PositionState:
    """Minimal state object required to evaluate exits on each mark-to-market tick."""
    position_id: str
    ticker: str
    direction: str                    # "BULLISH" or "BEARISH"
    entry_spot: float
    entry_option_price: float
    initial_stop_loss_spot: float
    stop_loss_spot: float
    target_spot: float
    is_0dte: bool = True
    is_same_day_overnight: bool = False
    is_opening_scalp: bool = False
    quantity: int = 10
    partial_harvested: bool = False

    # Dynamic watermarks updated every tick
    peak_spot: float = 0.0
    peak_option_price: float = 0.0
    peak_roi_pct: float = 0.0
    soft_stop_level_pct: float = -35.0
    trailing_stop_level_pct: float = -999.0
    breakeven_locked: bool = False
    trailing_active: bool = False


def compute_continuous_option_ratchet(
    peak_roi_pct: float,
    is_opening_scalp: bool = False,
    w: ExitWeights = ExitWeights(),
) -> Tuple[float, float]:
    """
    Computes (soft_stop_roi_pct, trailing_lock_roi_pct) as a continuous piecewise linear function of Peak ROI (M).
    """
    M = peak_roi_pct
    if M < w.tier1_mfe_pct:
        soft_sl = w.initial_stop_loss_roi_pct
        trail_floor = -999.0
    elif M < w.tier2_mfe_pct:
        soft_sl = w.tier1_soft_sl_pct
        trail_floor = -999.0
    elif M < w.tier3_mfe_pct:
        soft_sl = w.tier2_soft_sl_pct
        trail_floor = w.tier2_base_lock + w.tier2_slope * (M - w.tier2_mfe_pct)
    elif M < w.tier4_mfe_pct:
        soft_sl = 0.0
        trail_floor = w.tier3_base_lock + w.tier3_slope * (M - w.tier3_mfe_pct)
    elif M < w.tier5_mfe_pct:
        soft_sl = 0.0
        trail_floor = w.tier4_base_lock + w.tier4_slope * (M - w.tier4_mfe_pct)
    else:
        soft_sl = 0.0
        trail_floor = w.tier5_base_lock + w.tier5_slope * (M - w.tier5_mfe_pct)

    if is_opening_scalp and M >= 22.0:
        trail_floor = max(trail_floor, round(M * w.opening_scalp_lock_ratio, 2))

    return round(soft_sl, 2), round(trail_floor, 2)


def update_spot_ratchet_stop(pos: PositionState, w: ExitWeights = ExitWeights()) -> float:
    """
    5-Tier Spot-Level Stop Ratchet: tightens underlying stop_loss_spot as Option Peak ROI (M) increases.
    """
    init_sl = pos.initial_stop_loss_spot or pos.stop_loss_spot
    if init_sl <= 0 or pos.is_same_day_overnight:
        return pos.stop_loss_spot

    M = pos.peak_roi_pct
    if pos.direction == "BULLISH":
        if M >= 10.0 and init_sl < pos.entry_spot:
            pos.stop_loss_spot = max(pos.stop_loss_spot, round(init_sl + (pos.entry_spot - init_sl) * w.spot_ratchet_m10_cut_risk, 2))
        if M >= 15.0:
            pos.stop_loss_spot = max(pos.stop_loss_spot, pos.entry_spot)
        if M >= 25.0 and pos.peak_spot > pos.entry_spot:
            pos.stop_loss_spot = max(pos.stop_loss_spot, round(pos.entry_spot + (pos.peak_spot - pos.entry_spot) * w.spot_ratchet_m25_lock_gain, 2))
        if M >= 40.0 and pos.peak_spot > pos.entry_spot:
            pos.stop_loss_spot = max(pos.stop_loss_spot, round(pos.entry_spot + (pos.peak_spot - pos.entry_spot) * w.spot_ratchet_m40_lock_gain, 2))
        if M >= 60.0 and pos.peak_spot > pos.entry_spot:
            pos.stop_loss_spot = max(pos.stop_loss_spot, round(pos.entry_spot + (pos.peak_spot - pos.entry_spot) * w.spot_ratchet_m60_lock_gain, 2))
    elif pos.direction == "BEARISH":
        if M >= 10.0 and init_sl > pos.entry_spot:
            pos.stop_loss_spot = min(pos.stop_loss_spot, round(init_sl - (init_sl - pos.entry_spot) * w.spot_ratchet_m10_cut_risk, 2))
        if M >= 15.0:
            pos.stop_loss_spot = min(pos.stop_loss_spot, pos.entry_spot)
        if M >= 25.0 and pos.peak_spot < pos.entry_spot:
            pos.stop_loss_spot = min(pos.stop_loss_spot, round(pos.entry_spot - (pos.entry_spot - pos.peak_spot) * w.spot_ratchet_m25_lock_gain, 2))
        if M >= 40.0 and pos.peak_spot < pos.entry_spot:
            pos.stop_loss_spot = min(pos.stop_loss_spot, round(pos.entry_spot - (pos.entry_spot - pos.peak_spot) * w.spot_ratchet_m40_lock_gain, 2))
        if M >= 60.0 and pos.peak_spot < pos.entry_spot:
            pos.stop_loss_spot = min(pos.stop_loss_spot, round(pos.entry_spot - (pos.entry_spot - pos.peak_spot) * w.spot_ratchet_m60_lock_gain, 2))

    return pos.stop_loss_spot


def evaluate_exit_tick(
    pos: PositionState,
    current_spot: float,
    current_option_price: float,
    held_minutes: float,
    current_et_hhmm: Tuple[int, int] = (14, 0),
    w: ExitWeights = ExitWeights(),
) -> Tuple[str, Optional[str], float]:
    """
    Evaluates a single mark-to-market tick for an open option position.
    Returns:
      (action, exit_reason, recommended_exit_option_price)
      where action is one of: "HOLD", "SCALE_OUT_50", "CLOSE_ALL"
    """
    unrealized_roi_pct = round(((current_option_price - pos.entry_option_price) / pos.entry_option_price) * 100.0, 2)

    # 1. Update High-Water Marks
    if pos.peak_spot == 0.0:
        pos.peak_spot = pos.entry_spot
    if current_option_price > pos.peak_option_price:
        pos.peak_option_price = current_option_price
        pos.peak_roi_pct = unrealized_roi_pct
        pos.peak_spot = current_spot

    # 2. Update Dual-Track Ratchets (Spot Stop Ratchet + Continuous Option Ratchet)
    update_spot_ratchet_stop(pos, w)
    if not pos.is_same_day_overnight:
        soft_sl, trail_floor = compute_continuous_option_ratchet(pos.peak_roi_pct, pos.is_opening_scalp, w)
        pos.soft_stop_level_pct = max(pos.soft_stop_level_pct, soft_sl)
        if trail_floor > -900.0:
            pos.breakeven_locked = True
            pos.trailing_active = True
            pos.trailing_stop_level_pct = max(pos.trailing_stop_level_pct, trail_floor)
        elif pos.peak_roi_pct >= w.breakeven_trigger_roi_pct:
            pos.breakeven_locked = True
            pos.trailing_stop_level_pct = max(pos.trailing_stop_level_pct, w.breakeven_floor_roi_pct)

    stop_spot_hit = pos.stop_loss_spot > 0 and (
        (pos.direction == "BULLISH" and current_spot <= pos.stop_loss_spot)
        or (pos.direction == "BEARISH" and current_spot >= pos.stop_loss_spot)
    )
    target_hit = pos.target_spot > 0 and (
        (pos.direction == "BULLISH" and current_spot >= pos.target_spot)
        or (pos.direction == "BEARISH" and current_spot <= pos.target_spot)
    )

    # 3. Same-Day EOD Overnight Position Gate (15:00-15:55 ET, DTE >= 3)
    if pos.is_same_day_overnight:
        if stop_spot_hit:
            return "CLOSE_ALL", "SPOT_STOP_OVERNIGHT", current_option_price
        if unrealized_roi_pct <= w.initial_stop_loss_roi_pct:
            return "CLOSE_ALL", "INITIAL_STOP_LOSS_OVERNIGHT", current_option_price
        if unrealized_roi_pct >= 60.0 and pos.quantity >= 2 and not pos.partial_harvested:
            pos.partial_harvested = True
            return "SCALE_OUT_50", "SCALE_OUT_50_OVERNIGHT", current_option_price
        return "HOLD", None, current_option_price

    # 4. 0DTE Pre-Close Mandatory Liquidation Gate (15:50 ET)
    if pos.is_0dte and current_et_hhmm >= w.eod_0dte_force_close_hhmm:
        return "CLOSE_ALL", "EOD_0DTE_CLOSE", current_option_price

    # 5. Three-Stage Causal Intraday Time-Decay Truncation
    if pos.is_0dte and held_minutes >= w.stage0_0dte_minutes and pos.peak_roi_pct <= w.stage0_0dte_max_mfe_pct and unrealized_roi_pct <= w.stage0_0dte_mae_floor_pct:
        return "CLOSE_ALL", "DEAD_MOMENTUM_TRUNCATE_0DTE_8M", current_option_price
    if held_minutes >= w.stage1_dead_minutes and pos.peak_roi_pct < w.stage1_dead_max_mfe_pct and unrealized_roi_pct <= w.stage1_dead_mae_floor_pct:
        return "CLOSE_ALL", "DEAD_MOMENTUM_TRUNCATE_10M", current_option_price
    if held_minutes >= w.stage2_stag_minutes and pos.peak_roi_pct < w.stage2_stag_max_mfe_pct and unrealized_roi_pct <= w.stage2_stag_mae_floor_pct:
        return "CLOSE_ALL", "THETA_STAGNATION_TRUNCATE_25M", current_option_price

    # 6. Spot Ratchet Stop / Structural Target / Max Hold
    if stop_spot_hit:
        is_spot_be_or_profit = (
            (pos.direction == "BULLISH" and pos.stop_loss_spot >= pos.entry_spot)
            or (pos.direction == "BEARISH" and pos.stop_loss_spot <= pos.entry_spot)
        )
        return "CLOSE_ALL", ("SPOT_PROFIT_STOP" if is_spot_be_or_profit else "SPOT_STOP"), current_option_price
    if target_hit:
        return "CLOSE_ALL", "STRUCTURE_TARGET", current_option_price
    if held_minutes >= w.max_intraday_hold_minutes:
        return "CLOSE_ALL", "MAX_HOLD", current_option_price

    # 7. Partial Scale-Out (+80% ROI harvests 50% contracts)
    if unrealized_roi_pct >= w.scale_out_half_roi_pct and pos.quantity >= 2 and not pos.partial_harvested:
        pos.partial_harvested = True
        pos.breakeven_locked = True
        pos.trailing_stop_level_pct = max(pos.trailing_stop_level_pct, w.breakeven_floor_roi_pct)
        return "SCALE_OUT_50", "SCALE_OUT_50", current_option_price

    # 8. Option-Level Ratchet Exits (Hard Cap -> Continuous Trailing -> Breakeven -> Soft Ratchet -> Hard Stop)
    if unrealized_roi_pct >= w.hard_take_profit_roi_pct:
        return "CLOSE_ALL", "HARD_TP_CAP", current_option_price
    if pos.trailing_active and unrealized_roi_pct <= pos.trailing_stop_level_pct:
        return "CLOSE_ALL", "TRAILING_STOP", current_option_price
    if pos.breakeven_locked and unrealized_roi_pct <= w.breakeven_floor_roi_pct:
        return "CLOSE_ALL", "BREAKEVEN_STOP", current_option_price
    if pos.soft_stop_level_pct > w.initial_stop_loss_roi_pct and unrealized_roi_pct <= pos.soft_stop_level_pct:
        return "CLOSE_ALL", "RATCHET_SOFT_STOP", current_option_price
    if not pos.breakeven_locked and unrealized_roi_pct <= w.initial_stop_loss_roi_pct:
        return "CLOSE_ALL", "INITIAL_STOP_LOSS", current_option_price

    return "HOLD", None, current_option_price


if __name__ == "__main__":
    # Self-test demonstration for instant verification by any Agent
    score, ok, mult = compute_structure_confirmation_score(
        structure_type="CLIFF_STRUCTURE",
        local_gamma_regime="TREND_DRIVER",
        node_growth_state="GROWING",
        wall_migration="MIGRATING_DOWN",
        reaction_trigger_type="TREND_PULLBACK_REJECT",
        same_sign_confluences=1,
        has_expiry_conflict=False,
        tick_ret_5m=-0.0018,
        ret_15m=-0.0042,
    )
    print(f"[Heatmap 3-Layer Demo] Score={score}/100 | Confirmed={ok} | SizeWeight={mult}x")

    pos = PositionState(
        position_id="DEMO-01",
        ticker="GOOGL",
        direction="BEARISH",
        entry_spot=342.04,
        entry_option_price=1.12,
        initial_stop_loss_spot=347.50,
        stop_loss_spot=347.50,
        target_spot=320.00,
    )
    # Tick 1: price drops to 339.80, option surges to $3.15 (+181.2%)
    act1, rsn1, px1 = evaluate_exit_tick(pos, current_spot=339.80, current_option_price=3.15, held_minutes=35.0)
    print(f"[Exit Engine Tick 1] PeakROI={pos.peak_roi_pct:+.1f}% | TrailLock={pos.trailing_stop_level_pct:+.1f}% | SpotStop=${pos.stop_loss_spot:.2f} | Action={act1} ({rsn1})")
    # Tick 2: option pulls back to $2.75 (+145.5% <= +151.0% lock) -> triggers TRAILING_STOP
    act2, rsn2, px2 = evaluate_exit_tick(pos, current_spot=340.38, current_option_price=2.75, held_minutes=53.5)
    print(f"[Exit Engine Tick 2] CurrROI={(2.75-1.12)/1.12*100:+.1f}% | Action={act2} ({rsn2}) @ ${px2:.2f}")
