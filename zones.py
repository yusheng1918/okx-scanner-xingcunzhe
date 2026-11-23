#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
区域监控：价格进入关键位时推送
------------------------------
三类区域（由 levels_v3 同一套算法得出）：
  1) 扫单区   —— 破 24h 高点后的反手空，胜率最高
  2) 高位空区 —— 上方流动性堆积 / ATR 推算的溢价区
  3) 折价多区 —— 下方流动性堆积，且盈亏比 ≥ 1.5 才提示

去重：同币种同区域 60 分钟内只推一次（.zone_state.json）

用法：
  python zones.py --once     # 检查一次
  python zones.py --status   # 只看当前区域，不推送
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import scanner

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, ".zone_state.json")
BJ = timezone(timedelta(hours=8))
ZONE_TTL = 300          # 区域 5 分钟重算一次
COOLDOWN = 60 * 60      # 同区域 60 分钟内不重复推

_cache = {}


def _rsi(cl, n=14):
    ag = al = 0.0
    out = None
    for i in range(1, len(cl)):
        d = cl[i] - cl[i - 1]
        g, l = max(d, 0), max(-d, 0)
        if i <= n:
            ag += g; al += l
            if i == n:
                ag /= n; al /= n; out = 100 - 100 / (1 + ag / al) if al else 100
        else:
            ag = (ag * (n - 1) + g) / n
            al = (al * (n - 1) + l) / n
            out = 100 - 100 / (1 + ag / al) if al else 100
    return out


def _atr(cs, n=14):
    trs = [max(cs[i]["h"] - cs[i]["l"], abs(cs[i]["h"] - cs[i - 1]["c"]), abs(cs[i]["l"] - cs[i - 1]["c"]))
           for i in range(1, len(cs))]
    return sum(trs[-n:]) / min(n, len(trs))


def _swings(cs, k=3):
    hi, lo = [], []
    for i in range(k, len(cs) - k):
        seg = cs[i - k:i + k + 1]
        if cs[i]["h"] == max(x["h"] for x in seg):
            hi.append(cs[i]["h"])
        if cs[i]["l"] == min(x["l"] for x in seg):
            lo.append(cs[i]["l"])
    return hi, lo


def _cluster(levels, tol=0.0018):
    if not levels:
        return []
    ls = sorted(levels)
    out, grp = [], [ls[0]]
    for x in ls[1:]:
        if abs(x - grp[-1]) / grp[-1] <= tol:
            grp.append(x)
        else:
            out.append(sum(grp) / len(grp)); grp = [x]
    out.append(sum(grp) / len(grp))
    return out


def build(inst):
    d1 = scanner.candles(inst, "1D", 60, ttl=ZONE_TTL)
    h1 = scanner.candles(inst, "1H", 200, ttl=ZONE_TTL)
    px = float(scanner.api("/api/v5/market/ticker", instId=inst)[0]["last"])
    ccy = inst.split("-")[0]
    try:
        fr = float(scanner.api("/api/v5/public/funding-rate", instId=inst)[0]["fundingRate"])
    except Exception:
        fr = 0.0

    prev = d1[-2]
    d3 = d1[-3:]
    hi3, lo3 = max(c["h"] for c in d3), min(c["l"] for c in d3)
    w = h1[-24:]
    vw = sum(((c["h"] + c["l"] + c["c"]) / 3) * c["v"] for c in w) / max(1e-9, sum(c["v"] for c in w))
    hi24 = max(max(c["h"] for c in h1[-24:]), px)
    lo24 = min(min(c["l"] for c in h1[-24:]), px)
    a = _atr(d1)
    r1 = _rsi([c["c"] for c in h1])

    sh, sl = _swings(h1[-160:], 3)
    ups = sorted([x for x in _cluster(sh) if x > px * 1.002])
    dns = sorted([x for x in _cluster(sl) if x < px * 0.998], reverse=True)
    if not ups:
        ups = [px + a * 0.45]
    if not dns:
        dns = [px - a * 0.45]

    z_lo = max(ups[0] * 0.999, px * 1.003)
    z_hi = min(max(ups[0] * 1.001, z_lo * 1.003), z_lo * 1.008)
    stop_s = max(z_hi * 1.0035, z_hi + a * 0.22)

    l_hi = min(dns[0] * 1.001, px * 0.997)
    l_lo = max(min(dns[0] * 0.999, l_hi * 0.997), l_hi * 0.992)
    stop_l = min(l_lo * 0.9965, l_lo - a * 0.22)

    sw_lo = max(hi24 * 1.001, px * 1.002)
    sw_hi = max(hi24 * 1.006, sw_lo * 1.003)
    sw_stop = max(sw_hi * 1.005, sw_hi + a * 0.22)

    ep_s = (z_lo + z_hi) / 2
    cand = sorted([x for x in (vw, (hi3 + lo3) / 2, prev["l"], lo3) if x < ep_s * 0.995], reverse=True)
    t1 = cand[0] if cand else ep_s - a
    rr = (ep_s - t1) / (stop_s - ep_s) if stop_s > ep_s else 0

    ep_l = (l_lo + l_hi) / 2
    candl = sorted([x for x in (vw, (hi3 + lo3) / 2, prev["h"], hi3) if x > ep_l * 1.005])
    tl = candl[0] if candl else ep_l + a
    rr_l = (tl - ep_l) / (ep_l - stop_l) if ep_l > stop_l else 0

    return dict(inst=inst, px=px, fr=fr, rsi=r1, atr=a, vw=vw,
                zones={
                    "sw": dict(lo=sw_lo, hi=sw_hi, stop=sw_stop, t1=t1, name="扫单区"),
                    "sh": dict(lo=z_lo, hi=z_hi, stop=stop_s, t1=t1, rr=rr, name="高位空区"),
                    "lg": dict(lo=l_lo, hi=l_hi, stop=stop_l, t1=tl, rr=rr_l, name="折价多区"),
                })


def get(inst):
    c = _cache.get(inst)
    if c and time.time() - c[0] < ZONE_TTL:
        return c[1]
    d = build(inst)
    _cache[inst] = (time.time(), d)
    return d


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(s):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(s, f)


def check(quiet=False, dry=False):
    st = load_state()
    now = time.time()
    hits = []
    for inst in scanner.CFG["insts"]:
        if inst.split("-")[0].upper() not in __import__("paper").CRYPTO_OK:
            continue
        try:
            d = get(inst)
        except Exception as e:
            if not quiet:
                print(f"{inst} 区域计算失败: {e}")
            continue
        n = inst.split("-")[0]
        px = d["px"]
        for kind in ("sw", "sh", "lg"):
            z = d["zones"][kind]
            if not (z["lo"] <= px <= z["hi"]):
                continue
            if kind == "lg" and z.get("rr", 0) < 1.5:
                continue
            key = f"{inst}:{kind}"
            if now - st.get(key, 0) < COOLDOWN:
                continue
            st[key] = now
            side = "空" if kind != "lg" else "多"
            title = f"{n} 进入{z['name']}"
            body = (f"现价 {px:,.2f}  区域 {z['lo']:,.0f}~{z['hi']:,.0f}\n"
                    f"止损 {z['stop']:,.0f}  目标1 {z['t1']:,.0f}\n"
                    f"RSI {d['rsi']:.0f} | 资金费率 {d['fr']*100:+.4f}% | 方向 {side}")
            hits.append((title, body))
            if not quiet:
                print(f"  >>> {title}  现价 {px:,.2f} 区域 {z['lo']:,.0f}~{z['hi']:,.0f}")
            if not dry:
                scanner.bark(title, body)
    save_state(st)
    return hits


def status():
    for inst in scanner.CFG["insts"]:
        try:
            d = get(inst)
        except Exception as e:
            print(f"{inst} 失败: {e}")
            continue
        n = inst.split("-")[0]
        px = d["px"]
        print(f"\n{n}  现价 {px:,.2f}  RSI {d['rsi']:.0f}")
        for kind, z in d["zones"].items():
            inside = "  ← 已在区域内" if z["lo"] <= px <= z["hi"] else ""
            extra = f" 盈亏比 {z.get('rr', 0):.2f}" if kind != "sw" else ""
            print(f"  {z['name']:6s} {z['lo']:>10,.2f} ~ {z['hi']:>10,.2f}  止损 {z['stop']:>10,.2f}{extra}{inside}")


if __name__ == "__main__":
    if "--status" in sys.argv:
        status()
    elif "--dry" in sys.argv:
        check(dry=True)
    else:
        check()
