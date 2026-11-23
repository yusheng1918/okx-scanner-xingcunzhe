#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
三账户模拟盘（实时对比）
------------------------
A：统一 8 倍，满仓复利（你原本的想法）
B：顺势 8 倍 / 逆势 4 倍，满仓复利
C：优化版 —— 只做 4H 顺势，止损 1.8% / 止盈 4.0%，单笔风险 2% 本金（实际杠杆≈1.1x）

三账户都从 10,000 U 起步，盈亏计入本金滚动。

用法：
  python paper.py --status
  python paper.py --once
  python paper.py --loop 60
  python paper.py --reset
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

C_STOP, C_TP, C_RISK = 0.018, 0.040, 0.02

# 账户D：与C规则完全相同，但入场加"放量确认"闸门
#   放量 = 最近一根已收盘 1m K 线成交量 >= 前 20 根均量 × d_vol_mult
#   门槛写在 config.json 的 d_vol_mult，可随时改（每分钟重新读取，不用重启）
D_VOL_DEFAULT = 1.2
ACCOUNTS = ("A", "B", "C", "D")


def d_vol_mult():
    """每次扫描都读一次配置，改了立即生效"""
    try:
        with open(os.path.join(HERE, "config.json"), encoding="utf-8-sig") as f:
            return float(json.load(f).get("d_vol_mult", D_VOL_DEFAULT))
    except Exception:
        return D_VOL_DEFAULT

# 只做加密货币，股票代币一律不碰（SNDK/MU/SKHYNIX 等）
CRYPTO_OK = {"BTC", "ETH", "ZEC", "SOL", "XRP", "DOGE", "BNB", "LTC", "BCH", "ADA",
             "AVAX", "LINK", "TRX", "DOT", "SUI", "APT", "ARB", "OP", "TON", "NEAR"}


def is_crypto(inst):
    return inst.split("-")[0].upper() in CRYPTO_OK


def vol_ratio(inst):
    """最近一根已收盘 1m K 线成交量 / 前 20 根均量"""
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


def load_state():
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8-sig") as f:
            s = json.load(f)
    else:
        s = {}
    for k in ACCOUNTS:            # 兼容旧状态文件，缺账户自动补
        s.setdefault(k, {"cap": START_CAP, "pos": [], "wins": 0, "closed": 0,
                         "peak": START_CAP, "mdd": 0.0})
    return s


def save_state(s):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)


def price(inst):
    return float(scanner.api("/api/v5/market/ticker", instId=inst)[0]["last"])


def close_position(acc, p, px, why):
    ret = (p["entry"] - px) / p["entry"] if p["side"] == "SHORT" else (px - p["entry"]) / p["entry"]
    notional = p["margin"] * p["lev"]
    pnl = notional * ret - notional * FEE * 2
    acc["cap"] += pnl
    acc["closed"] += 1
    if pnl > 0:
        acc["wins"] += 1
    acc["peak"] = max(acc["peak"], acc["cap"])
    acc["mdd"] = max(acc["mdd"], 1 - acc["cap"] / acc["peak"]) if acc["peak"] > 0 else 1
    acc["pos"] = [x for x in acc["pos"] if x is not p]
    with open(LOG, "a", encoding="utf-8-sig") as f:
        f.write(f"{datetime.now(BJ):%Y-%m-%d %H:%M:%S},{p.get('acct','')},{p['inst']},{p['side']},"
                f"{p['lev']:.2f},{p['entry']:.2f},{px:.2f},{pnl:.2f},{acc['cap']:.2f},{why}\n")
    return pnl


def status(s):
    print(f"\n{'账户':<4}{'本金':>12}{'收益':>9}{'已平仓':>7}{'胜率':>7}{'持仓':>5}{'回撤':>7}")
    for k in ACCOUNTS:
        a = s[k]
        wr = a["wins"] / a["closed"] * 100 if a["closed"] else 0
        print(f"{k:<4}{a['cap']:>12,.0f}{(a['cap']/START_CAP-1)*100:>8.1f}%{a['closed']:>7}"
              f"{wr:>6.1f}%{len(a['pos']):>5}{a['mdd']*100:>6.1f}%")
    for k in ACCOUNTS:
        for p in s[k]["pos"]:
            print(f"  {k} 持仓 {p['inst'].split('-')[0]:4s} {p['side']:5s} {p['lev']:.2f}x "
                  f"入场{p['entry']:,.2f} 止损{p['stop']:,.2f} 止盈{p['tp']:,.2f}")


def scan(s):
    mult = d_vol_mult()
    for inst in scanner.CFG["insts"]:
        if not is_crypto(inst):
            continue
        px = price(inst)
        for k in ACCOUNTS:
            for p in list(s[k]["pos"]):
                if p["inst"] != inst:
                    continue
                hit_stop = (px >= p["stop"]) if p["side"] == "SHORT" else (px <= p["stop"])
                hit_tp = (px <= p["tp"]) if p["side"] == "SHORT" else (px >= p["tp"])
                if hit_stop or hit_tp:
                    # 止损/止盈单按挂单价成交，只加 0.05% 滑点；
                    # 不能用扫描时的现价，否则 60 秒间隔会把成交价算得离谱
                    if hit_stop:
                        fill = p["stop"] * (1 + 0.0005) if p["side"] == "SHORT" else p["stop"] * (1 - 0.0005)
                        why = "止损"
                    else:
                        fill = p["tp"] * (1 - 0.0005) if p["side"] == "SHORT" else p["tp"] * (1 + 0.0005)
                        why = "止盈"
                    pnl = close_position(s[k], p, fill, why)
                    print(f"  [{k}] 平仓 {inst.split('-')[0]} {p['side']} @{fill:,.2f}（现价 {px:,.2f}）"
                          f" {why} {pnl:+,.0f}U → 本金 {s[k]['cap']:,.0f}U")
                    if k in ("C", "D"):
                        notify(f"{k}平仓-{inst.split('-')[0]} {p['side']} {pnl:+,.0f}U",
                               f"{why} @{fill:,.2f}\n"
                               f"本金 {s[k]['cap']:,.0f}U（{(s[k]['cap']/START_CAP-1)*100:+.1f}%）")

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
            if any(x["inst"] == inst for x in s[k]["pos"]) or s[k]["cap"] <= 0:
                continue
            cap = s[k]["cap"]
            if k in ("C", "D"):
                if not aligned4:
                    continue
                if k == "D":
                    if vr is None:
                        vr = vol_ratio(inst)
                    if vr < mult:
                        print(f"  [D] 跳过 {inst.split('-')[0]}：放量不足 {vr:.2f}x < {mult}x")
                        continue
                entry = px
                stop = entry * (1 + C_STOP) if side == "SHORT" else entry * (1 - C_STOP)
                tp = entry * (1 - C_TP) if side == "SHORT" else entry * (1 + C_TP)
                lev = min(5.0, C_RISK / C_STOP)
                margin = cap
            else:
                entry, stop, tp = plan["entry"], plan["stop"], plan["tp1"]
                lev = 8 if k == "A" else (8 if aligned1d else 4)
                margin = cap
            s[k]["pos"].append(dict(acct=k, inst=inst, side=side, entry=entry, stop=stop,
                                    tp=tp, lev=lev, margin=margin, open_ts=time.time()))
            print(f"  [{k}] 开仓 {inst.split('-')[0]} {side} {lev:.2f}x 本金{cap:,.0f} "
                  f"名义{cap*lev:,.0f} 入场{entry:,.2f} 止损{stop:,.2f} 止盈{tp:,.2f}")
            if k in ("C", "D"):
                opened.append((k, entry, stop, tp, cap, lev))

        # C / D 同一轮开仓 → 合并成一条推送
        if len(opened) == 1:
            k, entry, stop, tp, cap, lev = opened[0]
            rr = C_TP / C_STOP
            extra = f"\n放量 {vr:.2f}x（门槛 {mult}x）" if k == "D" and vr else ""
            notify(f"{k}开仓-{inst.split('-')[0]} {side} {entry:,.0f}",
                   f"止损 {stop:,.2f}（-{C_STOP*100:.1f}%）\n"
                   f"止盈 {tp:,.2f}（+{C_TP*100:.1f}%，盈亏比 {rr:.1f}）\n"
                   f"风险 {C_RISK*100:.0f}% 本金 | 名义 {cap*lev:,.0f}U | 杠杆 {lev:.2f}x{extra}")
        elif len(opened) > 1:
            k, entry, stop, tp, cap, lev = opened[0]
            rr = C_TP / C_STOP
            notify(f"C+D 开仓-{inst.split('-')[0]} {side} {entry:,.0f}",
                   f"两个账户同时进场（C 与 D）\n"
                   f"止损 {stop:,.2f}（-{C_STOP*100:.1f}%）\n"
                   f"止盈 {tp:,.2f}（+{C_TP*100:.1f}%，盈亏比 {rr:.1f}）\n"
                   f"风险 {C_RISK*100:.0f}% 本金 | 杠杆 {lev:.2f}x | 放量 {vr:.2f}x（门槛 {mult}x）")
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
