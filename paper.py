#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
幸存者 · 模拟盘 v2（真实保证金模式）
------------------------------------
改动要点：
  1) 每笔开仓按「当前权益 × 15%」占用保证金，并检查可用资金
  2) 保证金占用超过权益 85% 时不再开新仓（真实账户开不出）
  3) 逐仓模式：单笔最大亏损 = 该笔保证金，不会亏到别的仓位
  4) 强平：逆向波动超过 (1/杠杆 - 0.5%) 时按强平价平仓
  5) 四个账户杠杆不同，其余规则一致，用来对比杠杆差异

账户：
  A  固定 8 倍
  B  顺势 8 倍 / 逆势 4 倍
  C  固定 5 倍
  D  固定 5 倍 + 放量过滤

用法：
  python paper.py --status / --once / --loop 60 / --reset
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import scanner

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, ".paper_state.json")
LOG = os.path.join(HERE, "paper_log.csv")
BJ = timezone(timedelta(hours=8))
FEE = 0.0005
START_CAP = 10000.0
SCHEMA = 2

C_STOP, C_TP = 0.018, 0.040          # 止损 / 止盈
MARGIN_PCT = 0.15                     # 单笔保证金 = 权益 × 15%
MAX_USE = 0.85                        # 保证金占用上限 85%
D_VOL_DEFAULT = 1.2
ACCOUNTS = ("A", "B", "C", "D")
SCN = {"SHORT": "空", "LONG": "多"}
LEV = {"A": 8.0, "B": 8.0, "C": 5.0, "D": 5.0}
DESC = {"A": "固定8倍", "B": "顺势8/逆势4", "C": "固定5倍", "D": "5倍+放量过滤"}

CRYPTO_OK = {"BTC", "ETH", "ZEC", "SOL", "XRP", "DOGE", "BNB", "LTC", "BCH", "ADA",
             "AVAX", "LINK", "TRX", "DOT", "SUI", "APT", "ARB", "OP", "TON", "NEAR"}


def is_crypto(inst):
    return inst.split("-")[0].upper() in CRYPTO_OK


def d_vol_mult():
    try:
        with open(os.path.join(HERE, "config.json"), encoding="utf-8-sig") as f:
            return float(json.load(f).get("d_vol_mult", D_VOL_DEFAULT))
    except Exception:
        return D_VOL_DEFAULT


def vol_ratio(inst):
    try:
        cs = scanner.candles(inst, "1m", 40, ttl=45)
    except Exception:
        return 0.0
    closed = cs[:-1]
    if len(closed) < 22:
        return 0.0
    base = sum(c["v"] for c in closed[-21:-1]) / 20
    return (closed[-1]["v"] / base) if base > 0 else 0.0


def notify(title, body):
    try:
        scanner.bark(title, body)
    except Exception as e:
        print(f"  推送失败: {e}")


def _new_acc():
    return {"cap": START_CAP, "used": 0.0, "pos": [], "wins": 0, "closed": 0,
            "peak": START_CAP, "mdd": 0.0, "r_sum": 0.0}


def save_state(s):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)


def load_state():
    s = {}
    if os.path.exists(STATE):
        try:
            with open(STATE, encoding="utf-8-sig") as f:
                s = json.load(f)
        except Exception:
            s = {}
    if s.get("_v") != SCHEMA:          # 算法升级 → 全部重置，避免新旧混算
        s = {"_v": SCHEMA}
        for k in ACCOUNTS:
            s[k] = _new_acc()
        save_state(s)
        print("[paper] 检测到算法升级，四个账户重置为 10,000")
    for k in ACCOUNTS:
        s.setdefault(k, _new_acc())
        a = s[k]
        if "r_sum" not in a:
            # 历史数据按标准 R 反推：止盈 +2.22R，止损 -1R
            a["r_sum"] = a.get("wins", 0) * (C_TP / C_STOP) - (a.get("closed", 0) - a.get("wins", 0))
    return s


def price(inst):
    return float(scanner.api("/api/v5/market/ticker", instId=inst)[0]["last"])


def unrealized(acc, pxmap):
    total = 0.0
    for p in acc["pos"]:
        px = pxmap.get(p["inst"])
        if px is None:
            continue
        move = (p["entry"] - px) / p["entry"] if p["side"] == "SHORT" else (px - p["entry"]) / p["entry"]
        pnl = p["margin"] * p["lev"] * move - p["margin"] * p["lev"] * FEE * 2
        total += max(pnl, -p["margin"])          # 逐仓：最多亏掉保证金
    return total


def close_position(acc, p, px, why):
    move = (p["entry"] - px) / p["entry"] if p["side"] == "SHORT" else (px - p["entry"]) / p["entry"]
    notional = p["margin"] * p["lev"]
    pnl = notional * move - notional * FEE * 2
    pnl = max(pnl, -p["margin"])                 # 逐仓，最多亏完保证金
    r_mult = move / C_STOP                       # 以止损幅度为 1R
    hold = (time.time() - p.get("open_ts", time.time())) / 3600
    acc["cap"] += pnl
    acc["used"] = max(0.0, acc["used"] - p["margin"])
    acc["closed"] += 1
    acc["r_sum"] = acc.get("r_sum", 0.0) + r_mult
    if pnl > 0:
        acc["wins"] += 1
    acc["peak"] = max(acc["peak"], acc["cap"])
    acc["mdd"] = max(acc["mdd"], 1 - acc["cap"] / acc["peak"]) if acc["peak"] > 0 else 1
    acc["pos"] = [x for x in acc["pos"] if x is not p]
    with open(LOG, "a", encoding="utf-8-sig") as f:
        f.write(f"{datetime.now(BJ):%Y-%m-%d %H:%M:%S},{p.get('acct','')},{p['inst']},{p['side']},"
                f"{p['lev']:.2f},{p['entry']:.2f},{px:.2f},{pnl:.2f},{acc['cap']:.2f},{why}\n")
    return pnl, move, r_mult, hold


def close_body(acc, p, fill, why, pnl, move, r_mult, hold):
    """账户平仓推送正文：入场/出场、保证金、R、持有时间、本源累计"""
    icon = "✅" if pnl > 0 else "❌"
    closed, wins = acc["closed"], acc["wins"]
    losses = closed - wins
    wr = (wins / closed * 100) if closed else 0.0
    return (f"{why} {icon}  入场 {p['entry']:,.2f} → 出场 {fill:,.2f}（{move*100:+.2f}%）\n"
            f"保证金 {p['margin']:,.0f}U @{p['lev']:.0f}x  ｜  {r_mult:+.2f}R  ｜  持有 {hold:.1f} 小时\n"
            f"本源累计：{closed} 笔 ｜ 胜 {wins} 负 {losses} ｜ 胜率 {wr:.1f}% ｜ {acc.get('r_sum', 0):+.1f}R\n"
            f"本金 {acc['cap']:,.0f}U（{(acc['cap']/START_CAP-1)*100:+.1f}%）")


def status(s):
    pxmap = {}
    for k in ACCOUNTS:
        for p in s[k]["pos"]:
            if p["inst"] not in pxmap:
                try:
                    pxmap[p["inst"]] = price(p["inst"])
                except Exception:
                    pxmap[p["inst"]] = None
    print(f"\n{'账户':<4}{'本金':>10}{'浮盈':>9}{'权益':>10}{'已用保证金':>11}{'可用':>9}{'持仓':>5}{'回撤':>7}")
    for k in ACCOUNTS:
        a = s[k]
        u = unrealized(a, pxmap)
        eq = a["cap"] + u
        free = eq - a["used"]
        print(f"{k:<4}{a['cap']:>10,.0f}{u:>+9,.0f}{eq:>10,.0f}{a['used']:>11,.0f}{free:>9,.0f}"
              f"{len(a['pos']):>5}{a['mdd']*100:>6.1f}%")
    for k in ACCOUNTS:
        for p in s[k]["pos"]:
            print(f"  {k} {p['inst'].split('-')[0]:4s} {p['side']:5s} {p['lev']:.1f}x "
                  f"入场{p['entry']:,.2f} 止损{p['stop']:,.2f} 止盈{p['tp']:,.2f} 保证金{p['margin']:,.0f}")


def scan(s):
    mult = d_vol_mult()
    for inst in scanner.CFG["insts"]:
        if not is_crypto(inst):
            continue
        px = price(inst)
        pxmap = {inst: px}

        for k in ACCOUNTS:
            for p in list(s[k]["pos"]):
                if p["inst"] != inst:
                    continue
                liq_px = (p["entry"] * (1 + 1 / p["lev"] - 0.005) if p["side"] == "SHORT"
                          else p["entry"] * (1 - 1 / p["lev"] + 0.005))
                hit_liq = (px >= liq_px) if p["side"] == "SHORT" else (px <= liq_px)
                hit_stop = (px >= p["stop"]) if p["side"] == "SHORT" else (px <= p["stop"])
                hit_tp = (px <= p["tp"]) if p["side"] == "SHORT" else (px >= p["tp"])
                if hit_liq:
                    pnl, mv, rr, hold = close_position(s[k], p, liq_px, "强平")
                    print(f"  [{k}] 强平 {inst.split('-')[0]} {p['side']} @{liq_px:,.2f} {pnl:+,.0f}U")
                    if k in ("C", "D"):
                        notify(f"{k}强平-{inst.split('-')[0]} {SCN.get(p['side'], p['side'])} {pnl:+,.0f}U",
                               close_body(s[k], p, liq_px, "强平", pnl, mv, rr, hold))
                elif hit_stop or hit_tp:
                    if hit_stop:
                        fill = p["stop"] * (1 + 0.0005) if p["side"] == "SHORT" else p["stop"] * (1 - 0.0005)
                        why = "止损"
                    else:
                        fill = p["tp"] * (1 - 0.0005) if p["side"] == "SHORT" else p["tp"] * (1 + 0.0005)
                        why = "止盈"
                    pnl, mv, rr, hold = close_position(s[k], p, fill, why)
                    print(f"  [{k}] 平仓 {inst.split('-')[0]} {p['side']} @{fill:,.2f} {why} "
                          f"{pnl:+,.0f}U → 本金 {s[k]['cap']:,.0f}U")
                    if k in ("C", "D"):
                        notify(f"{k}平仓-{inst.split('-')[0]} {SCN.get(p['side'], p['side'])} {pnl:+,.0f}U",
                               close_body(s[k], p, fill, why, pnl, mv, rr, hold))

        try:
            r = scanner.analyse(inst)
        except Exception:
            continue
        plan = r["plan"]
        if not plan:
            continue
        side = plan["side"]
        up4 = r["m"]["4H"]["trend"] == "UP"
        aligned4 = (side == "LONG" and up4) or (side == "SHORT" and not up4)
        up1d = r["m"]["1D"]["trend"] == "UP"
        aligned1d = (side == "LONG" and up1d) or (side == "SHORT" and not up1d)
        vr = None
        opened = []

        for k in ACCOUNTS:
            if any(x["inst"] == inst for x in s[k]["pos"]):
                continue
            if k in ("C", "D") and not aligned4:
                continue
            if k == "D":
                if vr is None:
                    vr = vol_ratio(inst)
                if vr < mult:
                    print(f"  [D] 跳过 {inst.split('-')[0]}：放量不足 {vr:.2f}x < {mult}x")
                    continue

            a = s[k]
            lev = LEV[k]
            if k == "B" and not aligned1d:
                lev = 4.0
            eq = a["cap"] + unrealized(a, pxmap)
            margin = eq * MARGIN_PCT
            if a["used"] + margin > eq * MAX_USE:
                print(f"  [{k}] 保证金不足（已用 {a['used']:,.0f} / 上限 {eq*MAX_USE:,.0f}），跳过")
                continue

            entry = px
            stop = entry * (1 + C_STOP) if side == "SHORT" else entry * (1 - C_STOP)
            tp = entry * (1 - C_TP) if side == "SHORT" else entry * (1 + C_TP)
            s[k]["pos"].append(dict(acct=k, inst=inst, side=side, entry=entry, stop=stop, tp=tp,
                                    lev=lev, margin=margin, open_ts=time.time()))
            a["used"] += margin
            print(f"  [{k}] 开仓 {inst.split('-')[0]} {side} {lev:.1f}x 保证金{margin:,.0f} "
                  f"名义{margin*lev:,.0f} 入场{entry:,.2f} 止损{stop:,.2f} 止盈{tp:,.2f}")
            if k in ("C", "D"):
                opened.append((k, entry, stop, tp, margin, lev))

        if len(opened) == 1:
            k, entry, stop, tp, margin, lev = opened[0]
            extra = f"\n放量 {vr:.2f}x（门槛 {mult}x）" if k == "D" and vr else ""
            notify(f"{k}开仓-{inst.split('-')[0]} {SCN.get(side, side)} {entry:,.0f}",
                   f"止损 {stop:,.2f}（-{C_STOP*100:.1f}%）\n"
                   f"止盈 {tp:,.2f}（+{C_TP*100:.1f}%）\n"
                   f"保证金 {margin:,.0f}U | 名义 {margin*lev:,.0f}U | {lev:.1f}x{extra}")
        elif len(opened) > 1:
            k, entry, stop, tp, margin, lev = opened[0]
            notify(f"C+D 开仓-{inst.split('-')[0]} {SCN.get(side, side)} {entry:,.0f}",
                   f"两个账户同时进场\n止损 {stop:,.2f}（-{C_STOP*100:.1f}%）\n"
                   f"止盈 {tp:,.2f}（+{C_TP*100:.1f}%）\n"
                   f"保证金 {margin:,.0f}U | {lev:.1f}x | 放量 {vr:.2f}x")
    save_state(s)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", type=int, default=0)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()
    if a.reset and os.path.exists(STATE):
        os.remove(STATE)
        print("已重置")
    st = load_state()
    if a.status:
        status(st)
    elif a.loop > 0:
        while True:
            scan(st)
            status(st)
            time.sleep(a.loop)
    else:
        scan(st)
        status(st)
