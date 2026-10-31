#!/usr/bin/env python3
"""Run bounded, repeatable measurements against a llama.cpp completion API."""

import argparse
import concurrent.futures
import json
import math
import time
import urllib.error
import urllib.request


PROMPT = (
    "Explain why measuring a system matters. Give a clear answer in numbered "
    "steps, keep each step concise, and finish with one practical example."
)


def request_json(url, payload, timeout):
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def get_json(url, timeout):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def completion(base_url, timeout):
    payload = {
        "prompt": PROMPT,
        "n_predict": 64,
        "temperature": 0.0,
        "seed": 42,
        "stream": False,
        "cache_prompt": False,
    }
    started = time.perf_counter()
    try:
        response = request_json(base_url + "/completion", payload, timeout)
        elapsed_ms = (time.perf_counter() - started) * 1000
        timings = response.get("timings") or {}
        if not timings:
            return {"client_ms": elapsed_ms, "error": "response has no timings"}
        return {
            "client_ms": elapsed_ms,
            "prompt_n": timings.get("prompt_n"),
            "prompt_ms": timings.get("prompt_ms"),
            "prompt_per_second": timings.get("prompt_per_second"),
            "predicted_n": timings.get("predicted_n"),
            "predicted_ms": timings.get("predicted_ms"),
            "predicted_per_second": timings.get("predicted_per_second"),
            "stop_type": response.get("stop_type"),
        }
    except (OSError, ValueError, urllib.error.URLError) as error:
        return {
            "client_ms": (time.perf_counter() - started) * 1000,
            "error": str(error),
        }


def percentile(values, percentile_value):
    ordered = sorted(values)
    if not ordered:
        return None
    return ordered[max(0, math.ceil(percentile_value * len(ordered)) - 1)]


def summarize(samples):
    successful = [sample for sample in samples if "error" not in sample]
    summary = {
        "requests": len(samples),
        "successes": len(successful),
        "errors": len(samples) - len(successful),
    }
    fields = (
        "client_ms",
        "prompt_ms",
        "prompt_per_second",
        "predicted_ms",
        "predicted_per_second",
        "predicted_n",
    )
    for field in fields:
        values = [sample[field] for sample in successful if sample.get(field) is not None]
        summary[field + "_p50"] = percentile(values, 0.50)
        summary[field + "_p95"] = percentile(values, 0.95)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="llama.cpp API base URL")
    parser.add_argument("--windows", type=int, default=3)
    parser.add_argument("--requests", type=int, default=10, help="requests per window")
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()
    if not 1 <= args.windows <= 3 or not 1 <= args.requests <= 20 or not 0 <= args.warmups <= 5:
        parser.error("use windows 1-3, requests 1-20, and warmups 0-5")

    base_url = args.base_url.rstrip("/")
    health = get_json(base_url + "/health", args.timeout)
    tokenized = request_json(base_url + "/tokenize", {"content": PROMPT}, args.timeout)
    warmups = [completion(base_url, args.timeout) for _ in range(args.warmups)]
    windows = []

    for concurrency in (1, 2):
        for window_number in range(1, args.windows + 1):
            started = time.perf_counter()
            with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
                samples = list(
                    pool.map(
                        lambda _: completion(base_url, args.timeout),
                        range(args.requests),
                    )
                )
            wall_seconds = time.perf_counter() - started
            generated_tokens = sum(sample.get("predicted_n") or 0 for sample in samples)
            windows.append(
                {
                    "concurrency": concurrency,
                    "window": window_number,
                    "wall_seconds": wall_seconds,
                    "aggregate_generated_tokens_per_second": generated_tokens / wall_seconds,
                    "summary": summarize(samples),
                    "samples": samples,
                }
            )

    summaries = {}
    for concurrency in (1, 2):
        selected = [window for window in windows if window["concurrency"] == concurrency]
        samples = [sample for window in selected for sample in window["samples"]]
        total_wall = sum(window["wall_seconds"] for window in selected)
        total_tokens = sum(sample.get("predicted_n") or 0 for sample in samples)
        summaries[str(concurrency)] = {
            "windows": len(selected),
            "summary": summarize(samples),
            "aggregate_generated_tokens_per_second": total_tokens / total_wall,
        }

    result = {
        "base_url": base_url,
        "prompt": PROMPT,
        "prompt_token_count": len(tokenized.get("tokens", [])),
        "request": {
            "endpoint": "/completion",
            "n_predict": 64,
            "temperature": 0.0,
            "seed": 42,
            "stream": False,
            "cache_prompt": False,
        },
        "health": health,
        "warmups": warmups,
        "warmup_summary": summarize(warmups),
        "summaries_by_concurrency": summaries,
        "windows": windows,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    if any("error" in sample for window in windows for sample in window["samples"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()