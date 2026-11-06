#!/usr/bin/env python3
"""组合层：评估已有持仓的风险，或按约束配置权重。

    python scripts/run.py analyze  --holdings holdings.csv --start 2024-01-01 --end 2025-12-31
    python scripts/run.py optimize --codes 600519,300750,688981 --start 2024-01-01 --end 2025-12-31 \
        --method minvar --max-weight 0.4
    python scripts/run.py demo
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
for candidate in (os.getenv("ASHARE_LIB"),
                  SKILL_DIR.parents[1] / "lib" if len(SKILL_DIR.parents) > 1 else None,
                  SKILL_DIR / "lib"):
    if candidate and Path(candidate).is_dir() and (Path(candidate) / "ashare_core").is_dir():
        sys.path.insert(0, str(candidate))
        break

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from ashare_core import chart, names, portfolio  # noqa: E402
from ashare_core.providers.eltdx_provider import EltdxProvider  # noqa: E402
from ashare_core.store import BarsCache  # noqa: E402


def _default_data_dir() -> Path:
    env = os.getenv("ASHARE_DATA")
    if env:
        return Path(env)
    pack_data = SKILL_DIR.parents[1] / "data" if len(SKILL_DIR.parents) > 1 else None
    if pack_data and (pack_data.exists() or (SKILL_DIR.parents[1] / "lib").exists()):
        return pack_data
    return Path.home() / ".ashare" / "data"


DATA = _default_data_dir()


def resolve_code(raw: str, provider=None) -> str:
    s = str(raw).strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    if digits and len(digits) <= 6:
        return digits.zfill(6)
    if provider is None:
        raise SystemExit(f"✗ 请用 6 位代码：{raw}")
    lst = provider.stock_list()
    hit = lst[lst["name"] == s]
    if hit.empty:
        hit = lst[lst["name"].str.contains(s, na=False)]
    if hit.empty or len(hit) > 1:
        raise SystemExit(f"✗ 无法唯一确定「{s}」")
    return str(hit.iloc[0]["code"])


def load_close(codes: list[str], start: str, end: str) -> pd.DataFrame:
    with EltdxProvider() as p:
        cache = BarsCache(p, DATA)
        resolved = [resolve_code(c, p) for c in codes]
        close = cache.panel(resolved, start, end, "hfq", "close")
    close = close.dropna(how="all").ffill()
    if close.empty:
        raise SystemExit("✗ 没有可用的收盘价数据")
    return close


def _chart(path: Path, weights, codes: list[str], ret: pd.DataFrame,
           stats: dict, method: str = "", title: str = "",
           desc: str = "", weights_title: str = "目标权重",
           series_label: str = "优化组合") -> None:
    eq = (1 + pd.Series(portfolio.portfolio_returns(weights, ret), index=ret.index)).cumprod()
    eq = eq * 100_000
    bench_w = portfolio.equal_weights(len(codes))
    bench = (1 + pd.Series(portfolio.portfolio_returns(bench_w, ret), index=ret.index)).cumprod() * 100_000

    label = {"minvar": "最小方差", "riskparity": "风险平价", "equal": "等权"}.get(method, method or "")
    display = [names.lookup_name(c, DATA) for c in codes]
    out = chart.render_portfolio(
        path, weights, codes, eq, bench, stats, names=display,
        title=title or (f"组合优化 · {label}" if label else "现有持仓风险"),
        desc=desc or f"{len(codes)} 只标的 · 按方法求解权重",
        weights_title=weights_title,
        series_label=series_label,
        period=f"{ret.index[0][:10]} ~ {ret.index[-1][:10]} · 后复权",
    )
    if out:
        print(f"  组合图 → {out}")


def _report(codes, weights, stats, ret, title: str) -> None:
    bar = "─" * 60
    print(bar)
    print(f"  {title}")
    print(bar)
    print(f"  年化收益 {stats['ann_return']*100:>7.2f}%   年化波动 {stats['ann_vol']*100:>6.2f}%   "
          f"夏普 {stats['sharpe']:>5.2f}")
    print(f"  区间累计 {stats['total_return']*100:>7.2f}%   最大回撤 {stats['max_dd']*100:>6.2f}%   "
          f"样本 {stats['n_days']} 天")
    print(f"  VaR95(日) {stats['var95']*100:>5.2f}%   CVaR95(日) {stats['cvar95']*100:>5.2f}%   "
          f"HHI {stats['hhi']:.3f}   前三大 {stats['top3']*100:.1f}%")
    print(bar)
    print(f"  {'代码':<8}{'权重':>8}")
    for c, w in sorted(zip(codes, weights), key=lambda x: -x[1]):
        print(f"  {c:<8}{w*100:>7.1f}%")


def cmd_analyze(args) -> int:
    csv = Path(args.holdings)
    if not csv.exists():
        raise SystemExit(f"✗ 找不到持仓文件 {csv}")
    df = pd.read_csv(csv)
    df.columns = [c.strip().lower() for c in df.columns]
    if "code" not in df.columns:
        raise SystemExit("✗ 持仓文件需要 code 列")
    codes = [str(c).zfill(6) for c in df["code"]]
    if "weight" in df.columns:
        w = df["weight"].astype(float).values
    elif "qty" in df.columns:
        close = load_close(codes, args.start, args.end)
        px = close.iloc[-1].values
        w = df["qty"].astype(float).values * px
    else:
        raise SystemExit("✗ 持仓文件需要 weight 或 qty 列")
    w = np.asarray(w, dtype=float)
    w = w / w.sum()

    close = load_close(codes, args.start, args.end)
    ret = portfolio.returns_matrix(close)
    stats = portfolio.risk_stats(w, ret)
    _report(codes, w, stats, ret, f"{len(codes)} 只标的 · 现有持仓风险")

    corr = portfolio.correlation(ret)
    print("\n  相关性矩阵")
    head = "        " + "".join(f"{c[-4:]:>8}" for c in corr.columns)
    print(head)
    for c in corr.index:
        row = "".join(f"{corr.loc[c, x]:>8.2f}" for x in corr.columns)
        print(f"  {c:<6}{row}")

    if args.json:
        print(json.dumps({"weights": dict(zip(codes, w.tolist())), "stats": stats},
                         ensure_ascii=False, indent=2, default=str))
        return 0
    if not args.no_plot:
        out = Path(args.plot) if args.plot else SKILL_DIR / "out" / f"portfolio_{len(codes)}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        _chart(out, w, codes, ret, stats, "", "现有持仓风险",
               desc=f"{len(codes)} 只标的 · 按当前市值权重", weights_title="当前权重",
               series_label="现有持仓")
    return 0


def cmd_optimize(args) -> int:
    codes = [c.strip() for chunk in args.codes for c in chunk.split(",") if c.strip()]
    if len(codes) < 2:
        raise SystemExit("✗ 至少两只标的")
    close = load_close(codes, args.start, args.end)
    ret = portfolio.returns_matrix(close)
    codes = list(ret.columns)

    w, note = portfolio.optimize(ret, method=args.method,
                                 max_weight=args.max_weight, min_weight=args.min_weight)
    stats = portfolio.risk_stats(w, ret)
    _report(codes, w, stats, ret, f"{len(codes)} 只标的 · {args.method} 优化")
    if note:
        print(f"\n  ⚠ {note}")

    eq_w = portfolio.equal_weights(len(codes))
    eq_stats = portfolio.risk_stats(eq_w, ret)
    print(f"\n  对比等权：等权波动 {eq_stats['ann_vol']*100:.2f}% / 夏普 {eq_stats['sharpe']:.2f}"
          f"　优化后波动 {stats['ann_vol']*100:.2f}% / 夏普 {stats['sharpe']:.2f}")
    print("\n  ⚠ 后视镜偏差：权重由整段历史协方差一次性求解，净值曲线来自同一段历史；")
    print("     这不是可实盘复现的净值，只用于对比不同约束下的组合形态。")

    if not args.no_plot:
        out = Path(args.plot) if args.plot else SKILL_DIR / "out" / f"portfolio_{args.method}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        _chart(out, w, codes, ret, stats, args.method,
               desc=(f"{len(codes)} 只标的 · 单票上限 {args.max_weight:.0%} · "
                     f"{ {'minvar': '最小方差', 'riskparity': '风险平价', 'equal': '等权'}.get(args.method, args.method) }"))
    return 0


def cmd_demo(args) -> int:
    codes = ["600519", "300750", "688981", "000001", "002594"]
    close = load_close(codes, "2024-01-01", "2025-12-31")
    ret = portfolio.returns_matrix(close)
    w = portfolio.equal_weights(len(codes))
    stats = portfolio.risk_stats(w, ret)

    w2, note2 = portfolio.optimize(ret, method="minvar", max_weight=0.4)
    s2 = portfolio.risk_stats(w2, ret)

    checks = [
        ("等权之和为 1", abs(float(w.sum()) - 1) < 1e-9, f"{w.sum():.6f}"),
        ("样本天数 > 200", stats["n_days"] > 200, f"{stats['n_days']} 天"),
        ("波动率有限", math.isfinite(stats["ann_vol"]) and stats["ann_vol"] > 0,
         f"{stats['ann_vol']*100:.2f}%"),
        ("最小方差权重合法", abs(float(w2.sum()) - 1) < 1e-6 and (w2 >= -1e-9).all(),
         "、".join(f"{x:.0%}" for x in w2)),
        ("优化后波动 ≤ 等权", s2["ann_vol"] <= stats["ann_vol"] + 1e-9,
         f"{s2['ann_vol']*100:.2f}% ≤ {stats['ann_vol']*100:.2f}%"),
    ]
    print("离线自检：5 只标的组合（2024-2025）")
    passed = 0
    for name, ok, detail in checks:
        passed += ok
        print(f"  {'✓' if ok else '✗'} {name}：{detail}")
    if note2:
        print(f"  提示：{note2}")
    print(f"\n  等权 波动 {stats['ann_vol']*100:.2f}% 夏普 {stats['sharpe']:.2f}｜"
          f"最小方差 波动 {s2['ann_vol']*100:.2f}% 夏普 {s2['sharpe']:.2f}")
    total = len(checks)
    print(f"\n{'✓ 通过' if passed == total else '✗ 未通过'}（{passed}/{total}）")
    return 0 if passed == total else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="组合风险与配置")
    ap.add_argument("--data-dir", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="评估已有持仓")
    a.add_argument("--holdings", required=True)
    a.add_argument("--start", required=True)
    a.add_argument("--end", required=True)
    a.add_argument("--plot", default=None)
    a.add_argument("--no-plot", action="store_true")
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_analyze)

    o = sub.add_parser("optimize", help="按约束配置权重")
    o.add_argument("--codes", required=True, nargs="+", help="逗号分隔或空格分隔，如 600519,300750")
    o.add_argument("--start", required=True)
    o.add_argument("--end", required=True)
    o.add_argument("--method", default="minvar", choices=["minvar", "riskparity", "equal"])
    o.add_argument("--max-weight", type=float, default=0.4)
    o.add_argument("--min-weight", type=float, default=0.0)
    o.add_argument("--plot", default=None)
    o.add_argument("--no-plot", action="store_true")
    o.set_defaults(func=cmd_optimize)

    d = sub.add_parser("demo", help="离线自检")
    d.set_defaults(func=cmd_demo)

    args = ap.parse_args()
    if args.data_dir:
        global DATA
        DATA = Path(args.data_dir)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
