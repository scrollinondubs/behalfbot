"""Score the router over multi-turn conversations - the deployment condition.

The single-utterance evals ask an unfair question: they hand the router a line
like "did it come back clear" with nothing before it, which no human could route
either. This harness replays whole conversations, feeding each turn the history
and the previous routing decision exactly as the live bot does, and reports the
same scoreboard.

    .venv/bin/python eval_router_conversations.py --model qwen2.5:7b-instruct
    .venv/bin/python eval_router_conversations.py --ablate   # isolate each layer
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from pathlib import Path

import httpx

import router
from eval_router import ORDER
from router import Route

PATH = Path(__file__).parent / "router_cases_conversations.json"


async def run(convos, model: str, *, history: bool, sticky: bool, prefilter: bool,
              model2: str = ""):
    """Replay every conversation turn by turn. Returns flat per-turn results."""
    results = []
    async with httpx.AsyncClient() as client:
        for mdl in {model, router.second_model(model2) if model2 else None} - {None}:
            await router.classify_llm("warm up the prompt cache", client,
                                      model=mdl, timeout=180.0)
        await router.route("warm up the prompt cache", client, model=model,
                           prefilter=False, model2=model2)
        for convo in convos:
            hist: list[dict] = []
            last: Route | None = None
            for turn in convo["turns"]:
                d = await router.route(
                    turn["user"], client, model=model, prefilter=prefilter,
                    history=hist if history else None,
                    last_route=last, sticky=sticky, model2=model2,
                )
                # An `ask` decision sends nothing. The bot reads the question
                # back and waits, so scoring it as though it had routed
                # somewhere measures a disclosure that cannot happen. Recorded
                # as ASK, and the conversation continues as though Sean answered
                # with the label - which is the premise the design rests on, and
                # is what makes the follow-ups stick.
                got = "ASK" if d.ask else d.route.value
                results.append({"convo": convo["name"], "text": turn["user"],
                                "expect": turn["expect"], "got": got,
                                "source": d.source, "ms": d.latency_ms})
                last = Route(turn["expect"]) if d.ask else d.route
                hist.append({"role": "user", "content": turn["user"]})
                # The bot's own reply is not available offline. A placeholder
                # keeps the turn alternation the classifier expects without
                # inventing content that would bias the decision.
                hist.append({"role": "assistant", "content": "(answered)"})
    return results


def score(results, label: str, show_wrong: bool = True):
    n = len(results)
    correct = sum(("HELD" if r["expect"] == "JILL" else r["expect"]) == r["got"]
                  for r in results)
    # "ASK" gets its own column: the turn was held and the destination question
    # put back to Sean, so nothing was sent and nothing leaked. Folding it into
    # JILL would claim a route that did not happen; folding it into JAX would
    # report a disclosure that did not happen either.
    cols = [r.value for r in ORDER] + ["ASK"]
    m = {a.value: {b: 0 for b in cols} for a in ORDER}
    for r in results:
        # The case files were written when JILL was a destination. The label now
        # means "this must not be escalated", which is exactly HELD, so it is
        # translated here rather than rewritten across four JSON files - the
        # labels are still correct about which turns are private.
        expect = "HELD" if r["expect"] == "JILL" else r["expect"]
        m[expect][r["got"]] += 1
    asked = sum(m[a.value]["ASK"] for a in ORDER)

    jill_total = sum(m["HELD"].values())
    leaked = m["HELD"]["JAX"] + m["HELD"]["CHAT"]
    over = m["CHAT"]["HELD"] + m["JAX"]["HELD"]
    non_jill = sum(m["CHAT"].values()) + sum(m["JAX"].values())
    calls = [r["ms"] for r in results if r["source"] == "model"]

    print(f"\n=== {label} ===")
    print(f"accuracy            {correct}/{n} = {correct / n:.1%}")
    print(f"SENSITIVE FN RATE   {leaked}/{jill_total} = {leaked / jill_total:.1%}")
    # Split by direction: JILL -> JAX reaches Anthropic, JILL -> CHAT does not
    # leave the box at all. See the same comment in eval_router.py.
    print(f"  off the box       {m['HELD']['JAX']}/{jill_total} = "
          f"{m['HELD']['JAX'] / jill_total:.1%}")
    print(f"  answered locally  {m['HELD']['CHAT']}/{jill_total} = "
          f"{m['HELD']['CHAT'] / jill_total:.1%}")
    print(f"wrongly refused    {over}/{non_jill} = {over / non_jill:.1%}")
    if asked:
        print(f"held and asked      {asked}/{n} = {asked / n:.1%}   "
              f"<- nothing sent; Sean names the destination")
    if calls:
        print(f"classifier latency  median {statistics.median(calls):.0f} ms  n={len(calls)}")
    print("\n            got ->")
    print("expect      " + "".join(f"{c:>7}" for c in cols))
    for a in ORDER:
        print(f"{a.value:<12}" + "".join(f"{m[a.value][c]:>7}" for c in cols))

    if show_wrong:
        wrong = [r for r in results if r["expect"] != r["got"]]
        if wrong:
            print(f"\n{len(wrong)} misroutes:")
            for r in wrong:
                flag = "LEAK " if r["expect"] in ("JILL", "HELD") else "     "
                print(f"  {flag}{r['expect']} -> {r['got']:<5} [{r['source']:<9}] "
                      f"{r['convo']}: {r['text']}")
    return {"label": label, "accuracy": correct / n, "asked": asked,
            "sensitive_fn_rate": leaked / jill_total,
            "jill_to_jax_rate": m["HELD"]["JAX"] / jill_total,
            "over_escalation_rate": over / non_jill,
            "median_ms": statistics.median(calls) if calls else None}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=router.ROUTER_MODEL)
    ap.add_argument("--model2", default="",
                    help="second voter; 'off' for the Phase 2 single-model router")
    ap.add_argument("--sweep", nargs="+")
    ap.add_argument("--sweep2", nargs="+",
                    help="compare second voters against the fixed primary")
    ap.add_argument("--ablate", action="store_true",
                    help="score every combination of history / stickiness / keyword net")
    args = ap.parse_args()

    convos = json.loads(PATH.read_text())["conversations"]
    turns = sum(len(c["turns"]) for c in convos)
    print(f"{len(convos)} conversations, {turns} turns")

    configs = [("history+sticky+prefilter", True, True, True)]
    if args.ablate:
        configs = [
            ("bare classifier (no history, no sticky, no keywords)", False, False, False),
            ("+ keyword net", False, False, True),
            ("+ history", True, False, False),
            ("+ history + sticky", True, True, False),
            ("+ history + sticky + keyword net", True, True, True),
        ]

    summaries = []
    pairs = ([(args.model, m2) for m2 in args.sweep2] if args.sweep2
             else [(m, args.model2) for m in (args.sweep or [args.model])])
    for mdl, mdl2 in pairs:
        second = router.second_model(mdl2 if mdl2 else router.ROUTER_MODEL_2)
        for name, hist, sticky, pf in configs:
            r = await run(convos, mdl, history=hist, sticky=sticky, prefilter=pf,
                          model2=mdl2)
            summaries.append(score(r, f"{mdl} + {second or 'solo'}  {name}",
                                   show_wrong=not args.ablate or True))

    if len(summaries) > 1:
        print("\n=== summary ===")
        print(f"{'config':<68}{'acc':>8}{'sens FN':>10}{'->JAX':>8}{'over-esc':>10}")
        for s in summaries:
            print(f"{s['label']:<68}{s['accuracy']:>7.1%}"
                  f"{s['sensitive_fn_rate']:>10.1%}{s['jill_to_jax_rate']:>8.1%}"
                  f"{s['over_escalation_rate']:>10.1%}")


if __name__ == "__main__":
    asyncio.run(main())
