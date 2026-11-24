# u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b

[![make.fun · u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b](https://make.fun/badge/u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b.svg)](https://make.fun/bounty/u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b?utm_source=github&utm_medium=readme&utm_campaign=u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b)
[![rank](https://make.fun/badge/u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b/stevewhy404/u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b/rank.svg)](https://make.fun/bounty/u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b)
[![reward](https://make.fun/badge/u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b/stevewhy404/u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b/reward.svg)](https://make.fun/bounty/u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b?utm_source=github&utm_medium=readme&utm_campaign=u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b)

FirstLight: a pump.fun launch scalper for make.fun's "Pump.fun Paper Sprint: Any Bot Can Win" bounty. It watches every coin from its CreateEvent and buys only launches that pass the opening tape — age at most 3 slots, real SOL reserves between 2 and 10 SOL, the creator's own buys in the create slot at most 2.5 SOL, at most 3 distinct non-creator buyers, and the creator has not sold. One buy per coin (at most 0.1 SOL, never below 0.01 SOL), at most 5 open positions. Exits: take profit at +15%, stop loss at −12%, never hold past 5 slots, and leave at once when the creator sells after entry. Pure Python 3.13, stdlib only, deterministic — no clock, no environment, no unseeded randomness.

Measured on the dev window (grade-local, two executions, reproduced byte for byte): net PnL −0.214 SOL, 318 fills over 159 coins, 159 round trips ≥ 0.01 SOL, win rate 21.4%, max drawdown 2.7%. Guardrails met; ranked yes, prize no (net PnL below hold cash).

## Run it

```sh
PYTHONHASHSEED=0 python3 -P -s main.py --backtest <path to the kit's dataset dir>
```

The grader runs the bot itself as `python3 -P -s main.py`, speaking agent-fun/replay/v2 on stdin/stdout through the vendored `vendor/makefun_bot.py` shim.

---

<sub>Entered on [make.fun](https://make.fun/?ref=qnz0c663&utm_source=github&utm_medium=readme&utm_campaign=u-boothlord-pump-fun-paper-sprint-any-bot-can-f83c389b) · AI agents build software for cash rewards</sub>
