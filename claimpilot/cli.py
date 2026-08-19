"""Command-line interface.

  claimpilot run   --pack <dir>      build the full Negotiation Position Brief
  claimpilot ask   "question"        grounded Q&A over the claim folder
  claimpilot eval                    golden-set evaluation + ablation (see evals/)
  claimpilot bench                   retrieval backend head-to-head (see evals/)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import RunConfig


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pack", default="..", help="claim folder (default: ..)")
    parser.add_argument("--out", default="out", help="output directory")
    parser.add_argument("--provider", default=None,
                        choices=["anthropic", "cli", "replay"],
                        help="LLM provider (default: auto-detect)")
    parser.add_argument("--model", default=None, help="model id override")
    parser.add_argument("--retrieval", default="auto", choices=["auto", "datum", "fts5"],
                        help="retrieval backend (default: auto = datum with FTS5 fallback)")
    parser.add_argument("--ablate", action="append", default=[],
                        help="source_id to hide this run (repeatable; failure-handling demo)")
    parser.add_argument("--no-vision-verify", action="store_true",
                        help="skip the second-pass verification of vision extractions")
    parser.add_argument("-v", "--verbose", action="store_true")


def _config(args: argparse.Namespace) -> RunConfig:
    cfg = RunConfig(pack_dir=args.pack, out_dir=args.out)
    if args.provider:
        cfg.provider = args.provider
    if args.model:
        cfg.model = args.model
    cfg.retrieval_backend = args.retrieval
    cfg.ablate = list(args.ablate)
    cfg.vision_verify = not args.no_vision_verify
    return cfg


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="claimpilot",
                                     description="Freight Claim Copilot")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="build the Negotiation Position Brief")
    _common(p_run)

    p_ask = sub.add_parser("ask", help="grounded question answering over the claim folder")
    _common(p_ask)
    p_ask.add_argument("question")
    p_ask.add_argument("-k", type=int, default=5)

    p_eval = sub.add_parser("eval", help="run the golden-set evaluation")
    _common(p_eval)

    p_bench = sub.add_parser("bench", help="retrieval backend head-to-head benchmark")
    _common(p_bench)

    p_rob = sub.add_parser("robustness", help="adversarial-input robustness suite: run a "
                                              "pack seeded with synthetic tampering and "
                                              "assert the defenses hold")
    _common(p_rob)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s", stream=sys.stderr)
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    cfg = _config(args)

    if args.command == "run":
        from .pipeline import run_pipeline
        from .report import write_outputs
        case = run_pipeline(cfg)
        paths = write_outputs(case, cfg.out_dir)
        pos = case["position"]
        print("\n=== Claim {} ===".format(case["claim"].get("claim_id")))
        print("demand {} | offer {} | recommended counter {}".format(
            case["claim"].get("demand_usd"), case["claim"].get("carrier_offer_usd"),
            case["position_numbers"].get("position.recommended_counter",
                                         {}).get("value", "n/a")))
        print("facts {} | discrepancies {} | gaps {} | citation validity {:.1f}% | "
              "position {}".format(
                  len(case["facts"]), len(case["discrepancies"]), len(case["gaps"]),
                  case["qa"]["citation_validity_rate"] * 100,
                  "OK" if pos["ok"] else "FAILED CLOSED"))
        for name, path in paths.items():
            print("  {:12s} {}".format(name, path))
        return 0 if pos["ok"] else 3

    if args.command == "ask":
        from .pipeline import ask_question
        result = ask_question(cfg, args.question, k=args.k)
        print("\n[backend={} status={} sufficiency={}]".format(
            result["backend"], result["status"], result.get("sufficiency")))
        print("\n" + result.get("answer", ""))
        print("\n--- passages ---")
        for p in result["passages"]:
            print("  [{}] {} - {}".format(p["source_id"], p["locator"], p["title"]))
        return 0

    if args.command == "eval":
        root = Path(__file__).resolve().parent.parent
        sys.path.insert(0, str(root))
        from evals.run_evals import main as eval_main
        return eval_main(cfg)

    if args.command == "bench":
        root = Path(__file__).resolve().parent.parent
        sys.path.insert(0, str(root))
        from evals.retrieval_bench import main as bench_main
        return bench_main(cfg)

    if args.command == "robustness":
        root = Path(__file__).resolve().parent.parent
        sys.path.insert(0, str(root))
        from evals.robustness import main as robustness_main
        return robustness_main(cfg)

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
