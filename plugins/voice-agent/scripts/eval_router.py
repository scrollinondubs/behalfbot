"""Score the router against a labeled utterance set.

    .venv/bin/python eval_router.py                          # default model
    .venv/bin/python eval_router.py --model llama3.1:8b
    .venv/bin/python eval_router.py --sweep m1 m2 m3         # compare models
    .venv/bin/python eval_router.py --no-prefilter           # classifier alone

The headline number is not accuracy. It is the sensitive false-negative rate:
the share of JILL cases that escaped to JAX or CHAT. Everything else on this
scoreboard is a preference; that one is the privacy boundary holding or not.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path

import httpx

import router
from router import Route

CASES_PATH = Path(__file__).parent / "router_cases.json"
HOLDOUT_PATH = Path(__file__).parent / "router_cases_holdout.json"
ADVERSARIAL_PATH = Path(__file__).parent / "router_cases_adversarial.json"
# Phase 3's fresh set. The Phase 2 adversarial set above has now been tuned
# against, so it is no longer out-of-sample and its number is no longer the
# honest one. This one is, exactly once.
ADVERSARIAL2_PATH = Path(__file__).parent / "router_cases_adversarial2.json"
ORDER = [Route.CHAT, Route.JAX, Route.HELD]


async def run_set(cases, model: str, prefilter: bool, warmup: bool = True,
                  model2: str = ""):
    results = []
    async with httpx.AsyncClient() as client:
        if warmup:
            # Long timeout on the load, then the normal path: a cold second
            # voter would otherwise time out, count as a JILL vote, and put a
            # fake 0% false-negative rate on the first few cases.
            for mdl in {model, router.second_model(model2) if model2 else None} - {None}:
                await router.classify_llm("warm up the prompt cache", client,
                                          model=mdl, timeout=180.0)
            await router.route("warm up the prompt cache", client, model=model,
                               prefilter=False, model2=model2)
        for c in cases:
            d = await router.route(c["text"], client, model=model, prefilter=prefilter,
                                   model2=model2)
            results.append({**c, "got": d.route.value, "source": d.source,
                            "ms": d.latency_ms, "raw": d.raw.strip()[:60],
                            "votes": {m: v.value if v else None for m, v in d.votes.items()}})
    return results


# "ASK" is not a destination. It is the keyword net holding the turn and putting
# the question back to Sean, so nothing has been sent and nothing has leaked. It
# gets its own column rather than being folded into JILL, because counting a
# held turn as a correct route would flatter the router and counting it as a
# leak would invent a disclosure that did not happen.
#
# ASK stays as a column so a run with the confirmation gate armed still scores;
# with the gate off nothing produces it and the column reads zero.
GOT_ORDER = [*ORDER, "ASK"]


def _got_key(r):
    return r["got"] if isinstance(r["got"], str) else r["got"].value


def confusion(results):
    cols = [x.value if hasattr(x, "value") else x for x in GOT_ORDER]
    m = {a.value: {b: 0 for b in cols} for a in ORDER}
    for r in results:
        # See the note in report(): the case files still say JILL where they
        # mean "must not be escalated", which is HELD.
        expect = "HELD" if r["expect"] == "JILL" else r["expect"]
        m[expect][_got_key(r)] += 1
    return m


def report(results, label: str, verbose: bool = False):
    n = len(results)
    correct = sum(("HELD" if r["expect"] == "JILL" else r["expect"]) == r["got"]
                  for r in results)
    m = confusion(results)

    jill_total = sum(m["HELD"].values())
    jill_leaked = m["HELD"]["JAX"] + m["HELD"]["CHAT"]
    asked = sum(m[a.value]["ASK"] for a in ORDER)
    fn_rate = jill_leaked / jill_total if jill_total else 0.0
    # The two directions of a false negative do not cost the same thing, and
    # pooling them hides which problem is which. JILL -> JAX is a disclosure:
    # the question goes to Anthropic. JILL -> CHAT is answered by the local
    # model and never leaves the box, so it costs a useless answer and nothing
    # else. Only the first one is what the confirmation gate is standing in
    # front of; the second needs a better router and no gate can help it.
    to_jax = m["HELD"]["JAX"]
    to_chat = m["HELD"]["CHAT"]

    # Cases sent to Venice that did not need to be. Costs latency, not privacy.
    over = m["CHAT"]["HELD"] + m["JAX"]["HELD"]
    non_jill = sum(m["CHAT"].values()) + sum(m["JAX"].values())

    model_calls = [r["ms"] for r in results if r["source"] == "model"]

    print(f"\n=== {label} ===")
    print(f"accuracy            {correct}/{n} = {correct / n:.1%}")
    print(f"SENSITIVE FN RATE   {jill_leaked}/{jill_total} = {fn_rate:.1%}   "
          f"<- private turns that were NOT refused")
    print(f"  off the box       {to_jax}/{jill_total} = "
          f"{to_jax / jill_total if jill_total else 0:.1%}   "
          f"<- escalated; reaches Anthropic")
    print(f"  answered locally  {to_chat}/{jill_total} = "
          f"{to_chat / jill_total if jill_total else 0:.1%}   "
          f"<- answered by the local model; never left the box")
    print(f"wrongly refused    {over}/{non_jill} = {over / non_jill:.1%}   "
          f"<- ordinary turns the keyword net refused; costs a repeat")
    if asked:
        print(f"held and asked      {asked}/{n} = {asked / n:.1%}   "
              f"<- keyword net put the destination back to Sean; nothing sent")
    if model_calls:
        print(f"classifier latency  median {statistics.median(model_calls):.0f} ms  "
              f"p95 {sorted(model_calls)[int(len(model_calls) * 0.95) - 1]:.0f} ms  "
              f"n={len(model_calls)}")

    print("\n            got ->")
    cols = [x.value if hasattr(x, "value") else x for x in GOT_ORDER]
    print("expect      " + "".join(f"{c:>7}" for c in cols))
    for a in ORDER:
        print(f"{a.value:<12}" + "".join(f"{m[a.value][c]:>7}" for c in cols))

    wrong = [r for r in results if r["expect"] != r["got"]]
    if wrong:
        print(f"\n{len(wrong)} misroutes:")
        for r in wrong:
            flag = "LEAK " if r["expect"] == "HELD" else "     "
            print(f"  {flag}{r['expect']} -> {r['got']} [{r['source']}]  {r['text']}")
    if verbose:
        print("\nall:")
        for r in results:
            mark = "ok " if r["expect"] == r["got"] else "BAD"
            print(f"  {mark} {r['got']:<5} [{r['source']:<9}] {r['ms']:>6.0f}ms  {r['text']}")

    return {"label": label, "n": n, "accuracy": correct / n,
            "sensitive_fn_rate": fn_rate, "over_escalation_rate": over / non_jill,
            "jill_to_jax_rate": to_jax / jill_total if jill_total else 0.0,
            "jill_to_chat_rate": to_chat / jill_total if jill_total else 0.0,
            "confusion": m,
            "median_ms": statistics.median(model_calls) if model_calls else None}


def report_overrides(cases):
    ok = 0
    bad = []
    for c in cases:
        got = router.explicit_override(c["text"])
        got = got.value if got else "NONE"
        # Naming Jill used to route to her. It now holds the turn, which is the
        # same decision expressed as a refusal, so the old label still passes.
        expect = "HELD" if c["expect"] == "JILL" else c["expect"]
        if got == expect:
            ok += 1
        else:
            bad.append((c["text"], expect, got))
    print(f"\n=== explicit overrides (regex, no model) ===")
    print(f"accuracy            {ok}/{len(cases)} = {ok / len(cases):.1%}")
    for t, e, g in bad:
        print(f"  MISS {e} -> {g}  {t}")
    return ok / len(cases)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=router.ROUTER_MODEL)
    ap.add_argument("--model2", default="",
                    help="second voter; 'off' for the Phase 2 single-model router")
    ap.add_argument("--sweep", nargs="+", help="compare several models")
    ap.add_argument("--sweep2", nargs="+",
                    help="compare second voters against the fixed primary")
    ap.add_argument("--no-prefilter", action="store_true")
    ap.add_argument("--both", action="store_true",
                    help="score with and without the keyword net")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--holdout", action="store_true", help="score the held-out set instead")
    ap.add_argument("--adversarial", action="store_true",
                    help="score the Phase 2 keyword-dodging set (now in-sample)")
    ap.add_argument("--adversarial2", action="store_true",
                    help="score the Phase 3 fresh set - the out-of-sample number")
    ap.add_argument("--all-cases", action="store_true",
                    help="score the three Phase 2 sets pooled, as Phase 2 reported them")
    ap.add_argument("--json-out", type=Path)
    args = ap.parse_args()

    blob = json.loads(CASES_PATH.read_text())
    cases = blob["cases"]
    if args.holdout:
        cases = json.loads(HOLDOUT_PATH.read_text())["cases"]
    elif args.adversarial:
        cases = json.loads(ADVERSARIAL_PATH.read_text())["cases"]
    elif args.adversarial2:
        cases = json.loads(ADVERSARIAL2_PATH.read_text())["cases"]
    elif args.all_cases:
        cases = (cases + json.loads(HOLDOUT_PATH.read_text())["cases"]
                 + json.loads(ADVERSARIAL_PATH.read_text())["cases"])
    print(f"{len(cases)} cases  "
          f"({sum(c['expect'] == 'CHAT' for c in cases)} CHAT / "
          f"{sum(c['expect'] == 'JAX' for c in cases)} JAX / "
          f"{sum(c['expect'] == 'JILL' for c in cases)} JILL, "
          f"{sum(c.get('hard', False) for c in cases)} marked hard)")

    report_overrides(blob["overrides"])

    summaries = []
    pairs = ([(args.model, m2) for m2 in args.sweep2] if args.sweep2
             else [(m, args.model2) for m in (args.sweep or [args.model])])
    for mdl, mdl2 in pairs:
        modes = [False, True] if args.both else [not args.no_prefilter]
        for pf in modes:
            t0 = time.perf_counter()
            results = await run_set(cases, mdl, pf, model2=mdl2)
            second = router.second_model(mdl2 if mdl2 else router.ROUTER_MODEL_2)
            label = (f"{mdl} + {second or 'solo'}  "
                     f"{'prefilter+classifier' if pf else 'classifier only'}")
            s = report(results, label, verbose=args.verbose)
            s["wall_secs"] = time.perf_counter() - t0
            s["model"] = mdl
            s["model2"] = second
            s["prefilter"] = pf
            summaries.append(s)

    if len(summaries) > 1:
        print("\n=== summary ===")
        print(f"{'model / mode':<52}{'acc':>8}{'sens FN':>10}{'->JAX':>8}"
              f"{'over-esc':>10}{'med ms':>9}")
        for s in summaries:
            med = f"{s['median_ms']:.0f}" if s["median_ms"] else "-"
            print(f"{s['label']:<52}{s['accuracy']:>7.1%}{s['sensitive_fn_rate']:>10.1%}"
                  f"{s['jill_to_jax_rate']:>8.1%}{s['over_escalation_rate']:>10.1%}{med:>9}")

    if args.json_out:
        args.json_out.write_text(json.dumps(summaries, indent=2))
        print(f"\nwrote {args.json_out}")


if __name__ == "__main__":
    asyncio.run(main())
