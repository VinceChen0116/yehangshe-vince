class Window:
    """Capacity includes retiring contracts. Cancel before adding replacements."""
    def __init__(self, budget, n=19, grace=60):
        self.budget, self.n, self.grace = budget, n, grace
        self.active = {}  # (strike,right) -> outside-since or None

    def reset(self):
        self.active.clear()

    def update(self, spot, strikes, now):
        if not strikes or not spot:
            return [], [], set()
        near = min(range(len(strikes)), key=lambda i: abs(strikes[i]-spot))
        lo = max(0, near-self.n)
        hi = min(len(strikes), near+self.n+1)
        wanted = {(k, r) for k in strikes[lo:hi] for r in ("C", "P")}
        removed = []
        for key, outside in list(self.active.items()):
            if key in wanted:
                self.active[key] = None
            elif outside is None:
                self.active[key] = now
            elif now-outside >= self.grace:
                removed.append(key)
                del self.active[key]
        added = []
        pending = sorted(wanted-self.active.keys(), key=lambda k: (abs(k[0]-spot), k))
        for key in pending[:max(0, self.budget-len(self.active))]:
            self.active[key] = None
            added.append(key)
        return removed, added, wanted
