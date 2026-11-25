"""BSM calculations. Rates/positions are assumptions; outputs are not dealer inventory."""
from math import erf, exp, isfinite, log, pi, sqrt

YEAR_SECONDS = 365.0 * 86400


def cdf(x):
    return (1 + erf(x / sqrt(2))) / 2


def bsm(spot, strike, seconds, sigma, right, rate=.04, dividend=.012):
    if not all(isfinite(x) and x > 0 for x in (spot, strike, seconds, sigma)):
        raise ValueError("positive finite spot/strike/time/volatility required")
    t = seconds / YEAR_SECONDS
    d1 = (log(spot / strike) + (rate - dividend + sigma*sigma/2)*t) / (sigma*sqrt(t))
    d2 = d1 - sigma*sqrt(t)
    ds, dk = exp(-dividend*t), exp(-rate*t)
    density = exp(-d1*d1/2) / sqrt(2*pi)
    call = spot*ds*cdf(d1) - strike*dk*cdf(d2)
    price = call if right == "C" else call - spot*ds + strike*dk
    delta = ds*(cdf(d1) if right == "C" else cdf(d1)-1)
    return {"price": max(0., price), "delta": delta,
            "gamma": ds*density/(spot*sigma*sqrt(t)),
            "vega": spot*ds*density*sqrt(t), "iv": sigma}


def implied(spot, strike, seconds, price, right, rate=.04, dividend=.012):
    if seconds <= 0 or price <= 0:
        return None
    t = seconds / YEAR_SECONDS
    s, k = spot*exp(-dividend*t), strike*exp(-rate*t)
    lower, upper = (max(s-k, 0), s) if right == "C" else (max(k-s, 0), k)
    if not lower+1e-7 < price < upper:
        return None
    low, high = 1e-4, 5.0
    if bsm(spot, strike, seconds, high, right, rate, dividend)["price"] < price:
        return None
    for _ in range(70):
        mid = (low+high)/2
        if bsm(spot, strike, seconds, mid, right, rate, dividend)["price"] < price:
            low = mid
        else:
            high = mid
    result = bsm(spot, strike, seconds, (low+high)/2, right, rate, dividend)
    return result if result["vega"] >= 1e-5 else None


def charm_per_minute(spot, strike, seconds, sigma, right, rate=.04, dividend=.012):
    """Elapsed-time derivative, scaled to 60s; positive = long-option delta grows.

    This is a local derivative, NOT a claim that delta will change linearly for 60s.
    """
    if seconds <= 10:
        return None
    step = min(1.0, seconds/10)
    before = bsm(spot, strike, seconds, sigma, right, rate, dividend)["delta"]
    after = bsm(spot, strike, seconds-step, sigma, right, rate, dividend)["delta"]
    return (after-before) * 60/step
