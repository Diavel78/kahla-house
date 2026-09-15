"""hedge_calc — pair economics for a $1 binary contract held on one venue and mirrored on another.

Rob, Sep 14 2026: "you'll need to build a calculator to figure out the amount to hedge based
on the mirror contract touch... repeg might have to adjust contract sizes."

The one fact the whole file rests on: for $1 binaries the EVEN hedge is 1:1 in contracts.
Long q YES at p1 + long q NO at p2 pays q − q(p1+p2) on EITHER outcome. Prices set the locked
P&L; they never change the ratio. Any q2 ≠ q1 re-opens directional exposure. So the calculator
answers PRICE questions (what may the mirror cost, where does it break even, what does a
partial fill leave un-hedged), and sizing tracks FILLS, not touches.

Pure functions, no I/O — unit-tested inline (`python3 hedge_calc.py`).
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class Leg:
    venue: str            # "poly" | "gemini"
    outcome: str          # "yes" | "no" — the side we HOLD / are bidding for, in the SAME contract frame
    price: float          # our cost (filled) or resting bid, in dollars (0-1), in that outcome's own terms
    qty_filled: float = 0.0
    qty_resting: float = 0.0
    fee_per_contract: float = 0.0   # maker fee actually charged per contract (Gemini: 0 today; Poly: sub-cent)


@dataclass
class PairPlan:
    hedge_outcome: str            # outcome the mirror leg must be long
    hedge_qty_now: float          # contracts to hold on the mirror to be flat against FILLED primary
    hedge_qty_rest: float         # contracts to REST on the mirror against the primary's still-resting qty
    breakeven_price: float        # max mirror price at which the pair locks ≥ 0 (after fees)
    rest_price: Optional[float]   # where to rest the mirror bid (touch join / lead), None if no book
    locked_if_rest_fills: Optional[float]   # locked $ per contract if the mirror rests and fills
    take_price: Optional[float]   # crossing price on the mirror right now
    locked_if_take: Optional[float]
    shortfall_qty: float          # primary filled − mirror filled: the un-hedged exposure right now
    note: str


def opposite(outcome: str) -> str:
    return "no" if outcome == "yes" else "yes"


def locked_pnl(p_primary: float, p_hedge: float, fees: float = 0.0) -> float:
    """$ per contract locked when both legs are held 1:1 (payout $1 either way)."""
    return round(1.0 - p_primary - p_hedge - fees, 4)


def mirror_book(yes_bid: Optional[float], yes_ask: Optional[float], outcome: str) -> tuple[Optional[float], Optional[float]]:
    """(best_bid, best_ask) for `outcome` given the YES book. NO bid = 1 − YES ask, NO ask = 1 − YES bid."""
    if outcome == "yes":
        return yes_bid, yes_ask
    nb = None if yes_ask is None else round(1.0 - yes_ask, 4)
    na = None if yes_bid is None else round(1.0 - yes_bid, 4)
    return nb, na


def rest_quote(best_bid: Optional[float], best_ask: Optional[float], tick: float = 0.01,
               join: bool = True) -> Optional[float]:
    """Where a resting bid sits: join the touch (Rob's football rule) or lead by one tick,
    never crossing the ask. No bid on the book → one tick under the ask if any, else None."""
    if best_bid is None and best_ask is None:
        return None
    if best_bid is None:
        return round(best_ask - tick, 4) if best_ask and best_ask - tick > 0 else None
    px = best_bid if join else round(best_bid + tick, 4)
    if best_ask is not None and px >= best_ask:
        px = best_bid
    return round(px, 4)


def plan(primary: Leg, mirror_yes_bid: Optional[float], mirror_yes_ask: Optional[float],
         tick: float = 0.01, join: bool = True, mirror_filled: float = 0.0,
         mirror_fee: float = 0.0) -> PairPlan:
    """Given the primary leg and the mirror contract's YES book, what should the mirror do."""
    h_out = opposite(primary.outcome)
    bb, ba = mirror_book(mirror_yes_bid, mirror_yes_ask, h_out)
    fees = primary.fee_per_contract + mirror_fee
    be = round(1.0 - primary.price - fees, 4)
    rp = rest_quote(bb, ba, tick, join)
    # The mirror's TOTAL target is primary filled + primary resting. Whatever the mirror already
    # holds counts against that total first (the mirror filled first ⇒ surplus ⇒ rest LESS, never more).
    surplus = max(0.0, mirror_filled - primary.qty_filled)
    plan_ = PairPlan(
        hedge_outcome=h_out,
        hedge_qty_now=max(0.0, round(primary.qty_filled - mirror_filled, 4)),
        hedge_qty_rest=max(0.0, round(primary.qty_resting - surplus, 4)),
        breakeven_price=be,
        rest_price=rp,
        locked_if_rest_fills=None if rp is None else locked_pnl(primary.price, rp, fees),
        take_price=ba,
        locked_if_take=None if ba is None else locked_pnl(primary.price, ba, fees),
        shortfall_qty=max(0.0, round(primary.qty_filled - mirror_filled, 4)),
        note="",
    )
    notes = []
    if rp is not None and rp > be:
        notes.append(f"resting at {rp:.2f} locks a LOSS ({plan_.locked_if_rest_fills:+.3f}/contract); breakeven {be:.2f}")
    if plan_.shortfall_qty > 0:
        notes.append(f"UN-HEDGED {plan_.shortfall_qty:g} contracts: primary filled, mirror not")
    plan_.note = "; ".join(notes) or "ok"
    return plan_


def size_for_fill(primary_filled: float, mirror_filled: float, primary_resting: float) -> dict:
    """The repeg's sizing rule. The mirror should HOLD what the primary has FILLED and REST what the
    primary is still RESTING. Returns the order to place/replace on the mirror side."""
    hold_gap = max(0.0, primary_filled - mirror_filled)
    return {"urgent_qty": round(hold_gap, 4),          # close this at/near the ask — exposure exists NOW
            "rest_qty": round(primary_resting, 4)}     # rest this at the touch — earns rent, no exposure yet


if __name__ == "__main__":
    # BUF -1.5 held on Poly at 59.3¢ (20 resting); Gemini "Buffalo wins by more than 1.5" YES book 0.61/0.65
    L = Leg("poly", "yes", 0.593, qty_filled=0, qty_resting=20)
    P = plan(L, 0.61, 0.65)
    assert P.hedge_outcome == "no"
    assert P.rest_price == 0.35 and P.take_price == 0.39, P
    assert abs(P.locked_if_rest_fills - 0.057) < 1e-9 and abs(P.locked_if_take - 0.017) < 1e-9
    assert P.hedge_qty_rest == 20 and P.hedge_qty_now == 0
    # after Poly fills 12 of 20 and the mirror has 5
    L2 = Leg("poly", "yes", 0.593, qty_filled=12, qty_resting=8)
    P2 = plan(L2, 0.61, 0.65, mirror_filled=5)
    assert P2.hedge_qty_now == 7 and P2.hedge_qty_rest == 8 and P2.shortfall_qty == 7
    assert size_for_fill(12, 5, 8) == {"urgent_qty": 7, "rest_qty": 8}
    # under 52.5 on Poly at 49¢; Gemini over book 0.53/0.54 → hedge = YES(over), rest 0.53 → pair 1.02
    P3 = plan(Leg("poly", "no", 0.49, qty_resting=20), 0.53, 0.54)
    assert P3.hedge_outcome == "yes" and P3.rest_price == 0.53 and P3.locked_if_rest_fills < 0
    assert "LOSS" in P3.note
    # mirror filled FIRST: Poly still resting 20, Gemini already holds 20 → rest NOTHING more
    P4 = plan(Leg("poly", "no", 0.545, qty_filled=0, qty_resting=20), 0.44, 0.45, mirror_filled=20)
    assert P4.hedge_qty_now == 0 and P4.hedge_qty_rest == 0, P4
    # partial both ways: Poly filled 8 / resting 12, Gemini holds 5 → urgent 3, rest 12 (total 20)
    P5 = plan(Leg("poly", "no", 0.545, qty_filled=8, qty_resting=12), 0.44, 0.45, mirror_filled=5)
    assert P5.hedge_qty_now == 3 and P5.hedge_qty_rest == 12, P5
    # Gemini over-filled vs Poly partial: Poly filled 8 / resting 12, Gemini holds 15 → urgent 0, rest 5
    P6 = plan(Leg("poly", "no", 0.545, qty_filled=8, qty_resting=12), 0.44, 0.45, mirror_filled=15)
    assert P6.hedge_qty_now == 0 and P6.hedge_qty_rest == 5, P6
    # 1:1 invariance: locked pnl identical on both outcomes by construction
    q, p1, p2 = 20, 0.593, 0.35
    assert abs((q - q*p1 - q*p2) - (q*locked_pnl(p1, p2))) < 1e-9
    print("hedge_calc ok:", asdict(P))
