#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OKX 多周期扫描器 v2 + Bark 推送
--------------------------------
六周期共振：1周 / 1日 / 4小时 / 1小时 / 15分钟 / 1分钟
  - 高周期定方向（周、日、4H 加权投票）
  - 中周期定位置（1H、15m 均线与关键位）
  - 低周期定扳机（1m 突破/跌破确认）
  - 止损放在高周期结构外侧，止盈指向下一档高周期关键位

用法：
  python scanner.py --once --dry-run
  python scanner.py --once
  python scanner.py --loop 60
  python scanner.py --test-push
环境变量：BARK_KEY（部署时用，优先于 config.json）、OKX_PROXY（默认空=直连）
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, "config.json")
LOG = os.path.join(HERE, "signals.csv")
STATE = os.path.join(HERE, ".scan_state.json")
BJ = timezone(timedelta(hours=8))

DEFAULT_CFG = {
    "bark_key": "",
    "bark_server": "https://api.day.app",
    "proxy": "",
    "insts": ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "ZEC-USDT-SWAP", "SOL-USDT-SWAP"],
    "sound": "bell",
    "level": "timeSensitive",
    "title_prefix": "幸存者-",
    "cooldown_min": 45,
    "max_push_per_hour": 6,
    "leverage": 5,
    "risk_pct": 1.0,
    "equity": 10000,
    "bias_threshold": 0.35,
}


def load_cfg():
    c = dict(DEFAULT_CFG)
    if os.path.exists(CFG_PATH):
        with open(CFG_PATH, encoding="utf-8-sig") as f:
            c.update(json.load(f))
    if os.environ.get("BARK_KEY"):
        c["bark_key"] = os.environ["BARK_KEY"]
    if os.environ.get("OKX_PROXY") is not None:
        c["proxy"] = os.environ["OKX_PROXY"]
    return c


CFG = load_cfg()
_op = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": CFG["proxy"], "https": CFG["proxy"]})
) if CFG["proxy"] else urllib.request.build_opener()

_CACHE = {}


def api(path, **p):
    url = "https://www.okx.com" + path + ("?" + urllib.parse.urlencode(p) if p else "")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with _op.open(req, timeout=20) as r:
        d = json.loads(r.read().decode())
    if d.get("code") != "0":
        raise RuntimeError(d.get("msg"))
    return d["data"]


def candles(inst, bar, limit, ttl=0):
    key = (inst, bar)
    if ttl and key in _CACHE and time.time() - _CACHE[key][0] < ttl:
        return _CACHE[key][1]
    rows = list(reversed(api("/api/v5/market/candles", instId=inst, bar=bar, limit=limit)))
    out = [dict(ts=int(r[0]), o=float(r[1]), h=float(r[2]), l=float(r[3]), c=float(r[4]), v=float(r[5]))
           for r in rows]
    _CACHE[key] = (time.time(), out)
    return out


def ema(v, n):
    k = 2 / (n + 1)
    e = v[0]
    out = [e]
    for x in v[1:]:
        e = x * k + e * (1 - k)
        out.append(e)
    return out


def rsi(cl, n=14):
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
            ag = (ag * (n - 1) + g) / n; al = (al * (n - 1) + l) / n
            out = 100 - 100 / (1 + ag / al) if al else 100
    return out


def atr(cs, n=14):
    trs = [max(cs[i]["h"] - cs[i]["l"], abs(cs[i]["h"] - cs[i - 1]["c"]), abs(cs[i]["l"] - cs[i - 1]["c"]))
           for i in range(1, len(cs))]
    return sum(trs[-n:]) / min(n, len(trs))


def trend(cs, fast=20, slow=50, slope_n=5):
    cl = [c["c"] for c in cs]
    if len(cl) < slow + slope_n:
        return "RANGE", 0.0, rsi(cl)
    ef = ema(cl, fast)
    es = ema(cl, slow)
    slope = (ef[-1] / ef[-1 - slope_n] - 1) * 100
    if ef[-1] > es[-1] and slope > 0:
        return "UP", slope, rsi(cl)
    if ef[-1] < es[-1] and slope < 0:
        return "DOWN", slope, rsi(cl)
    return "RANGE", slope, rsi(cl)


TFS = [("1W", "1W", 100, 1800), ("1D", "1D", 120, 900), ("4H", "4H", 150, 900),
       ("1H", "1H", 200, 300), ("15m", "15m", 200, 120)]
WEIGHTS = {"1W": 1.5, "1D": 1.5, "4H": 1.2, "1H": 1.0, "15m": 0.8}


def mtf(inst):
    res, score, wsum = {}, 0.0, 0.0
    for label, bar, limit, ttl in TFS:
        cs = candles(inst, bar, limit, ttl)
        t, slope, r = trend(cs)
        res[label] = dict(trend=t, slope=slope, rsi=r, cs=cs)
        v = 1 if t == "UP" else (-1 if t == "DOWN" else 0)
        score += v * WEIGHTS[label]
        wsum += WEIGHTS[label]
    return res, score / wsum


def htf_levels(m):
    """收集高周期关键位"""
    lv = []
    for label in ("1W", "1D", "4H"):
        cs = m[label]["cs"]
        recent = cs[-30:] if label == "4H" else cs[-10:]
        lv.append((max(c["h"] for c in recent), label + "高"))
        lv.append((min(c["l"] for c in recent), label + "低"))
    return sorted(lv)


def analyse(inst):
    m, bias = mtf(inst)
    m1 = candles(inst, "1m", 300)[:-1]
    px = m1[-1]["c"]
    a1 = atr(m1)
    r1 = rsi([c["c"] for c in m1])
    e20_1m = ema([c["c"] for c in m1], 20)[-1]
    cur, prev = m1[-1], m1[-2]
    win = m1[-60:]
    hi = max(c["h"] for c in win)
    lo = min(c["l"] for c in win)
    pct60 = (px - lo) / (hi - lo) * 100 if hi > lo else 50

    levels = htf_levels(m)
    res_above = [x for x in levels if x[0] > px * 1.0005]
    sup_below = [x for x in levels if x[0] < px * 0.9995]
    nearest_res = min(res_above, key=lambda x: x[0]) if res_above else None
    nearest_sup = max(sup_below, key=lambda x: x[0]) if sup_below else None

    # 【优化版】方向只由 4H 趋势决定：只做顺势，逆势单直接放弃
    side = None
    why = ""
    t4 = m["4H"]["trend"]
    if t4 == "DOWN" and px >= e20_1m * 0.9995 and 50 <= r1 <= 78 and cur["c"] < prev["l"]:
        side = "SHORT"
        why = f"4H空头 / 1m反弹EMA20 / RSI {r1:.0f} / 跌破前低"
    elif t4 == "UP" and px <= e20_1m * 1.0005 and 22 <= r1 <= 50 and cur["c"] > prev["h"]:
        side = "LONG"
        why = f"4H多头 / 1m回踩EMA20 / RSI {r1:.0f} / 突破前高"

    plan = None
    if side:
        sp = CFG.get("stop_pct", 0.018)
        tpp = CFG.get("tp_pct", 0.040)
        if side == "SHORT":
            stop = px * (1 + sp)
            tp1 = px * (1 - tpp)
            tp2 = px * (1 - tpp * 1.5)
        else:
            stop = px * (1 - sp)
            tp1 = px * (1 + tpp)
            tp2 = px * (1 + tpp * 1.5)
        plan = dict(side=side, entry=px, stop=stop, tp1=tp1, tp2=tp2,
                    risk_pct=sp * 100, why=why,
                    res=nearest_res, sup=nearest_sup)

    return dict(inst=inst, px=px, m=m, bias=bias, rsi=r1, atr=a1, pct60=pct60, plan=plan)


def bark(title, body, dry=False):
    key = CFG["bark_key"]
    if not key:
        print(f"  [无 Key，跳过] {title} | {body}")
        return False
    title = CFG.get("title_prefix", "") + title
    payload = {
        "device_key": key,
        "title": title,
        "body": body,
        "group": "OKX预警",
        "sound": CFG.get("sound", "bell"),
        "level": CFG.get("level", "active"),
    }
    if dry:
        print(f"  [DRY] {title} | {body}")
        return True
    req = urllib.request.Request(
        CFG["bark_server"].rstrip("/") + "/push",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": "Mozilla/5.0"},
        method="POST",
    )
    try:
        with _op.open(req, timeout=15) as r:
            ok = json.loads(r.read().decode()).get("code") == 200
    except Exception as e:
        print(f"  推送异常: {e}")
        return False
    print(f"  推送{'成功' if ok else '失败'}: {title}")
    return ok


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("cooldown", {})
    s.setdefault("pushes", [])
    s.setdefault("open", [])                       # 已发出、尚未止盈止损的信号
    s.setdefault("stats", {"n": 0, "w": 0, "l": 0, "r": 0.0})
    return s


def save_state(s):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(s, f)


def stats_line(st):
    s = st["stats"]
    n = s["n"]
    wr = (s["w"] / n * 100) if n else 0.0
    return f"累计 {n} 笔 | 胜 {s['w']} 负 {s['l']} | 胜率 {wr:.1f}% | {s['r']:+.1f}R"


def check_tracked(st, inst, px, dry=False):
    """检查已发出的信号是否触发止盈/止损，并推送结果"""
    keep = []
    for t in st["open"]:
        if t["inst"] != inst:
            keep.append(t)
            continue
        side = t["side"]
        res = None
        if side == "SHORT":
            if px >= t["stop"]:
                res = ("止损", t["stop"])
            elif px <= t["tp1"]:
                res = ("止盈", t["tp1"])
        else:
            if px <= t["stop"]:
                res = ("止损", t["stop"])
            elif px >= t["tp1"]:
                res = ("止盈", t["tp1"])
        if res is None:
            if time.time() - t["ts"] > 72 * 3600:
                res = ("超时", px)
            else:
                keep.append(t)
                continue

        why, out_px = res
        risk = abs(t["entry"] - t["stop"])
        move = ((t["entry"] - out_px) if side == "SHORT" else (out_px - t["entry"])) / t["entry"]
        pnl = t["margin"] * t["lev"] * move - t["margin"] * t["lev"] * 0.001
        r = (move * t["entry"]) / risk if risk else 0.0

        st["stats"]["n"] += 1
        st["stats"]["r"] += r
        if r > 0:
            st["stats"]["w"] += 1
        else:
            st["stats"]["l"] += 1

        hold = (time.time() - t["ts"]) / 3600
        icon = "✅" if r > 0 else "❌"
        title = f"{t['name']} {t['side_cn']} {t['entry']:,.0f} 已{why}{icon}"
        body = (f"出场 {out_px:,.2f}（{move*100:+.1f}%）| 持有 {hold:.1f} 小时\n"
                f"盈亏 {pnl:+,.0f}U（保证金 {t['margin']:,.0f}U @{t['lev']:.0f}x）\n"
                f"{stats_line(st)}")
        print(f"  [追踪] {title}  {pnl:+,.0f}U  [{stats_line(st)}]")
        if not dry:
            bark(title, body)
    st["open"] = keep[:60]


def brief(m):
    return " ".join(f"{k}:{'多' if v['trend']=='UP' else ('空' if v['trend']=='DOWN' else '震')}"
                    for k, v in m.items())


def scan(dry=False):
    now = time.time()
    st = load_state()
    st["pushes"] = [t for t in st["pushes"] if now - t < 3600]
    print(f"===== {datetime.now(BJ):%m-%d %H:%M:%S} 扫描 =====")
    for inst in CFG["insts"]:
        try:
            r = analyse(inst)
        except Exception as e:
            print(f"{inst} 失败: {e}")
            continue
        n = inst.split("-")[0]
        m = r["m"]
        print(f"{n:4s} {r['px']:>10.2f} | 周{m['1W']['trend'][:1]} 日{m['1D']['trend'][:1]} "
              f"4H{m['4H']['trend'][:1]} 1H{m['1H']['trend'][:1]} 15m{m['15m']['trend'][:1]} "
              f"| 共振 {r['bias']:+.2f} | RSI {r['rsi']:.1f}")
        check_tracked(st, inst, r["px"], dry)   # 先结算已有信号的止盈/止损
        p = r["plan"]
        if not p:
            continue
        key = f"{inst}:{p['side']}"
        if now - st["cooldown"].get(key, 0) < CFG["cooldown_min"] * 60:
            print("  (冷却中，跳过)")
            continue
        if len(st["pushes"]) >= CFG["max_push_per_hour"]:
            print("  (本小时推送超限，跳过)")
            continue
        eq = CFG["equity"]
        # 与模拟盘一致的口径：保证金 = 权益 × 15%，名义 = 保证金 × 杠杆
        mpct = CFG.get("margin_pct", 15) / 100.0
        lev = CFG.get("leverage", 5)
        margin = eq * mpct
        notional = margin * lev
        side_cn = "空" if p["side"] == "SHORT" else "多"
        day = {"UP": "多", "DOWN": "空", "RANGE": "震荡"}[m["1D"]["trend"]]
        week = {"UP": "多", "DOWN": "空", "RANGE": "震荡"}[m["1W"]["trend"]]
        title = f"{n} {side_cn} {p['entry']:,.0f}"
        body = (f"日线趋势:{day} | 周线:{week}\n"
                f"周期: {brief(m)} | 共振{r['bias']:+.2f}\n"
                f"{p['why']}\n"
                f"入场 {p['entry']:,.2f}  止损 {p['stop']:,.2f} ({p['risk_pct']:.2f}%)\n"
                f"TP1 {p['tp1']:,.2f}  TP2 {p['tp2']:,.2f}\n"
                f"保证金 {margin:,.0f}U（权益{mpct*100:.0f}%）| 名义 {notional:,.0f}U @ {lev}x")
        print(f"  >>> {side_cn} {p['why']}")
        print(f"      入场 {p['entry']:,.2f} 止损 {p['stop']:,.2f} TP1 {p['tp1']:,.2f} TP2 {p['tp2']:,.2f}")
        bark(title, body, dry=dry)
        # 记录这条信号，等它止盈/止损后再推一条结果
        st["open"].append(dict(inst=inst, name=n, side=p["side"], side_cn=side_cn,
                               entry=p["entry"], stop=p["stop"], tp1=p["tp1"],
                               margin=margin, lev=lev, ts=now))
        st["cooldown"][key] = now
        st["pushes"].append(now)
        with open(LOG, "a", encoding="utf-8-sig") as f:
            f.write(f"{datetime.now(BJ):%Y-%m-%d %H:%M:%S},{n},{p['side']},{p['entry']:.2f},"
                    f"{p['stop']:.2f},{p['tp1']:.2f},{p['risk_pct']:.2f},{m['1D']['trend']},{r['bias']:.2f}\n")
    save_state(st)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test-push", action="store_true")
    a = ap.parse_args()
    if a.test_push:
        bark("推送测试", "多周期扫描器已就绪：日线趋势 / 周线 / 4H / 1H / 15m 共振。", dry=a.dry_run)
    elif a.loop > 0:
        while True:
            scan(dry=a.dry_run)
            time.sleep(a.loop)
    else:
        scan(dry=a.dry_run)
