"""FirstLight: a pump.fun launch scalper for agent-fun/replay/v2.

Strategy (FirstLight)
---------------------
Watch every coin from its CreateEvent. In a coin's first 3 slots, read the
opening tape: the creator's own buy in the create slot, how many other
wallets bought with it, whether the creator has already sold, and how much
real SOL the curve holds. Buy only launches that pass all of these:

  * age <= 3 slots since creation (buy the first 3 slots, never chase);
  * real SOL reserves between 2 and 10 SOL at decision time;
  * creator's own buys in the create slot total at most 2.5 SOL;
  * at most 3 distinct non-creator buyers seen before entry;
  * the creator has not sold before entry.

One buy per coin, at most 0.1 SOL, sized down to the harness cap (1% of the
real SOL reserve) and never below 0.01 SOL (the guardrail's minimum round
trip). At most 5 open positions (the harness cap).

Exits: take profit at +15% over cost, stop loss at -12%, never hold past
5 slots, and leave at once when the creator sells after entry. The harness
closes a position when its curve completes and flattens the rest at the end.

Curve math follows the kit's own bonding-curve rules (harness curve.ts):
fees sit on top of the trade, 95 bps protocol + 30 bps creator, and an
exact-in buy of S splits into net0 = S*10000/10125 with the fees on top.
A sell of t tokens pays q - fee - creator_fee where q = t*vq/(vt+t).

Everything is a pure function of the batches: no clock, no environment, no
unseeded randomness, stdlib only. Run it as the bot (no arguments), or as a
local backtest over the kit's dev dataset:

    PYTHONHASHSEED=0 python3 -P -s main.py --backtest <dataset dir>

The backtest replays the dev window through the same on_batch with a local
fill simulator (2-slot latency, the same fee model) and prints an honest
JSON summary: net PnL, fills, distinct mints, round trips >= 0.01 SOL and
max drawdown.

Measured on the dev window (24 Nov 2026, grade-local --driver process, two
executions, L1 regrade reproduced byte for byte):
  net PnL -0.214 SOL (90% interval -0.345 .. -0.091), 318 fills over 159
  coins, 159 round trips >= 0.01 SOL, win rate 21.4%, max drawdown 2.7%,
  fees 0.161 SOL curve + 0.034 SOL tx. Guardrails met; ranked yes, prize no
  (net PnL below hold cash). The local --backtest agrees: -0.222 SOL,
  294 fills, 147 coins, 2.4% drawdown.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402  (after the sys.path line: -P leaves the script directory off sys.path)

LAMPORTS_PER_SOL = makefun_bot.LAMPORTS_PER_SOL
BPS = 10_000

# --- FirstLight parameters -------------------------------------------------
MAX_AGE = 3                    # buy only within 3 slots of a coin's creation
STAKE_LAMPORTS = 100_000_000   # 0.1 SOL target per coin
MIN_BUY_LAMPORTS = 10_000_000  # 0.01 SOL: the guardrail's minimum round trip
MIN_REAL_SOL = 2_000_000_000   # 2 SOL of real reserves at entry
MAX_REAL_SOL = 10_000_000_000  # 10 SOL of real reserves at entry
MAX_DEV_BUY = 2_500_000_000    # creator's own buys in the create slot, at most 2.5 SOL
MAX_BUYERS = 3                 # distinct non-creator buyers before entry, at most
TP_BPS = 1_500                 # take profit +15%
SL_BPS = 1_200                 # stop loss -12%
MAX_HOLD_SLOTS = 5             # never hold past 5 slots
BUY_SLIPPAGE_BPS = 10_000
SELL_SLIPPAGE_BPS = 10_000
PROTOCOL_FEE_BPS = 95
CREATOR_FEE_BPS = 30
CURVE_FEE_BPS = PROTOCOL_FEE_BPS + CREATOR_FEE_BPS


def ceil_div(a, b):
    return -(-a // b)


def sell_value(tokens, vq, vt):
    """What selling `tokens` against (vq, vt) pays after curve fees (95 + 30 bps)."""
    if tokens <= 0 or vt <= 0:
        return 0
    q = tokens * vq // (vt + tokens)
    return q - ceil_div(q * PROTOCOL_FEE_BPS, BPS) - ceil_div(q * CREATOR_FEE_BPS, BPS)


def exact_in_split(spendable):
    """Split an exact-in buy of `spendable` lamports (fees included) into curve quote q and fees."""
    net0 = spendable * BPS // (BPS + CURVE_FEE_BPS)
    fee = ceil_div(net0 * PROTOCOL_FEE_BPS, BPS)
    creator_fee = ceil_div(net0 * CREATOR_FEE_BPS, BPS)
    over = net0 + fee + creator_fee - spendable
    if over < 0:
        over = 0
    return net0 - over, fee, creator_fee


def buy_tokens_out(quote, vq, vt):
    """Tokens out for exactly `quote` lamports of curve quote (buy_exact_sol_in)."""
    if quote <= 1 or vq <= 0 or vt <= 0:
        return 0
    return (quote - 1) * vt // (vq + quote - 1)


class Coin:
    """A launch watched from its CreateEvent, with its opening tape."""

    __slots__ = (
        "mint", "create_slot", "creator", "dev_buy", "trades", "buyers",
        "dev_sold", "last_trade_slot", "buys_sent", "done", "complete",
    )

    def __init__(self, mint, create_slot, creator):
        self.mint = mint
        self.create_slot = create_slot
        self.creator = creator
        self.dev_buy = 0            # creator's own buys in the create slot, lamports
        self.trades = 0
        self.buyers = set()         # distinct non-creator buyers (membership only)
        self.dev_sold = False       # the creator sold at least once
        self.last_trade_slot = create_slot
        self.buys_sent = 0
        self.done = False           # never buy it again
        self.complete = False       # its CompleteEvent arrived


class Held:
    """An open position, as the bot's own books see it."""

    __slots__ = ("opened_slot", "tx_paid", "dev_sold_at_open")

    def __init__(self, opened_slot, tx_paid, dev_sold_at_open):
        self.opened_slot = opened_slot
        self.tx_paid = tx_paid
        self.dev_sold_at_open = dev_sold_at_open


class FirstLight:
    """The strategy: a pure function of the batches it is fed."""

    def __init__(self):
        self.coins = {}       # mint -> Coin, first-seen order
        self.young = {}       # mint -> Coin, still within MAX_AGE slots of creation
        self.held = {}        # mint -> Held
        self.selling = set()  # mints with a sell in flight

    # -- tape ----------------------------------------------------------------
    def read_events(self, batch):
        for e in batch["events"]:
            kind = e["event"]
            if kind == "CreateEvent":
                coin = Coin(e["mint"], e["slot"], e["creator"])
                self.coins[e["mint"]] = coin
                self.young[e["mint"]] = coin
            elif kind == "TradeEvent":
                coin = self.coins.get(e["mint"])
                if coin is None:
                    continue
                sol = int(e["sol_amount"])
                coin.trades += 1
                coin.last_trade_slot = e["slot"]
                is_dev = e["user"] == coin.creator
                if e["is_buy"]:
                    if is_dev and e["slot"] == coin.create_slot:
                        coin.dev_buy += sol
                    elif not is_dev:
                        coin.buyers.add(e["user"])
                elif is_dev:
                    coin.dev_sold = True
            else:  # CompleteEvent
                coin = self.coins.get(e["mint"])
                if coin is not None:
                    coin.complete = True
                    coin.done = True
                    self.young.pop(e["mint"], None)

    # -- helpers ---------------------------------------------------------------
    def _has_pending(self, state, mint, side):
        for p in state.pending.values():
            if p.order["mint"] == mint and p.order["side"] == side:
                return True
        return False

    def _mark(self, batch, mint, state):
        for m in batch["marks"]:
            if m["mint"] == mint:
                return int(m["virtual_quote_reserves"]), int(m["virtual_token_reserves"])
        curve = state.curves.get(mint)
        if curve is None:
            return None
        return curve.virtual_quote_reserves, curve.virtual_token_reserves

    # -- main ------------------------------------------------------------------
    def on_batch(self, state, batch):
        self.read_events(batch)
        tx = state.tx_fee_lamports()
        cap = state.hello["caps"]["max_orders_per_batch"]

        # Fold fills: harness closes drop the position, filled buys open one.
        for f in batch["fills"]:
            cid = f["client_id"]
            if cid.startswith("_"):
                self.held.pop(f["mint"], None)
                self.selling.discard(f["mint"])
                continue
            if f["status"] == "filled" and f["side"] == "buy":
                if f["mint"] not in self.held:
                    coin = self.coins.get(f["mint"])
                    self.held[f["mint"]] = Held(
                        f.get("slot", state.slot),
                        int(f.get("tx_fee_lamports", tx)),
                        coin.dev_sold if coin is not None else False,
                    )
            if f["status"] == "filled" and f["side"] == "sell":
                self.held.pop(f["mint"], None)
        for mint in list(self.selling):
            if not self._has_pending(state, mint, "sell"):
                self.selling.discard(mint)
        for mint in list(self.held):
            if mint not in state.positions and mint not in self.selling \
                    and not self._has_pending(state, mint, "buy"):
                self.held.pop(mint, None)

        orders = []

        # Exits first: TP +15%, SL -12%, max hold 5 slots, creator sold after entry.
        for mint, pos in state.positions.items():
            if len(orders) >= cap or mint in self.selling:
                continue
            held = self.held.get(mint)
            if held is None:
                coin = self.coins.get(mint)
                held = Held(state.slot, tx, coin.dev_sold if coin is not None else False)
                self.held[mint] = held
            mark = self._mark(batch, mint, state)
            if mark is None:
                continue
            vq, vt = mark
            value = sell_value(pos.token_amount, vq, vt) - tx
            basis = pos.cost_lamports + held.tx_paid
            coin = self.coins.get(mint)
            exit_now = False
            if basis > 0:
                if value * BPS >= basis * (BPS + TP_BPS):
                    exit_now = True
                elif value * BPS <= basis * (BPS - SL_BPS):
                    exit_now = True
            if state.slot - held.opened_slot >= MAX_HOLD_SLOTS:
                exit_now = True
            if coin is not None and coin.dev_sold and not held.dev_sold_at_open:
                exit_now = True
            if not exit_now:
                continue
            orders.append({"side": "sell", "mint": mint,
                           "token_amount": pos.token_amount,
                           "max_slippage_bps": SELL_SLIPPAGE_BPS})
            self.selling.add(mint)
            if coin is not None:
                coin.done = True
                self.young.pop(mint, None)

        # Entries: only coins still within their first 3 slots.
        free = state.free_cash_lamports() - tx * len(orders)
        open_slots = state.committed_position_count()
        for mint in list(self.young):
            if len(orders) >= cap:
                break
            coin = self.young[mint]
            age = state.slot - coin.create_slot
            if age > MAX_AGE or coin.done or coin.complete:
                coin.done = True
                self.young.pop(mint, None)
                continue
            if open_slots >= state.hello["caps"]["max_open_positions"]:
                continue
            if coin.buys_sent > 0 or mint in state.positions \
                    or self._has_pending(state, mint, "buy"):
                continue
            curve = state.curves.get(mint)
            if curve is None or curve.complete:
                continue
            if coin.dev_sold:
                coin.done = True
                self.young.pop(mint, None)
                continue
            rq = curve.real_quote_reserves
            if rq < MIN_REAL_SOL or rq > MAX_REAL_SOL:
                continue
            if coin.dev_buy > MAX_DEV_BUY:
                continue
            if len(coin.buyers) > MAX_BUYERS:
                continue
            amount = min(STAKE_LAMPORTS, state.max_buy_lamports(mint))
            if amount < MIN_BUY_LAMPORTS or free < amount + tx:
                continue
            orders.append({"side": "buy", "mint": mint, "quote_lamports": amount,
                           "max_slippage_bps": BUY_SLIPPAGE_BPS})
            free -= amount + tx
            open_slots += 1
            coin.buys_sent += 1
        return orders


STRATEGY = FirstLight()


def on_batch(state, batch):
    return STRATEGY.on_batch(state, batch)


def on_done(state, done):
    cash = state.cash
    value = 0
    for mint, pos in state.positions.items():
        curve = state.curves.get(mint)
        if curve is not None:
            value += sell_value(pos.token_amount,
                                curve.virtual_quote_reserves,
                                curve.virtual_token_reserves)
    print(f"[firstlight] done: cash={cash} open_value={value} "
          f"equity={cash + value}", file=sys.stderr)


# --- local backtest -----------------------------------------------------------

def _load_events(dataset_dir):
    """All dev-window events, in (slot, chain_seq) order."""
    import gzip
    import glob
    events = []
    for path in sorted(glob.glob(os.path.join(dataset_dir, "events", "*.ndjson.gz"))):
        with gzip.open(path, "rt") as f:
            for line in f:
                events.append(json.loads(line))
    events.sort(key=lambda e: (e["slot"], e.get("chain_seq", 0)))
    return events


def backtest(argv):
    """Replay the dev dataset through the strategy with a local fill simulator.

    Prints one JSON summary to stdout: net PnL, fills, distinct mints, round
    trips >= 0.01 SOL and max drawdown. The simulator applies market events
    to the curves, executes the bot's orders 2 slots after they are decided
    (the harness latency), charges the harness fees (95 + 30 bps on top of
    the trade, 5,000 + 100,000 lamports per order), closes positions on
    CompleteEvent and flattens the rest at the end of the window.
    """
    dataset_dir = argv[0] if argv else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "dataset")
    events = _load_events(dataset_dir)
    by_slot = {}
    for e in events:
        by_slot.setdefault(e["slot"], []).append(e)
    first_slot = events[0]["slot"]
    last_slot = events[-1]["slot"]

    hello = {
        "type": "hello",
        "protocol": "agent-fun/replay/v2",
        "capital": {"lamports": "10000000000"},
        "caps": {"max_open_positions": 5, "max_order_bps_of_real_reserve": 100,
                 "max_orders_per_batch": 16},
        "fees": {"base_fee_lamports": "5000", "priority_fee_lamports": "100000",
                 "protocol_bps_fallback": 95, "creator_bps_fallback": 30},
        "seed": "0",
    }
    tx_fee = 105_000
    strategy = FirstLight()
    state = makefun_bot.BotState(hello)

    # Simulator books: mint -> [vq, vt, rq, rt, complete]
    book = {}
    cash = 10_000_000_000
    positions = {}   # mint -> [tokens, cost_lamports]
    pending = []     # (fill_slot, order dict) decided, not yet executed
    seq = 0
    fills_done = 0
    distinct_mints = set()
    round_trips = set()
    bought_big = set()  # mints with a filled buy >= 0.01 SOL
    peak_equity = cash
    max_dd = 0.0
    close_n = 0

    def equity_now():
        total = cash
        for mint, (tokens, _cost) in positions.items():
            curve = book.get(mint)
            if curve is not None:
                total += sell_value(tokens, curve[0], curve[1]) - tx_fee
        return total

    def close_position(mint, tag):
        nonlocal cash, close_n
        tokens, _cost = positions.pop(mint)
        curve = book.get(mint)
        if curve is None or tokens <= 0:
            return None
        vq, vt = curve[0], curve[1]
        q = tokens * vq // (vt + tokens)
        proceeds = q - ceil_div(q * PROTOCOL_FEE_BPS, BPS) - ceil_div(q * CREATOR_FEE_BPS, BPS)
        cash += proceeds - tx_fee
        curve[0] -= q
        curve[1] += tokens
        curve[2] -= q
        curve[3] += tokens
        close_n += 1
        if mint in bought_big:
            round_trips.add(mint)
        return {"type": "fill", "client_id": f"{tag}{close_n}", "mint": mint,
                "side": "sell", "status": "filled", "slot": slot,
                "tx_fee_lamports": str(tx_fee)}

    # Slots worth processing: event slots, fill slots, and slots where an open
    # position may hit its 5-slot max hold. Event slots are walked with an
    # index (bisect), never a linear scan, or the replay would be quadratic.
    import bisect
    event_slots = sorted(by_slot)
    slot = first_slot
    while slot <= last_slot:
        slot_events = by_slot.get(slot, [])
        # 1. Market events move the curves (post-trade reserves are in the event).
        touched = set()
        for e in slot_events:
            kind = e["event"]
            if kind == "CreateEvent":
                book[e["mint"]] = [int(e["virtual_quote_reserves"]),
                                   int(e["virtual_token_reserves"]), 0,
                                   int(e["real_token_reserves"]), False]
                touched.add(e["mint"])
            elif kind == "TradeEvent":
                curve = book.get(e["mint"])
                if curve is None:
                    curve = [int(e["virtual_sol_reserves"]),
                             int(e["virtual_token_reserves"]), 0,
                             int(e["real_token_reserves"]), False]
                    book[e["mint"]] = curve
                curve[0] = int(e["virtual_sol_reserves"])
                curve[1] = int(e["virtual_token_reserves"])
                curve[2] = int(e["real_sol_reserves"])
                curve[3] = int(e["real_token_reserves"])
                touched.add(e["mint"])
            else:  # CompleteEvent
                curve = book.get(e["mint"])
                if curve is not None:
                    curve[4] = True
                touched.add(e["mint"])
        # 2. Fills due this slot (decided 2 slots ago).
        fills = []
        still_pending = []
        for fill_slot, order in pending:
            if fill_slot != slot:
                still_pending.append((fill_slot, order))
                continue
            mint = order["mint"]
            curve = book.get(mint)
            if order["side"] == "buy":
                amount = int(order["quote_lamports"])
                cap = 0 if curve is None or curve[4] else curve[2] * 100 // BPS
                if curve is None or curve[4] or amount > cap:
                    fills.append({"type": "fill", "client_id": order["client_id"],
                                  "mint": mint, "side": "buy", "status": "rejected",
                                  "slot": slot, "tx_fee_lamports": str(tx_fee),
                                  "reason": "token_complete" if curve is not None and curve[4]
                                  else "max_order_size"})
                    cash -= tx_fee
                    continue
                q, _fee, _creator = exact_in_split(amount)
                tokens = buy_tokens_out(q, curve[0], curve[1])
                if tokens <= 0:
                    fills.append({"type": "fill", "client_id": order["client_id"],
                                  "mint": mint, "side": "buy", "status": "rejected",
                                  "slot": slot, "tx_fee_lamports": str(tx_fee),
                                  "reason": "zero_amount"})
                    cash -= tx_fee
                    continue
                curve[0] += q
                curve[1] -= tokens
                curve[2] += q
                curve[3] -= tokens
                cash -= amount + tx_fee
                held = positions.get(mint)
                if held is None:
                    positions[mint] = [tokens, amount]
                else:
                    held[0] += tokens
                    held[1] += amount
                fills_done += 1
                distinct_mints.add(mint)
                if amount >= MIN_BUY_LAMPORTS:
                    bought_big.add(mint)
                fills.append({"type": "fill", "client_id": order["client_id"],
                              "mint": mint, "side": "buy", "status": "filled",
                              "slot": slot, "tx_fee_lamports": str(tx_fee)})
            else:  # sell
                tokens = int(order["token_amount"])
                held = positions.get(mint)
                if held is None or held[0] < tokens or curve is None:
                    fills.append({"type": "fill", "client_id": order["client_id"],
                                  "mint": mint, "side": "sell", "status": "rejected",
                                  "slot": slot, "tx_fee_lamports": str(tx_fee),
                                  "reason": "insufficient_tokens"})
                    cash -= tx_fee
                    continue
                q = tokens * curve[0] // (curve[1] + tokens)
                proceeds = q - ceil_div(q * PROTOCOL_FEE_BPS, BPS) \
                    - ceil_div(q * CREATOR_FEE_BPS, BPS)
                curve[0] -= q
                curve[1] += tokens
                curve[2] -= q
                curve[3] += tokens
                cash += proceeds - tx_fee
                held[0] -= tokens
                if held[0] <= 0:
                    positions.pop(mint, None)
                    if mint in bought_big:
                        round_trips.add(mint)
                fills_done += 1
                fills.append({"type": "fill", "client_id": order["client_id"],
                              "mint": mint, "side": "sell", "status": "filled",
                              "slot": slot, "tx_fee_lamports": str(tx_fee)})
        pending = still_pending
        # 3. Harness closes: positions on completed curves close at once.
        for e in slot_events:
            if e["event"] == "CompleteEvent" and e["mint"] in positions:
                record = close_position(e["mint"], "_complete_")
                if record is not None:
                    fills.append(record)
        # 4. Drive the strategy on this batch.
        marks = []
        for mint in touched:
            curve = book.get(mint)
            if curve is not None:
                marks.append({"mint": mint,
                              "virtual_quote_reserves": str(curve[0]),
                              "virtual_token_reserves": str(curve[1]),
                              "real_quote_reserves": str(curve[2]),
                              "real_token_reserves": str(curve[3]),
                              "slot": slot})
        for mint in positions:
            if mint not in touched:
                curve = book.get(mint)
                if curve is not None:
                    marks.append({"mint": mint,
                                  "virtual_quote_reserves": str(curve[0]),
                                  "virtual_token_reserves": str(curve[1]),
                                  "real_quote_reserves": str(curve[2]),
                                  "real_token_reserves": str(curve[3]),
                                  "slot": slot})
        account = {"cash_lamports": str(cash),
                   "positions": [{"mint": m, "token_amount": str(t), "cost_lamports": str(c)}
                                 for m, (t, c) in positions.items()]}
        seq += 1
        batch = {"type": "batch", "seq": seq, "slot": slot,
                 "events": slot_events, "marks": marks, "fills": fills,
                 "account": account}
        state.apply_batch(batch)
        orders = state.to_orders(strategy.on_batch(state, batch))
        state.record_orders(orders)
        for order in orders:
            pending.append((slot + 2, order))
        # 5. Drawdown on mark-to-market equity.
        eq = equity_now()
        if eq > peak_equity:
            peak_equity = eq
        elif peak_equity > 0:
            dd = (peak_equity - eq) / peak_equity
            if dd > max_dd:
                max_dd = dd
        # Advance: step one slot while a position is open or a fill is due
        # (a position lives at most 5 slots), else jump to the next event
        # slot or the next due fill, whichever comes first.
        if positions or pending:
            slot += 1
            continue
        candidates = []
        i = bisect.bisect_right(event_slots, slot)
        if i < len(event_slots):
            candidates.append(event_slots[i])
        if pending:
            candidates.append(min(fs for fs, _o in pending))
        if not candidates:
            break
        slot = min(candidates)
    # Flatten whatever is left at the end of the window.
    for mint in list(positions):
        close_position(mint, "_flatten_")
    pnl = cash - 10_000_000_000
    print(json.dumps({
        "net_pnl_lamports": pnl,
        "net_pnl_sol": pnl / LAMPORTS_PER_SOL,
        "fills": fills_done,
        "distinct_mints": len(distinct_mints),
        "round_trips_ge_0.01_sol": len(round_trips),
        "max_drawdown": round(max_dd, 6),
        "final_cash_lamports": cash,
        "events": len(events),
    }, indent=2))
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--backtest":
        raise SystemExit(backtest(sys.argv[2:]))
    makefun_bot.run(on_batch, on_done=on_done)
