#!/usr/bin/env python3
"""Benchmark a running Strata LLM server: solo and two concurrent streams.

  bench    measure an already running server (pure HTTP client) and save
           raw.jsonl, summary.json and meta.json to <results-dir>/<ts>-<label>/
  compare  diff two result directories

Host lifecycle (proxy, engine start/stop) belongs to the `host_strata` plugin
tool. Stdlib only. Request bodies are hashed so separate runs can be proven
identical.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

TOKENS_PER_LINE = 32  # measured with the Qwen3.8 pack tokenizer (~31.7/line)
INSTRUCTION = ("\nWrite a detailed technical essay of at least 2000 words about software "
               "reliability, testing, concurrency and memory management. "
               "Do not conclude early.")


@dataclass(frozen=True)
class BenchOpts:
    url: str
    out: Path
    prompt_tokens: int = 8192
    max_tokens: int = 512
    repeats: int = 3
    model: str = "strata"
    timeout: float = 900.0
    provenance: dict | None = None


def fmt(v, nd: int = 2) -> str:
    return "-" if v is None else f"{v:.{nd}f}"


def median(values) -> float | None:
    vals = [v for v in values if v is not None]
    return statistics.median(vals) if vals else None


def dump(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def build_corpus(prompt_tokens: int) -> str:
    """Deterministic text of roughly `prompt_tokens` tokens."""
    n = max(1, prompt_tokens // TOKENS_PER_LINE)
    return "\n".join(f"def transform_{i:05d}(value): return (value * {i % 97 + 1} + {i}) % 100003"
                     for i in range(n))


def build_prompt(corpus: str, tag: str) -> str:
    # The unique tag defeats prefix-cache reuse while keeping the payload stable.
    return f"[bench {tag}]\n{corpus}{INSTRUCTION}"


def send_request(opts: BenchOpts, prompt: str, tag: str,
                 case: str, repeat: int, stream: int) -> dict:
    """One streaming request; returns the raw sample record."""
    body = json.dumps({
        "model": opts.model, "messages": [{"role": "user", "content": prompt}],
        "max_tokens": opts.max_tokens, "temperature": 0, "reasoning_effort": "none",
        "stream": True, "stream_options": {"include_usage": True}, "cache_prompt": False,
    }, separators=(",", ":")).encode()
    req = urllib.request.Request(
        opts.url.rstrip("/") + "/v1/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"})

    usage, timings, finish, ttft = {}, {}, None, None
    t_start = time.perf_counter()
    with urllib.request.urlopen(req, timeout=opts.timeout) as resp:
        for line in resp:
            if not line.startswith(b"data: "):
                continue
            payload = line[6:].strip()
            if payload == b"[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if ttft is None and (delta.get("content") or delta.get("reasoning_content")):
                    ttft = time.perf_counter() - t_start
                finish = choice.get("finish_reason") or finish
            usage = chunk.get("usage") or usage
            timings = chunk.get("timings") or timings
    t_end = time.perf_counter()

    cache_n, completion = timings.get("cache_n", 0) or 0, usage.get("completion_tokens")
    problems = (["cache_n>=32"] if cache_n >= 32 else []) \
        + (["short_output"] if completion != opts.max_tokens else []) \
        + (["no_timings"] if not timings else [])

    return {
        "case": case, "repeat": repeat, "stream": stream, "tag": tag,
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "ttft_s": ttft, "wall_s": t_end - t_start,
        "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": completion,
        "cache_n": cache_n, "finish_reason": finish, "problems": problems,
        "prompt_per_second": timings.get("prompt_per_second"),
        "predicted_per_second": timings.get("predicted_per_second"),
        "prompt_ms": timings.get("prompt_ms"), "predicted_ms": timings.get("predicted_ms"),
        "makespan_s": None, "t_start": t_start, "t_end": t_end,
    }


def append_jsonl(path: Path, record: dict, lock: threading.Lock) -> None:
    with lock, path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
        fh.flush()


def report(i: int, total: int, rec: dict) -> None:
    state = "OK" if not rec["problems"] else "INVALID:" + ",".join(rec["problems"])
    print(f"[{i}/{total}] {rec['case']:<7}{rec['tag']:<12} wall={rec['wall_s']:.1f}s "
          f"ttft={fmt(rec['ttft_s'])}s pp={fmt(rec['prompt_per_second'], 1)}t/s "
          f"tg={fmt(rec['predicted_per_second'], 1)}t/s {state}", flush=True)


def get_json(url: str, timeout: float = 10.0) -> tuple[int, dict]:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw.strip() else {})


def health_ok(base: str, timeout: float = 10.0) -> bool:
    try:
        return get_json(base.rstrip("/") + "/health", timeout)[0] == 200
    except Exception:
        return False


def summarize(records: list[dict]) -> dict:
    """Medians over problem-free samples; invalid samples are only counted."""
    measured = [r for r in records if r["case"] in ("solo", "pair")]

    def stats(case: str, stream: int | None = None) -> dict:
        all_rows = [r for r in measured if r["case"] == case
                    and (stream is None or r["stream"] == stream)]
        rows = [r for r in all_rows if not r["problems"]]
        return {"n_valid": len(rows), "n_total": len(all_rows),
                "prefill_tps": median([r["prompt_per_second"] for r in rows]),
                "decode_tps": median([r["predicted_per_second"] for r in rows]),
                "ttft_s": median([r["ttft_s"] for r in rows]),
                "wall_s": median([r["wall_s"] for r in rows])}

    pair_valid = [r for r in measured if r["case"] == "pair"
                  and not r["problems"] and r["makespan_s"]]
    by_repeat: dict[int, list[dict]] = {}
    for rec in pair_valid:
        by_repeat.setdefault(rec["repeat"], []).append(rec)

    def aggregate(field: str):
        return median([sum(r[field] or 0 for r in rows) / rows[0]["makespan_s"]
                       for rows in by_repeat.values()])

    return {"solo": stats("solo"),
            "pair": {"s0": stats("pair", 0), "s1": stats("pair", 1),
                     "makespan_s": median([r["makespan_s"] for r in pair_valid]),
                     "aggregate_output_tps": aggregate("completion_tokens"),
                     "aggregate_input_tps": aggregate("prompt_tokens")},
            "counts": {"n_valid": sum(1 for r in measured if not r["problems"]),
                       "n_total": len(measured)}}


def print_summary(summary: dict) -> None:
    head = (f"{'case':<10}{'valid':>8}{'prefill t/s':>13}"
            f"{'decode t/s':>12}{'ttft s':>9}{'wall s':>9}")
    print("\n" + head + "\n" + "-" * len(head))
    pair = summary["pair"]
    for name, st in (("solo", summary["solo"]), ("pair s0", pair["s0"]),
                     ("pair s1", pair["s1"])):
        print(f"{name:<10}{st['n_valid']}/{st['n_total']:<6}"
              f"{fmt(st['prefill_tps'], 1):>13}{fmt(st['decode_tps'], 1):>12}"
              f"{fmt(st['ttft_s'], 3):>9}{fmt(st['wall_s'], 2):>9}")
    print(f"{'pair agg':<10}{'':>8}{fmt(pair['aggregate_input_tps'], 1):>13}"
          f"{fmt(pair['aggregate_output_tps'], 1):>12}{'':>9}{fmt(pair['makespan_s'], 2):>9}")


def bench(opts: BenchOpts) -> dict:
    """Warmup, then solo and simultaneous-pair samples; writes the result files."""
    if not health_ok(opts.url):
        raise RuntimeError(f"health check failed for {opts.url}")
    opts.out.mkdir(parents=True, exist_ok=True)
    raw, lock = opts.out / "raw.jsonl", threading.Lock()
    corpus = build_corpus(opts.prompt_tokens)
    records: list[dict] = []
    started = datetime.now().isoformat(timespec="seconds")
    total = 1 + opts.repeats * 3

    def record(rec: dict) -> None:
        records.append(rec)
        append_jsonl(raw, rec, lock)
        report(len(records), total, rec)

    try:
        status = get_json(opts.url.rstrip("/") + "/v1/status")[1]
    except Exception:
        status = None

    record(send_request(opts, build_prompt(corpus, "warmup"), "warmup", "warmup", 0, 0))
    for r in range(opts.repeats):
        tag = f"solo-r{r}"
        record(send_request(opts, build_prompt(corpus, tag), tag, "solo", r, 0))

        # A barrier releases both streams together, aligning their start times.
        barrier = threading.Barrier(2)

        def one(idx: int, repeat: int = r) -> dict:
            pair_tag = f"pair-r{repeat}-s{idx}"
            barrier.wait()
            return send_request(opts, build_prompt(corpus, pair_tag), pair_tag,
                                "pair", repeat, idx)

        with ThreadPoolExecutor(max_workers=2) as pool:
            pair = [f.result() for f in [pool.submit(one, i) for i in (0, 1)]]
        makespan = max(x["t_end"] for x in pair) - min(x["t_start"] for x in pair)
        for rec in pair:
            rec["makespan_s"] = makespan
            record(rec)

    summary = summarize(records)
    summary["args"] = {k: str(v) for k, v in asdict(opts).items()}
    dump(opts.out / "summary.json", summary)
    dump(opts.out / "meta.json", {
        "url": opts.url, "args": summary["args"],
        "body_sha256": [r["body_sha256"] for r in records],
        "started": started, "finished": datetime.now().isoformat(timespec="seconds"),
        "v1_status": status, "provenance": opts.provenance})
    print_summary(summary)
    print(f"\nsaved: {opts.out}")
    return summary


ROWS = [("solo prefill t/s", ("solo", "prefill_tps")),
        ("solo decode t/s", ("solo", "decode_tps")),
        ("solo ttft s", ("solo", "ttft_s")),
        ("pair s0 prefill t/s", ("pair", "s0", "prefill_tps")),
        ("pair s0 decode t/s", ("pair", "s0", "decode_tps")),
        ("pair s1 prefill t/s", ("pair", "s1", "prefill_tps")),
        ("pair s1 decode t/s", ("pair", "s1", "decode_tps")),
        ("pair agg output t/s", ("pair", "aggregate_output_tps")),
        ("pair makespan s", ("pair", "makespan_s"))]


def lookup(summary: dict, path: tuple[str, ...]):
    node = summary
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return node


def compare(before_dir: Path, after_dir: Path) -> int:
    before = json.loads((before_dir / "summary.json").read_text(encoding="utf-8"))
    after = json.loads((after_dir / "summary.json").read_text(encoding="utf-8"))
    for name, directory in (("before", before_dir), ("after", after_dir)):
        try:
            prov = json.loads((directory / "meta.json").read_text(encoding="utf-8")).get("provenance") or {}
        except (OSError, json.JSONDecodeError):
            prov = {}
        print(f"{name + ':':<8}{directory}  [{prov.get('git_describe', '?')}]")
    print()
    print(f"{'metric':<22}{'before':>12}{'after':>12}{'change':>10}\n" + "-" * 56)
    for label, path in ROWS:
        b, a = lookup(before, path), lookup(after, path)
        change = f"{(a - b) / b * 100:+.1f}%" if b and a is not None else "-"
        print(f"{label:<22}{fmt(b):>12}{fmt(a):>12}{change:>10}")
    try:
        hb = json.loads((before_dir / "meta.json").read_text(encoding="utf-8"))["body_sha256"]
        ha = json.loads((after_dir / "meta.json").read_text(encoding="utf-8"))["body_sha256"]
        if hb != ha:
            print("\nWARNING: request payload hashes differ; runs are not comparable",
                  file=sys.stderr)
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        print(f"\nWARNING: cannot compare payload hashes ({exc})", file=sys.stderr)
    return 0


def add_bench_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--prompt-tokens", type=int, default=8192)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--model", default="strata")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_bench = sub.add_parser("bench", help="benchmark a running server")
    p_bench.add_argument("--url", required=True)
    p_bench.add_argument("--results-dir", required=True, type=Path)
    p_bench.add_argument("--label", required=True, help="e.g. before, after-v0.2.12")
    p_bench.add_argument("--provenance", type=Path,
                         help="JSON describing the served build, embedded in meta.json "
                              "(host_strata writes plugin-state/host-strata/host-strata-serve.json)")
    add_bench_args(p_bench)

    p_cmp = sub.add_parser("compare", help="diff two result directories")
    p_cmp.add_argument("before")
    p_cmp.add_argument("after")

    args = parser.parse_args(argv)
    if args.command == "bench":
        if not re.fullmatch(r"[A-Za-z0-9._-]+", args.label):
            parser.error("--label may only contain letters, digits, '.', '_' and '-'")
        provenance = (json.loads(args.provenance.read_text(encoding="utf-8"))
                      if args.provenance else None)
        out = args.results_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{args.label}"
        bench(BenchOpts(url=args.url, out=out, prompt_tokens=args.prompt_tokens,
                        max_tokens=args.max_tokens, repeats=args.repeats, model=args.model,
                        provenance=provenance))
        return 0
    return compare(Path(args.before), Path(args.after))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
