"""Crease IQ P3 — the shot-quality (xG) model over nhl_shot_events.

The P2 verdict: raw shot-count sv% can't separate NHL goalies — the
published models run xG built from shot LOCATION. This fits a logistic
P(goal | unblocked attempt) on distance / angle / shot type / strength
state, pure Python (IRLS — Newton converges in ~8 passes, no sklearn).

Conventions:
- Rink x ∈ [-100,100], goals at x = ±89. Attack side varies by team/
  period, so geometry uses the NEAREST goal: dist = hypot(89−|x|, y),
  angle = atan2(|y|, 89−|x|) (behind the net ⇒ angle > π/2). This is
  the standard public-xG simplification and is side-agnostic.
- Strength orientation comes from the GOALIE's side: situationCode is
  "[away goalie][away skaters][home skaters][home goalie]"; the goalie
  in net defends, so if the goalie is the HOME goalie the shooter is
  the away side. skater_diff = shooter skaters − defender skaters
  (+1 power play, 0 even, −1 shorthanded).
- EMPTY-NET attempts (goalie_id null) are EXCLUDED from the fit and
  from every goalie/team aggregate — a 6-on-5 empty-netter is not a
  goalie-quality observation and its ~90% goal rate poisons the fit.

Fit on the TRAIN season only, frozen for eval — shot physics is stable,
season-grain freezing keeps the backtest honest.
"""
from __future__ import annotations

import math

_SHOT_TYPES = ("wrist", "slap", "snap", "backhand", "tip-in",
               "deflected", "wrap-around")


def shot_features(x: float | None, y: float | None,
                  shot_type: str | None,
                  skater_diff: int) -> list[float] | None:
    """Feature vector for one unblocked attempt; None = no coordinates."""
    if x is None or y is None:
        return None
    dx = 89.0 - abs(float(x))
    dy = float(y)
    dist = math.hypot(dx, dy)
    angle = math.atan2(abs(dy), dx)          # >π/2 = behind the goal line
    st = (shot_type or "").strip().lower()
    onehot = [1.0 if st == t else 0.0 for t in _SHOT_TYPES]
    return [dist, dist * dist / 100.0, angle,
            float(max(-2, min(2, skater_diff)))] + onehot


def skater_diff_from_situation(situation: str | None,
                               goalie_is_home: bool | None) -> int:
    """Shooter-minus-defender skater count from the 4-char situationCode.
    Unknown/odd codes → 0 (even strength)."""
    s = str(situation or "")
    if len(s) != 4 or not s.isdigit() or goalie_is_home is None:
        return 0
    away_sk, home_sk = int(s[1]), int(s[2])
    # goalie defends: home goalie in net ⇒ shooter is the away side
    if goalie_is_home:
        return away_sk - home_sk
    return home_sk - away_sk


class XGModel:
    """Standardized-feature logistic: P(goal) per unblocked attempt."""

    def __init__(self, weights: list[float], means: list[float],
                 stds: list[float]):
        self.w = weights                     # [bias] + per-feature
        self.means = means
        self.stds = stds

    def predict(self, feats: list[float]) -> float:
        z = self.w[0]
        for j, f in enumerate(feats):
            sd = self.stds[j]
            z += self.w[j + 1] * ((f - self.means[j]) / sd if sd > 0 else 0.0)
        if z < -30:
            return 1e-9
        if z > 30:
            return 1.0 - 1e-9
        return 1.0 / (1.0 + math.exp(-z))


def fit_xg(samples: list[tuple[list[float], int]],
           iters: int = 10, ridge: float = 1e-4) -> XGModel | None:
    """IRLS logistic fit. samples = [(features, is_goal)]. None on
    degenerate input. Pure Python — ~450k × 12 features × 10 Newton
    passes runs in a couple of minutes on an Actions runner."""
    n = len(samples)
    if n < 1000:
        return None
    k = len(samples[0][0])
    means = [0.0] * k
    for f, _y in samples:
        for j in range(k):
            means[j] += f[j]
    means = [m / n for m in means]
    var = [0.0] * k
    for f, _y in samples:
        for j in range(k):
            d = f[j] - means[j]
            var[j] += d * d
    stds = [math.sqrt(v / n) if v > 0 else 0.0 for v in var]
    X = []
    for f, _y in samples:
        X.append([1.0] + [((f[j] - means[j]) / stds[j] if stds[j] > 0 else 0.0)
                          for j in range(k)])
    y = [float(s[1]) for s in samples]
    p_dim = k + 1
    w = [0.0] * p_dim
    w[0] = math.log(max(sum(y), 1.0) / max(n - sum(y), 1.0))   # base-rate bias
    for _it in range(iters):
        # gradient + Hessian (X'WX) in one pass
        grad = [0.0] * p_dim
        hess = [[ridge * (1.0 if a == b else 0.0) for b in range(p_dim)]
                for a in range(p_dim)]
        for i in range(n):
            xi = X[i]
            z = 0.0
            for j in range(p_dim):
                z += w[j] * xi[j]
            if z < -30:
                p = 1e-9
            elif z > 30:
                p = 1.0 - 1e-9
            else:
                p = 1.0 / (1.0 + math.exp(-z))
            e = p - y[i]
            wt = max(p * (1.0 - p), 1e-6)
            for a in range(p_dim):
                grad[a] += e * xi[a]
                xa = xi[a] * wt
                row = hess[a]
                for b in range(a, p_dim):
                    row[b] += xa * xi[b]
        for a in range(p_dim):              # mirror the upper triangle
            for b in range(a + 1, p_dim):
                hess[b][a] = hess[a][b]
        step = _solve(hess, grad)
        if step is None:
            break
        moved = 0.0
        for j in range(p_dim):
            w[j] -= step[j]
            moved += abs(step[j])
        if moved < 1e-6:
            break
    return XGModel(w, means, stds)


def _solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting (small dense system)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            return None
        m[col], m[piv] = m[piv], m[col]
        pv = m[col][col]
        for r in range(col + 1, n):
            f = m[r][col] / pv
            if f == 0.0:
                continue
            for c in range(col, n + 1):
                m[r][c] -= f * m[col][c]
    out = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = m[r][n]
        for c in range(r + 1, n):
            s -= m[r][c] * out[c]
        out[r] = s / m[r][r]
    return out


def calibration_table(model: XGModel,
                      samples: list[tuple[list[float], int]],
                      bins: int = 10) -> list[str]:
    """Decile calibration: mean predicted xG vs realized goal rate."""
    scored = sorted(((model.predict(f), y) for f, y in samples),
                    key=lambda t: t[0])
    n = len(scored)
    out = []
    for b in range(bins):
        chunk = scored[b * n // bins:(b + 1) * n // bins]
        if not chunk:
            continue
        mp = sum(p for p, _ in chunk) / len(chunk)
        mr = sum(y for _, y in chunk) / len(chunk)
        out.append(f"decile {b + 1}: pred {mp:.3f} vs real {mr:.3f} "
                   f"(n={len(chunk)})")
    return out
