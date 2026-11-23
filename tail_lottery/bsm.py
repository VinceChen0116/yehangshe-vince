"""BSM 欧式期权：价格、delta、gamma、vega、charm，以及二分法反推 IV（Skew / Charm 用）。

约定：T 以年为单位（日历时间，365 天）；r = 无风险利率 4%，q = 股息率 1.2%（说明 7.4 节给定）。
charm 定义为"时间流逝 1 年 delta 的变化"（= −∂Δ/∂T），乘以 dt_years 即得 dt 内的 delta 变化。
"""
from __future__ import annotations

import math

R = 0.04
Q = 0.012
YEAR_S = 365.0 * 24 * 3600


def _ncdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def _npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1d2(S, K, T, sigma, r, q):
    st = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / st
    return d1, d1 - st


def price(S, K, T, sigma, right, r=R, q=Q) -> float:
    if T <= 0 or sigma <= 0:
        return max(0.0, (S - K) if right == "C" else (K - S))
    d1, d2 = _d1d2(S, K, T, sigma, r, q)
    if right == "C":
        return S * math.exp(-q * T) * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * math.exp(-q * T) * _ncdf(-d1)


def delta(S, K, T, sigma, right, r=R, q=Q) -> float:
    d1, _ = _d1d2(S, K, T, sigma, r, q)
    if right == "C":
        return math.exp(-q * T) * _ncdf(d1)
    return -math.exp(-q * T) * _ncdf(-d1)


def gamma(S, K, T, sigma, r=R, q=Q) -> float:
    d1, _ = _d1d2(S, K, T, sigma, r, q)
    return math.exp(-q * T) * _npdf(d1) / (S * sigma * math.sqrt(T))


def vega(S, K, T, sigma, r=R, q=Q) -> float:
    """每 1.00 波动率（不是每 1 个百分点）的价格变化。"""
    d1, _ = _d1d2(S, K, T, sigma, r, q)
    return S * math.exp(-q * T) * _npdf(d1) * math.sqrt(T)


def charm(S, K, T, sigma, right, r=R, q=Q) -> float:
    """−∂Δ/∂T（每年）。"""
    d1, d2 = _d1d2(S, K, T, sigma, r, q)
    st = sigma * math.sqrt(T)
    common = math.exp(-q * T) * _npdf(d1) * (2 * (r - q) * T - d2 * st) / (2 * T * st)
    if right == "C":
        return q * math.exp(-q * T) * _ncdf(d1) - common
    return -q * math.exp(-q * T) * _ncdf(-d1) - common


def bounds(S, K, T, right, r=R, q=Q):
    """无套利边界 (下限, 上限)。"""
    fs, fk = S * math.exp(-q * T), K * math.exp(-r * T)
    if right == "C":
        return max(0.0, fs - fk), fs
    return max(0.0, fk - fs), fk


def implied_vol(px, S, K, T, right, r=R, q=Q, lo=0.01, hi=5.0, tol=1e-6, max_iter=200):
    """二分法反推 IV；价格不在 [price(lo), price(hi)] 内返回 None。"""
    if px is None or T <= 0 or S <= 0 or K <= 0:
        return None
    plo, phi = price(S, K, T, lo, right, r, q), price(S, K, T, hi, right, r, q)
    if not (plo <= px <= phi):
        return None
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        pm = price(S, K, T, mid, right, r, q)
        if abs(pm - px) < tol:
            return mid
        if pm < px:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)
