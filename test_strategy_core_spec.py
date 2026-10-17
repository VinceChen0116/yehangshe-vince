"""strategy_core_spec 离线测试：Heatmap 三层评分 + 双轨棘轮止盈止损。0 配额。

运行：python -m unittest test_strategy_core_spec -v
"""
from __future__ import annotations

import unittest

from strategy_core_spec import (
    ExitWeights,
    HeatmapWeights,
    PositionState,
    compute_continuous_option_ratchet,
    compute_local_gamma_regime,
    compute_structure_confirmation_score,
    evaluate_exit_tick,
    update_spot_ratchet_stop,
)

G_MIN = 2_500_000.0
SPOT = 100.0


def cell(strike: float, gex: float) -> dict:
    return {"strike_usd": strike, "net_dealer_gex_usd": gex}


def regime(cells: list[dict], migration: str = "STABLE") -> str:
    return compute_local_gamma_regime(cells, SPOT, G_MIN, migration)[0]


class LocalGammaRegimeTest(unittest.TestCase):
    def test_speed_bump_mud(self):
        cells = [cell(99.0, 3e6), cell(100.5, 3e6), cell(101.0, 3e6)]
        self.assertEqual(regime(cells), "SPEED_BUMP_MUD")

    def test_disordered_by_alternation(self):
        strikes = [97.6, 98.4, 99.2, 100.8, 101.6, 102.4]
        cells = [cell(k, 3e6 if i % 2 == 0 else -3e6) for i, k in enumerate(strikes)]
        self.assertEqual(regime(cells), "DISORDERED_WHIPSAW")

    def test_disordered_by_wall_whipsaw(self):
        self.assertEqual(regime([cell(102.0, 5e6), cell(97.0, -4e6)], "WHIPSAW"), "DISORDERED_WHIPSAW")

    def test_stacked_flip(self):
        self.assertEqual(regime([cell(99.5, 5e6), cell(100.5, -5e6)]), "STACKED_FLIP_ZONE")

    def test_trend_driver_by_neg_share(self):
        cells = [cell(102.0, -10e6), cell(97.0, 2e6)]
        r, share = compute_local_gamma_regime(cells, SPOT, G_MIN)
        self.assertEqual(r, "TREND_DRIVER")
        self.assertAlmostEqual(share, 2 / 12, places=4)

    def test_trend_driver_by_migration(self):
        self.assertEqual(regime([cell(102.0, 5e6), cell(97.0, -4e6)], "MIGRATING_DOWN"), "TREND_DRIVER")

    def test_range_buffer_requires_bidirectional_walls(self):
        self.assertEqual(regime([cell(98.0, 5e6), cell(102.0, 5e6), cell(103.0, -2e6)]), "RANGE_BUFFER")
        # 只有上方正墙（天花板），即使 pos_share 高也不算 RANGE_BUFFER
        self.assertEqual(regime([cell(102.0, 10e6), cell(97.0, -2e6)]), "NEUTRAL")

    def test_neutral(self):
        self.assertEqual(regime([cell(102.0, 5e6), cell(97.0, -4e6)]), "NEUTRAL")

    def test_falls_back_to_all_cells_outside_band(self):
        r, share = compute_local_gamma_regime([cell(110.0, -5e6), cell(90.0, 1e6)], SPOT, G_MIN)
        self.assertEqual(r, "TREND_DRIVER")
        self.assertAlmostEqual(share, 1 / 6, places=4)


def score(**kw) -> tuple[float, bool, float]:
    args = dict(
        structure_type="SPRINGBOARD_STRUCTURE",
        local_gamma_regime="NEUTRAL",
        node_growth_state="FLAT",
        wall_migration="STABLE",
        reaction_trigger_type="TREND_PULLBACK_HOLD",
        same_sign_confluences=0,
        has_expiry_conflict=False,
        tick_ret_5m=0.0,
        ret_15m=0.0,
    )
    args.update(kw)
    return compute_structure_confirmation_score(**args)


class ConfirmationScoreTest(unittest.TestCase):
    def test_base_flat(self):
        self.assertEqual(score(), (62.0, True, 0.85))

    def test_node_growth_weights(self):
        self.assertEqual(score(node_growth_state="GROWING")[0], 70.0)
        self.assertEqual(score(node_growth_state="NEW")[0], 66.0)
        self.assertEqual(score(node_growth_state="DECAYING")[0], 56.0)
        s, ok, size = score(node_growth_state="DECAYING", structure_type="CLIFF_STRUCTURE",
                            reaction_trigger_type="REJECTION_DROP")
        self.assertEqual((s, ok, size), (42.0, False, 0.50))

    def test_regime_weights(self):
        self.assertEqual(score(local_gamma_regime="SPEED_BUMP_MUD")[0], 40.0)
        self.assertEqual(score(local_gamma_regime="DISORDERED_WHIPSAW")[0], 40.0)
        self.assertEqual(score(local_gamma_regime="RANGE_BUFFER")[0], 70.0)
        self.assertEqual(score(local_gamma_regime="TREND_DRIVER")[0], 72.0)  # TREND_PULLBACK_HOLD 对齐
        self.assertEqual(score(local_gamma_regime="TREND_DRIVER", reaction_trigger_type="REJECTION_BOUNCE")[0], 62.0)
        self.assertEqual(score(local_gamma_regime="RANGE_BUFFER", structure_type="SHORT_GAMMA_VACUUM_SQUEEZE",
                               reaction_trigger_type="EFFECTIVE_BREAKOUT")[0], 62.0)

    def test_wall_migration(self):
        self.assertEqual(score(wall_migration="MIGRATING_UP")[0], 72.0)
        self.assertEqual(score(wall_migration="MIGRATING_DOWN")[0], 50.0)
        self.assertEqual(score(structure_type="SHORT_GAMMA_AVALANCHE", wall_migration="MIGRATING_DOWN")[0], 72.0)
        self.assertEqual(score(structure_type="PINNING_MAGNET", wall_migration="MIGRATING_DOWN")[0], 62.0)

    def test_expiry_confluence_capped_and_conflict(self):
        self.assertEqual(score(same_sign_confluences=1)[0], 68.0)
        self.assertEqual(score(same_sign_confluences=5)[0], 74.0)
        self.assertEqual(score(has_expiry_conflict=True)[0], 48.0)

    def test_mtf_momentum(self):
        self.assertEqual(score(tick_ret_5m=0.0006, ret_15m=0.0016)[0], 70.0)
        self.assertEqual(score(tick_ret_5m=0.0005, ret_15m=0.0016)[0], 62.0)  # 严格大于
        self.assertEqual(score(structure_type="CLIFF_STRUCTURE", tick_ret_5m=-0.0006, ret_15m=-0.0016)[0], 70.0)

    def test_size_tiers_and_clip(self):
        self.assertEqual(score(node_growth_state="GROWING", same_sign_confluences=1), (76.0, True, 1.15))
        self.assertEqual(score(node_growth_state="NEW", local_gamma_regime="RANGE_BUFFER"), (74.0, True, 0.85))
        s, _, _ = score(node_growth_state="GROWING", local_gamma_regime="TREND_DRIVER", wall_migration="MIGRATING_UP",
                        same_sign_confluences=2, tick_ret_5m=0.001, ret_15m=0.002)
        self.assertEqual(s, 100.0)

    def test_spec_demo(self):
        s = compute_structure_confirmation_score(
            "CLIFF_STRUCTURE", "TREND_DRIVER", "GROWING", "MIGRATING_DOWN", "TREND_PULLBACK_REJECT",
            1, False, -0.0018, -0.0042)
        self.assertEqual(s, (100.0, True, 1.15))


class OptionRatchetTest(unittest.TestCase):
    def test_tiers(self):
        cases = {
            0.0: (-35.0, -999.0), 5.99: (-35.0, -999.0), 6.0: (-12.0, -999.0), 9.99: (-12.0, -999.0),
            10.0: (-5.0, 3.0), 15.0: (-5.0, 6.25), 20.0: (0.0, 9.5), 30.0: (0.0, 17.5),
            40.0: (0.0, 25.5), 60.0: (0.0, 42.7), 80.0: (0.0, 59.9), 100.0: (0.0, 77.9),
        }
        for m, expected in cases.items():
            with self.subTest(m=m):
                self.assertEqual(compute_continuous_option_ratchet(m), expected)

    def test_continuous_at_breakpoints(self):
        for m in (20.0, 40.0, 80.0):
            below = compute_continuous_option_ratchet(m - 1e-9)[1]
            self.assertAlmostEqual(below, compute_continuous_option_ratchet(m)[1], places=2)

    def test_opening_scalp_lock(self):
        self.assertEqual(compute_continuous_option_ratchet(21.0, is_opening_scalp=True), (0.0, 10.3))
        self.assertEqual(compute_continuous_option_ratchet(30.0, is_opening_scalp=True), (0.0, 25.5))


def make_pos(direction: str = "BULLISH", **kw) -> PositionState:
    if direction == "BULLISH":
        base = dict(entry_spot=100.0, initial_stop_loss_spot=98.0, stop_loss_spot=98.0, target_spot=110.0)
    else:
        base = dict(entry_spot=100.0, initial_stop_loss_spot=102.0, stop_loss_spot=102.0, target_spot=90.0)
    base.update(kw)
    return PositionState(position_id="T", ticker="AAA", direction=direction, entry_option_price=1.0, **base)


class SpotRatchetTest(unittest.TestCase):
    def check(self, direction: str, peak_spot: float, expected: dict[float, float]):
        for m, sl in expected.items():
            with self.subTest(direction=direction, m=m):
                pos = make_pos(direction, peak_spot=peak_spot)
                pos.peak_roi_pct = m
                self.assertAlmostEqual(update_spot_ratchet_stop(pos), sl, places=2)

    def test_bullish_tiers(self):
        self.check("BULLISH", 104.0, {5.0: 98.0, 10.0: 99.0, 15.0: 100.0, 25.0: 102.0, 40.0: 102.8, 60.0: 103.2})

    def test_bearish_tiers(self):
        self.check("BEARISH", 96.0, {5.0: 102.0, 10.0: 101.0, 15.0: 100.0, 25.0: 98.0, 40.0: 97.2, 60.0: 96.8})

    def test_never_loosens(self):
        pos = make_pos("BULLISH", peak_spot=104.0, stop_loss_spot=103.5)
        pos.peak_roi_pct = 25.0
        self.assertEqual(update_spot_ratchet_stop(pos), 103.5)

    def test_overnight_skips_ratchet(self):
        pos = make_pos("BULLISH", peak_spot=104.0, is_same_day_overnight=True)
        pos.peak_roi_pct = 60.0
        self.assertEqual(update_spot_ratchet_stop(pos), 98.0)


class ExitTickTest(unittest.TestCase):
    def test_eod_0dte_force_close(self):
        self.assertEqual(evaluate_exit_tick(make_pos(), 100.0, 1.0, 5.0, (15, 49))[0], "HOLD")
        self.assertEqual(evaluate_exit_tick(make_pos(), 100.0, 1.0, 5.0, (15, 50))[:2], ("CLOSE_ALL", "EOD_0DTE_CLOSE"))
        self.assertEqual(evaluate_exit_tick(make_pos(is_0dte=False), 100.0, 1.0, 5.0, (15, 55))[0], "HOLD")

    def test_stage0_0dte_8m(self):
        self.assertEqual(evaluate_exit_tick(make_pos(), 99.5, 0.88, 7.9)[0], "HOLD")
        self.assertEqual(evaluate_exit_tick(make_pos(), 99.5, 0.88, 8.0)[1], "DEAD_MOMENTUM_TRUNCATE_0DTE_8M")
        self.assertEqual(evaluate_exit_tick(make_pos(is_0dte=False), 99.5, 0.88, 8.0)[0], "HOLD")

    def test_stage1_dead_10m(self):
        self.assertEqual(evaluate_exit_tick(make_pos(is_0dte=False), 99.5, 0.80, 10.0)[1], "DEAD_MOMENTUM_TRUNCATE_10M")

    def test_stage2_theta_25m(self):
        self.assertEqual(evaluate_exit_tick(make_pos(is_0dte=False), 99.5, 0.85, 24.9)[0], "HOLD")
        self.assertEqual(evaluate_exit_tick(make_pos(is_0dte=False), 99.5, 0.85, 25.0)[1], "THETA_STAGNATION_TRUNCATE_25M")

    def test_initial_stop_loss(self):
        self.assertEqual(evaluate_exit_tick(make_pos(), 99.0, 0.65, 1.0)[1], "INITIAL_STOP_LOSS")

    def test_soft_ratchet_stop(self):
        pos = make_pos()
        self.assertEqual(evaluate_exit_tick(pos, 100.2, 1.07, 2.0)[0], "HOLD")
        self.assertEqual(pos.soft_stop_level_pct, -12.0)
        self.assertEqual(evaluate_exit_tick(pos, 99.5, 0.88, 3.0)[1], "RATCHET_SOFT_STOP")

    def test_trailing_stop(self):
        pos = make_pos()
        evaluate_exit_tick(pos, 102.0, 1.30, 5.0)
        self.assertEqual(pos.trailing_stop_level_pct, 17.5)
        self.assertEqual(pos.stop_loss_spot, 101.0)
        self.assertEqual(evaluate_exit_tick(pos, 101.5, 1.18, 6.0)[0], "HOLD")
        self.assertEqual(evaluate_exit_tick(pos, 101.5, 1.17, 7.0)[1], "TRAILING_STOP")

    def test_spot_stops_and_target(self):
        self.assertEqual(evaluate_exit_tick(make_pos(), 98.0, 0.90, 1.0)[1], "SPOT_STOP")
        pos = make_pos()
        evaluate_exit_tick(pos, 102.0, 1.30, 5.0)
        self.assertEqual(evaluate_exit_tick(pos, 100.9, 1.25, 6.0)[1], "SPOT_PROFIT_STOP")
        self.assertEqual(evaluate_exit_tick(make_pos(), 110.0, 1.50, 5.0)[1], "STRUCTURE_TARGET")
        self.assertEqual(evaluate_exit_tick(make_pos(is_0dte=False), 100.0, 1.0, 180.0)[1], "MAX_HOLD")

    def test_scale_out_once_then_hard_tp(self):
        pos = make_pos(target_spot=200.0)
        self.assertEqual(evaluate_exit_tick(pos, 105.0, 1.80, 5.0)[:2], ("SCALE_OUT_50", "SCALE_OUT_50"))
        self.assertEqual(evaluate_exit_tick(pos, 105.0, 1.80, 6.0)[0], "HOLD")
        self.assertEqual(evaluate_exit_tick(pos, 108.0, 3.50, 7.0)[1], "HARD_TP_CAP")
        single = make_pos(target_spot=200.0, quantity=1)
        self.assertEqual(evaluate_exit_tick(single, 105.0, 1.80, 5.0)[0], "HOLD")

    def test_overnight_gate(self):
        pos = make_pos(is_same_day_overnight=True, is_0dte=False)
        self.assertEqual(evaluate_exit_tick(pos, 101.0, 1.30, 30.0, (15, 55))[0], "HOLD")
        self.assertEqual(pos.trailing_stop_level_pct, -999.0)  # 当日豁免期权棘轮
        self.assertEqual(evaluate_exit_tick(pos, 100.5, 1.05, 31.0)[0], "HOLD")  # 回吐也不触发 trailing
        self.assertEqual(evaluate_exit_tick(pos, 102.0, 1.60, 32.0)[1], "SCALE_OUT_50_OVERNIGHT")
        self.assertEqual(evaluate_exit_tick(pos, 99.5, 0.65, 33.0)[1], "INITIAL_STOP_LOSS_OVERNIGHT")
        self.assertEqual(evaluate_exit_tick(pos, 97.9, 0.90, 34.0)[1], "SPOT_STOP_OVERNIGHT")

    def test_spec_demo(self):
        pos = PositionState(position_id="DEMO-01", ticker="GOOGL", direction="BEARISH", entry_spot=342.04,
                            entry_option_price=1.12, initial_stop_loss_spot=347.50, stop_loss_spot=347.50,
                            target_spot=320.00)
        self.assertEqual(evaluate_exit_tick(pos, 339.80, 3.15, 35.0)[:2], ("SCALE_OUT_50", "SCALE_OUT_50"))
        self.assertAlmostEqual(pos.peak_roi_pct, 181.25, places=2)
        self.assertAlmostEqual(pos.trailing_stop_level_pct, 151.03, places=2)
        self.assertEqual(pos.stop_loss_spot, 340.25)
        # 现货 340.38 >= 抬升后的现货止损 340.25，现货轨道先于期权 trailing 触发
        self.assertEqual(evaluate_exit_tick(pos, 340.38, 2.75, 53.5)[:2], ("CLOSE_ALL", "SPOT_PROFIT_STOP"))


class WeightsDefaultsTest(unittest.TestCase):
    def test_key_defaults(self):
        h, e = HeatmapWeights(), ExitWeights()
        self.assertEqual((h.base_confirmed_score, h.min_passing_score), (58.0, 55.0))
        self.assertEqual(e.eod_0dte_force_close_hhmm, (15, 50))
        self.assertEqual((e.initial_stop_loss_roi_pct, e.hard_take_profit_roi_pct), (-35.0, 250.0))


if __name__ == "__main__":
    unittest.main()
