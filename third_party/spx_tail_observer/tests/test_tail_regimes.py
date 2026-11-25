"""Synthetic source frames and order timestamps, no network or credentials."""
from datetime import datetime
import unittest

from observer.clock import NY
from observer.config import Config
from observer.nightwatch import NightwatchEngine, symbol


T = datetime(2026,11,17,15,55,tzinfo=NY).timestamp()


def iso(t):
    return datetime.fromtimestamp(t,NY).isoformat()


def index_frame(t, value, **changes):
    return {"status": "OK", "results": [{"ticker": "I:SPX", "timeframe": "REAL-TIME", "market_status": "regular_trading",
            "last_updated": round(t*1e9), "value": value, **changes}]}


def run_engine(now=T):
    e=NightwatchEngine(Config(source="nightwatch"))
    e.started_at=now-900
    e.roll_day(now)
    return e


def tick(e,t,price,*,node=7000,age=1,total=2000):
    e.apply_gex({"session_date_et":e.day,"snapshot_at":iso(t),"spot_usd":price,"state":"fresh",
        "strikes":[{"strike_usd":node,"net_gex_usd":1000}],"summary":{"total_gex_usd":total}},
        {"data_freshness_seconds":age},t)


def quote(e,t,ask=4.6):
    if not e.strikes:
        e.apply_chain([{"contract_symbol":symbol((k,r),e.day)}
                       for k in range(6970,7031,5) for r in "CP"],t)
    e.apply_quotes({"quotes":[{"contract_symbol":symbol(k,e.day), "bid_usd":.65, "ask_usd":ask,
                       "quoted_at":iso(t)} for k in e.focus]},t)


def proxy_quotes(e,t,level,*,mode=None):
    if not e.strikes:
        e.apply_chain([{"contract_symbol":symbol((k,r),e.day)}
                       for k in range(6970,7031,5) for r in "CP"],t)
    rows=[]
    strikes=sorted({k for k,r in e.focus})
    for k,r in sorted(e.focus):
        if mode=="missing" and r=="P" and k in strikes[:3]:
            continue
        mid=5+max(0,(level-k)*(1 if r=="C" else -1))
        if mode=="disagree" and r=="C" and k==strikes[0]:
            mid+=4
        at=t-8 if mode=="unsynced" and r=="P" else t
        width=1 if mode=="wide" else .1
        rows.append({"contract_symbol":symbol((k,r),e.day),"bid_usd":mid-width,"ask_usd":mid+width,
                     "quoted_at":iso(at)})
    e.apply_quotes({"quotes":rows},t)


def order(e,t,k,r,qty,tag):
    e.apply_flow([{"kind":"order","aggregate_id":tag,"contract_symbol":symbol((k,r),e.day),
        "occurred_at":iso(t),"bucket_end_at":iso(t),"contracts":qty,"premium_usd":qty*100,
        "nbbo_bid_usd":.9,"nbbo_ask_usd":1.}],t,include_backfill=True)


def finish(e,t):
    quote(e,t)
    e.apply_flow([],t)
    e.flow_covered_at=t
    return e.snapshot(t)["playbook"]


class TailRegimeTest(unittest.TestCase):
    def test_near_flow_separates_buy_sell_unknown_and_uses_the_live_window(self):
        e=run_engine(T)
        tick(e,T,7000,node=7000)
        quote(e,T)

        def row(tag, k, right, offset, qty, price, end=None):
            return {"kind":"order","aggregate_id":tag,"contract_symbol":symbol((k,right),e.day),
                    "occurred_at":iso(T+offset),"bucket_end_at":iso(T+offset if end is None else T+end),
                    "contracts":qty,"premium_usd":qty*price*100,"nbbo_bid_usd":.8,"nbbo_ask_usd":1.}

        e.apply_flow([row('buy',7010,'C',-10,60,1.),row('sell',7005,'C',-4,20,.8),
                      row('unknown',7005,'C',-3,10,.9),row('put',6995,'P',-2,10,1.),
                      row('far',7015,'C',-1,200,1.),row('old',7000,'C',-301,200,1.,end=-1)],
                     T,include_backfill=True)
        e.flow_gap=True
        flow=e.snapshot(T)["near_flow"]
        self.assertEqual(flow["state"],"PARTIAL")
        self.assertEqual(flow["sides"]["C"],{"volume":90.,"buy":6000.,"sell":1600.,"unknown":900.})
        self.assertEqual(flow["sides"]["P"],{"volume":10.,"buy":1000.,"sell":0.,"unknown":0.})
        e.flow_gap=False
        e.flow_covered_at=T
        self.assertEqual(e.snapshot(T)["near_flow"]["state"],"RECENT")
        self.assertEqual(e.snapshot(T+46)["near_flow"]["state"],"WAIT")
        self.assertEqual(e.snapshot(T+400)["near_flow"]["state"],"OUTSIDE")

    def test_feed_keeps_first_trigger_context_when_a_late_order_or_update_arrives(self):
        e=run_engine(T)
        for i,off in enumerate(range(1,10)):
            tick(e,T-121+i*15,7000+off,node=6960)
        e.native["summary"].update(call_wall_strike_usd=7010.,put_wall_strike_usd=6995.)
        quote(e,T-1)
        e.apply_flow([],T-1)
        e.flow_gap=True
        e.tail_journal.observe(e.snapshot(T-1))

        tick(e,T+3,7020,node=7025)
        quote(e,T+3)
        e.apply_flow([],T+3)
        e.flow_gap=False
        e.flow_covered_at=T+3
        e.tail_journal.observe(e.snapshot(T+3))

        late={"kind":"order","aggregate_id":"late","contract_symbol":symbol((7025,"C"),e.day),
              "occurred_at":iso(T),"bucket_end_at":iso(T),"contracts":70,"premium_usd":7000,
              "nbbo_bid_usd":.90,"nbbo_ask_usd":1.0}
        e.apply_flow([late],T+3,include_backfill=True)
        signal=e.snapshot(T+3)["signals"][0]
        context=signal["trigger_context"]
        self.assertEqual(context["at"],T-1)
        self.assertEqual(context["level"]["strike"],7010.)
        self.assertIn("Call Wall",context["level"]["roles"])
        self.assertEqual((context["stage"],context["side"]),("PRICE","CALL"))
        self.assertIn("成交未确认",context["missing"])
        self.assertEqual([v["name"] for v in context["support"]],["price_path"])
        self.assertIsNone(e.tail_journal.for_order(T-2,"C"))  # Neither recorded assessment existed yet.
        other=e.tail_journal.for_order(T,"P")
        self.assertEqual(other["stage"],"PRICE")
        self.assertFalse(other["support"])
        # Group updates change live quotes and size but do not replace the first context.
        order(e,T+6,7025,"C",55,"later")
        follow=e.snapshot(T+6)["signals"][0]
        self.assertEqual(follow["updates"],2)
        self.assertEqual(follow["trigger_context"],context)

    def test_gex_level_cannot_be_reused_past_its_freshness_or_trigger_lookup_window(self):
        e=run_engine(T)
        tick(e,T-44,7001,node=7000)
        e.tail_journal.observe(e.snapshot(T-1))
        self.assertEqual(e.tail_journal.for_order(T,"P")["level"]["strike"],7000.)
        self.assertIsNone(e.tail_journal.for_order(T+2,"P")["level"])
        self.assertIsNone(e.tail_journal.for_order(T+15,"P"))

    def test_intraday_journal_survives_close_and_new_snapshots_but_resets_next_et_day(self):
        e=run_engine(T)
        for i,off in enumerate(range(1,10)):
            tick(e,T-120+i*15,7000+off,node=6960)
        e.tail_journal.observe(e.snapshot(T))
        quote(e,T+1)
        e.apply_flow([],T+1)
        e.flow_gap=True
        e.tail_journal.observe(e.snapshot(T+1))
        e.tail_journal.observe(e.snapshot(T+2))
        e.flow_gap=False
        e.flow_covered_at=T+3
        e.tail_journal.observe(e.snapshot(T+3))
        intraday=e.snapshot(T+3)["tail_history"]
        self.assertEqual([n["stage"] for n in intraday],["WAIT","PRICE","DIRECTION"])
        self.assertEqual([n["at"] for n in intraday],[T,T+1,T+3])
        self.assertEqual(intraday[-1]["side"],"CALL")

        closed=e.snapshot(T+400)
        e.tail_journal.observe(closed)
        self.assertFalse(closed["market_open"])
        self.assertEqual(closed["playbook"]["direction"],"STAND_BY")
        self.assertEqual(e.snapshot(T+401)["tail_history"],intraday)
        self.assertEqual(e.snapshot(T+86400)["tail_history"],[])

    def test_continuation_both_ways_without_a_retest_or_a_node_crossing(self):
        for sign,d in [(1,"CALL"),(-1,"PUT")]:
            with self.subTest(d=d):
                e=run_engine()
                for i in range(9):
                    tick(e,T-120+15*i,7001+sign*i,node=6960)
                book=finish(e,T)
                self.assertEqual((book["direction"],book["regime"]["state"]), (d,"CONTINUATION"))
                self.assertEqual(book["reference"]["price_state"],"ABOVE_LIMIT")
                self.assertEqual(book["volume"]["state"],"WAIT")  # not required for direction

    def test_node_break_holds_both_ways_and_same_price_points_are_not_stacked(self):
        for sign,d in [(1,"CALL"),(-1,"PUT")]:
            e=run_engine()
            for i,off in enumerate([-2.0,-2.0,-2.0,1.55,1.60,1.55,1.60,1.60,1.65]):
                tick(e,T-120+i*15,7000+sign*off)
            book=finish(e,T)
            self.assertEqual((book["direction"],book["regime"]["state"]), (d,"CONTINUATION"))
            self.assertEqual(book["pattern"]["node"],7000)
            votes={v["name"]:v["points"] for v in book["evidence"]}
            self.assertEqual(votes["key_level"]+votes["price_path"],25)

    def test_v_a_require_a_return_and_follow_through_not_a_single_wick(self):
        for sign,d,label in [(1,"CALL","V形反转"),(-1,"PUT","A形反转")]:
            e=run_engine()
            for i,p in enumerate([5,4,2,0,-2,-4,-2,.5,2]):
                tick(e,T-120+i*15,7000+sign*p,node=6800)
            b=finish(e,T)
            self.assertEqual((b["direction"],b["regime"]["label"]), (d,label))
            e=run_engine()
            for i,p in enumerate([5,4,2,0,-2,-3,-4,-4,1]):
                tick(e,T-120+i*15,7000+sign*p,node=6800)
            self.assertNotEqual(finish(e,T)["regime"]["state"],"REVERSAL")

    def test_pin_overrides_an_individual_order_and_releases_on_escape_or_stale(self):
        e=run_engine()
        for i in range(13):
            tick(e,T-180+i*15,7000+(.85 if i%2 else -.85))
        order(e,T,7020,"C",240,"far-call")
        b=finish(e,T)
        self.assertEqual((b["regime"]["state"],b["pin"]["strike"]),("PIN",7000))
        self.assertIsNone(b["reference"])
        self.assertTrue(any(s["right"]=="C" and s["strike"]==7020 for s in e.snapshot(T)["signals"]))
        e.flow_gap=True
        self.assertEqual(e.snapshot(T)["playbook"]["regime"]["state"],"STAND_BY")
        e.flow_gap=False
        tick(e,T+1,7006)
        self.assertNotEqual(finish(e,T+1)["regime"]["state"],"PIN")
        self.assertEqual(e.snapshot(T+50)["playbook"]["regime"]["state"],"STAND_BY")

    def test_duplicate_frames_moving_nodes_and_stale_content_are_not_pin_evidence(self):
        e=run_engine()
        for i in range(13):
            tick(e,T-180+i*15,7000+(.8 if i%2 else -.8),node=7005 if i==6 else 7000)
        self.assertNotEqual(finish(e,T)["regime"]["state"],"PIN")
        tick(e,T+1,7000,age=80)
        for i in range(20):
            tick(e,T+2,7000+(.8 if i%2 else -.8))
        b=finish(e,T+2)
        self.assertNotEqual(b["regime"]["state"],"PIN")
        self.assertIsNone(b["pattern"])

    def test_volume_uses_only_complete_windows_and_cannot_elect_a_direction(self):
        e=run_engine()
        tick(e,T,7000)
        # Balance C/P in every window. A large current, unfinished minute is excluded.
        for i,n in enumerate([20,30,25,75]):
            for r in "CP":
                order(e,T-235+i*60,7000,r,n,r+str(i))
        for r in "CP":
            order(e,T+1,7000,r,850,"unfinished"+r)
        b=finish(e,T+2)
        self.assertEqual((b["volume"]["state"],b["volume"]["ratio"],b["volume"]["contracts"]),
                         ("EXPANDING",3.,150))
        self.assertEqual((b["regime"]["state"],b["direction"]),("NEUTRAL","NEUTRAL"))
        self.assertIsNone(b["reference"])
        e.flow_gap=True
        self.assertEqual(e.snapshot(T+2)["playbook"]["volume"]["state"],"WAIT")
        e.flow_gap=False
        e.started_at=T-205
        self.assertEqual(e.snapshot(T+2)["playbook"]["volume"]["state"],"WAIT")
        e.started_at=T-900
        e.flow_covered_at=T-1
        self.assertEqual(e.snapshot(T+2)["playbook"]["volume"]["state"],"WAIT")

    def test_one_large_updated_order_remains_an_anomaly_without_voting_direction(self):
        e=run_engine()
        tick(e,T,7000)
        order(e,T,7020,"C",240,"same-order")
        order(e,T+1,7020,"C",350,"same-order")
        b=finish(e,T+1)
        self.assertEqual(b["direction"],"NEUTRAL")
        self.assertTrue(any(s["strike"]==7020 for s in e.snapshot(T+1)["signals"]))
        self.assertFalse(any(v["points"] for v in b["evidence"] if v["name"]=="strike_bias"))

    def test_expansion_confirms_trend_without_flipping_and_actual_put_stays_separate(self):
        e=run_engine()
        for i in range(9):
            tick(e,T-120+i*15,7001+i,node=6960)
        for i,n in enumerate([25,25,25,65]):
            for r in "CP":
                order(e,T-236+i*60,7005,r,n*2,r+str(i))
        order(e,T,6995,"P",70,"actual-put")
        b=finish(e,T)
        self.assertEqual((b["regime"]["state"],b["volume"]["state"]),("CONTINUATION","EXPANDING"))
        self.assertEqual(b["direction"],"CALL")
        self.assertEqual((b["reference"]["right"],b["reference"]["strike"]),("C",7010))
        self.assertTrue(any(s["right"]=="P" and s["strike"]==6995 for s in e.snapshot(T)["signals"]))

    def test_recent_flow_gap_exposes_only_the_price_shape_then_recomputes_combined_direction(self):
        for offsets, side, label in [
            (range(1,10), "CALL", "顺势延续"),
            (range(5,-4,-1), "PUT", "顺势延续"),
            ([5,4,2,0,-2,-4,-2,.5,2], "CALL", "V形反转"),
            ([-5,-4,-2,0,2,4,2,-.5,-2], "PUT", "A形反转"),
        ]:
            with self.subTest(side=side,label=label):
                e=run_engine()
                for i,off in enumerate(offsets):
                    tick(e,T-120+i*15,7000+off,node=6950)
                quote(e,T)
                opposite="P" if side=="CALL" else "C"
                levels=[6985, 6990] if opposite=="P" else [7015, 7020]
                for i,k in enumerate(levels):
                    order(e,T,k,opposite,150,f"opposite-order-{i}")
                e.flow_gap=True
                e.flow_pending_pages=9
                s=e.snapshot(T)
                b=s["playbook"]
                self.assertFalse(s["healthy"])
                self.assertEqual(b["direction"],"STAND_BY")
                self.assertEqual(b["price_preview"]["side"],side)
                self.assertEqual(b["price_preview"]["label"],label)
                self.assertIsNone(b["reference"])
                self.assertEqual(b["volume"]["state"],"WAIT")
                self.assertEqual(s["signals"][0]["right"],opposite)
                e.flow_gap=False
                e.flow_pending_pages=0
                e.flow_covered_at=T
                b=e.snapshot(T)["playbook"]
                self.assertIsNone(b["price_preview"])
                self.assertEqual(b["direction"],"NEUTRAL") # 25 vs 40, below the 21-point margin

    def test_price_preview_withdraws_when_inputs_fail_or_no_shape_qualifies(self):
        for condition in ("outside", "no-shape", "GEX-stale", "quotes-stale", "flow-stale", "disconnected"):
            with self.subTest(condition=condition):
                t=T-26*60 if condition=="outside" else T
                e=run_engine(t)
                for i in range(9):
                    tick(e,t-120+i*15,7000 if condition=="no-shape" else 7001+i,node=6950)
                quote(e,t-46 if condition=="quotes-stale" else t)
                e.apply_flow([],t-46 if condition=="flow-stale" else t)
                e.flow_gap=True
                if condition=="GEX-stale":
                    tick(e,t,7009,node=6950,age=46)
                if condition=="disconnected":
                    e.connected=False
                b=e.snapshot(t)["playbook"]
                self.assertEqual(b["direction"],"STAND_BY")
                self.assertIsNone(b["price_preview"])

    def test_price_preview_keeps_waiting_when_a_reversal_shape_overlaps_a_pin(self):
        e=run_engine()
        offsets=[-.8,.8,-.8,.8,-.8,.8,1.6,1.1,0,-1.6,-.7,0,1.6]
        for i,off in enumerate(offsets):
            tick(e,T-180+i*15,7000+off)
        quote(e,T)
        e.apply_flow([],T)
        e.flow_gap=True
        b=e.snapshot(T)["playbook"]
        self.assertEqual(b["pattern"]["kind"],"REVERSAL")
        self.assertIsNone(b["price_preview"])
        e.flow_gap=False
        e.flow_covered_at=T
        self.assertEqual(e.snapshot(T)["playbook"]["regime"]["state"],"PIN")

    def test_live_index_preserves_only_pure_price_shape_when_gex_expires(self):
        for sign, side, label, offsets in [
            (1,"CALL","V形反转",[5,4,2,0,-2,-4,-2,.5,2]),
            (-1,"PUT","A形反转",[5,4,2,0,-2,-4,-2,.5,2]),
            (1,"CALL","顺势延续",range(1,10)),
            (-1,"PUT","顺势延续",range(1,10)),
        ]:
            with self.subTest(side=side,label=label):
                e=run_engine()
                for i,off in enumerate(offsets):
                    t=T-120+i*15
                    self.assertTrue(e.apply_index_spot(index_frame(t,7000+sign*off),t))
                    tick(e,t,7000,node=7000,age=180)
                quote(e,T)
                e.flow_gap=True
                e.flow_covered_at=T-15
                b=e.snapshot(T)["playbook"]
                self.assertEqual((b["direction"],b["pin"]["state"]),("STAND_BY","WAIT"))
                self.assertIsNone(b["reference"])
                self.assertEqual(b["price_preview"]["source"],"massive")
                self.assertEqual((b["price_preview"]["side"],b["price_preview"]["label"]),(side,label))
                self.assertEqual(b["price_preview"]["unconfirmed"],["GEX 未确认","成交未确认"])
                self.assertIn("180 秒",b["data_status"]["gex"])
                self.assertIn("15:54:45",b["data_status"]["flow"])
                self.assertIsNone(e.snapshot(T+46)["playbook"]["price_preview"])

    def test_delayed_wrong_or_repeated_index_frames_do_not_make_a_price_path(self):
        e=run_engine()
        for i in range(8):
            self.assertTrue(e.apply_index_spot(index_frame(T-10,7000+i),T))
        self.assertEqual(len(e.index_samples),1)
        for changes in ({"timeframe":"DELAYED"},{"ticker":"I:DJI"},{"market_status":"closed"},
                        {"last_updated":None},{"last_updated":round((T-46)*1e9)},
                        {"last_updated":round((T+1)*1e9)},{"value":True}):
            with self.subTest(changes=changes):
                e.apply_index_spot(index_frame(T,7000),T)
                row=index_frame(T,7000);row["results"][0].update(changes)
                self.assertFalse(e.apply_index_spot(row,T))
                self.assertFalse(e.index_samples)
                self.assertIsNone(e.snapshot(T)["playbook"]["price_preview"])

    def test_a_node_break_alone_does_not_trigger_without_verified_gex(self):
        e=run_engine()
        for i,off in enumerate([-2,-2,-2,1.55,1.60,1.55,1.60,1.60,1.65]):
            t=T-120+i*15
            e.apply_index_spot(index_frame(t,7000+off),t)
            tick(e,t,7000+off,node=7000,age=80)
        self.assertIsNone(e.snapshot(T)["playbook"]["price_preview"])
        self.assertIn("尚未形成",e.snapshot(T)["playbook"]["data_status"]["price"])

    def test_nightwatch_quotes_alone_can_show_price_structure_during_gex_and_flow_gaps(self):
        for sign,side,offsets,label in [
            (1,"CALL",[5,4,2,0,-2,-4,-2,.5,2],"V形反转"),
            (-1,"PUT",[5,4,2,0,-2,-4,-2,.5,2],"A形反转"),
            (1,"CALL",range(1,10),"顺势延续"),
            (-1,"PUT",range(1,10),"顺势延续"),
        ]:
            with self.subTest(side=side,label=label):
                e=run_engine()
                for i,off in enumerate(offsets):
                    t=T-120+i*15
                    tick(e,t,7000,node=7000,age=125)
                    proxy_quotes(e,t,7000+sign*off)
                e.flow_gap=True
                b=e.snapshot(T)["playbook"]
                p=b["price_preview"]
                self.assertEqual((p["source"],p["side"],p["label"]),("parity",side,label))
                self.assertEqual(p["unconfirmed"],["GEX 未确认","成交未确认"])
                self.assertIn("期权隐含价格",b["data_status"]["price"])
                self.assertEqual((b["direction"],b["pin"]["state"],b["reference"]),("STAND_BY","WAIT",None))
                self.assertIsNone(e.snapshot(T+21)["playbook"]["price_preview"])

    def test_quote_proxy_withdraws_on_missing_async_wide_or_disagreeing_quotes(self):
        for mode in ("missing","unsynced","wide","disagree"):
            with self.subTest(mode=mode):
                e=run_engine()
                for i in range(9):
                    t=T-120+i*15
                    tick(e,t,7000,age=80)
                    proxy_quotes(e,t,7000+i)
                self.assertEqual(e.snapshot(T)["playbook"]["price_preview"]["source"],"parity")
                proxy_quotes(e,T+1,7008,mode=mode)
                self.assertIsNone(e.snapshot(T+1)["playbook"]["price_preview"])

    def test_repeated_unchanged_quote_time_does_not_build_the_proxy_path(self):
        e=run_engine()
        tick(e,T,7000,age=80)
        for i in range(9):
            proxy_quotes(e,T,7000+i)
        self.assertEqual(len(e.parity_samples),1)
        self.assertIsNone(e.snapshot(T)["playbook"]["price_preview"])

    def test_proxy_watch_can_lead_a_v_or_a_but_does_not_replace_the_standard_window(self):
        for sign,side,label in ((1,"CALL","V形反转"),(-1,"PUT","A形反转")):
            with self.subTest(side=side):
                e=run_engine()
                for i,off in enumerate([5,2,-4,-3,-1,2,2.5,3,3.5]):
                    t=T-80+i*10
                    tick(e,t,7000,age=120)
                    proxy_quotes(e,t,7000+sign*off)
                    s=e.snapshot(t)
                    b=s["playbook"]
                    if i<5:
                        self.assertIsNone(b["price_preview"])
                    elif i in (5,8):
                        p=b["price_preview"]
                        self.assertEqual((p["side"],p["label"],p["tier"]),(side,label,"watch" if i==5 else "preview"))
                        self.assertEqual(b["direction"],"STAND_BY")
                        self.assertIsNone(b["reference"])
                        self.assertEqual(s["candidate_spot"]["source"],"parity")

    def test_watch_withdraws_when_the_full_window_has_no_pattern(self):
        e=run_engine()
        for i,off in enumerate([0,1,2,3,4,5,3,1,1]):
            t=T-80+i*10
            tick(e,t,7000,age=120)
            proxy_quotes(e,t,7000+off)
            b=e.snapshot(t)["playbook"]
            if i==5:
                self.assertEqual(b["price_preview"]["tier"],"watch")
        self.assertIsNone(b["price_preview"])
