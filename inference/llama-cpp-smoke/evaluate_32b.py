#!/usr/bin/env python3
"""Bounded paired-quality and long-context checks for the staged Qwen 32B profiles."""

import argparse
import concurrent.futures
import json
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


QUALITY_CASES = [
    {
        "id": "faithful-extraction",
        "system": "Return only valid JSON with the requested keys and source values.",
        "user": (
            "Extract project, owner, and deadline from this note. Do not infer "
            "missing details.\nNote: Project Cedar-17 is owned by Leena Rao; "
            "the handoff deadline is 2026-11-04 16:30 UTC."
        ),
        "rubric": "JSON parses and all three extracted values exactly match the note.",
        "check": "json_exact",
        "expected": {"project": "Cedar-17", "owner": "Leena Rao", "deadline": "2026-11-04 16:30 UTC"},
    },
    {
        "id": "concise-summary",
        "system": "Summarize faithfully and concisely.",
        "user": (
            "Summarize this update in one sentence of at most 25 words, preserving "
            "the key facts.\nThe Larch-3 migration finished at 15:20 UTC. "
            "All 48 services passed checks. The old cluster remains available "
            "until Friday for rollback. No customer data was changed."
        ),
        "rubric": "One sentence, at most 25 words, includes the migration, time, 48 services, rollback window, and unchanged customer data.",
        "check": "summary_facts",
        "expected": ["larch-3", "15:20", "48", "friday", "customer data"],
    },
    {
        "id": "arithmetic-reasoning",
        "system": "Calculate carefully. State the final amount clearly.",
        "user": (
            "A lab buys 320 units at $18 each. Apply a 12.5% discount, then "
            "8% sales tax to the discounted subtotal. What is the final total?"
        ),
        "rubric": "Discounted subtotal is $5,040.00 and final total is $5,443.20.",
        "check": "arithmetic_values",
        "expected": ["5040.00", "5443.20"],
    },
    {
        "id": "instruction-following",
        "system": "Follow output constraints exactly.",
        "user": (
            "Return exactly three lines, with no introduction or extra text, in "
            "this format: A: ...\nA is the color of a clear daytime sky. "
            "B is 9 + 5. C answers whether 2 is greater than 7."
        ),
        "rubric": "Exactly three lines and exact labels/answers: A: blue; B: 14; C: no.",
        "check": "exact_lines",
        "expected": ["A: blue", "B: 14", "C: no"],
    },
    {
        "id": "code-explanation",
        "system": "Explain the code accurately and briefly.",
        "user": (
            "Explain what this function returns and what condition it applies.\n"
            "```python\n"
            "def passed(results):\n"
            "    return [name for name, score in results if score >= 70]\n"
            "```\n"
            "For [('Mina', 70), ('Nia', 69), ('Omar', 91)], what is returned?"
        ),
        "rubric": "Explains the inclusive score threshold and returns Mina and Omar, excluding Nia.",
        "check": "code_facts",
        "expected": {"returned_names": ["Mina", "Omar"], "minimum_score": 70},
    },
    {
        "id": "source-grounded-facts",
        "system": "Answer only from the provided report; be concise.",
        "user": (
            "In one sentence, report the tested unit, run duration, peak "
            "temperature, and leak result.\nReport: Unit Cobalt-2 ran for 71 "
            "minutes. Its peak temperature was 63 degrees Celsius. Inspection "
            "found no leak."
        ),
        "rubric": "Includes Cobalt-2, 71 minutes, 63 degrees Celsius, and no leak without contradicting the report.",
        "check": "grounded_facts",
        "expected": ["cobalt-2", "71", "63", "no leak"],
    },
]

FILLER = (
    "Background note: this paragraph is unrelated to the ledger values. "
    "Keep operational context separate from the three requested records. "
    "Routine status remains unchanged; no additional action is required.\n"
)
FACT_BEGIN = "Beginning record: project ORBIT-741; location rack R-12.\n"
FACT_MIDDLE = "Middle record: failover host mica-3; checksum 83A9.\n"
FACT_END = "Ending record: owner Imani; rollback window 6 minutes.\n"
QUESTION = (
    "Report the project, rack, failover host, checksum, owner, and rollback "
    "window exactly. Use one concise line."
)
CHAT_PREFIX = (
    "<|im_start|>system\nYou are a precise assistant. Use only information in "
    "the user message. Keep the requested answer concise.<|im_end|>\n"
    "<|im_start|>user\n"
)
CHAT_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n"


def request_json(url, payload=None, timeout=900):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {} if data is None else {"Content-Type": "application/json"}
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def tokenize(base_url, prompt, timeout):
    result = request_json(base_url + "/tokenize", {"content": prompt}, timeout)
    return len(result["tokens"])


def render_chat(system, user):
    return (
        "<|im_start|>system\n" + system + "<|im_end|>\n"
        "<|im_start|>user\n" + user + "<|im_end|>\n"
        "<|im_start|>assistant\n"
    )


def completion(base_url, prompt, max_tokens, timeout):
    started = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        response = request_json(
            base_url + "/completion",
            {
                "prompt": prompt,
                "n_predict": max_tokens,
                "temperature": 0.0,
                "seed": 42,
                "stream": False,
                "cache_prompt": False,
            },
            timeout,
        )
        return {
            "ok": True,
            "started_at_utc": started_at,
            "client_ms": round((time.perf_counter() - started) * 1000, 1),
            "text": response.get("content", ""),
            "stop_type": response.get("stop_type"),
            "truncated": response.get("truncated", False),
            "timings": response.get("timings", {}),
            "tokens_evaluated": (response.get("timings") or {}).get("prompt_n"),
            "tokens_generated": (response.get("timings") or {}).get("predicted_n"),
        }
    except (OSError, ValueError, urllib.error.URLError) as error:
        return {
            "ok": False,
            "started_at_utc": started_at,
            "client_ms": round((time.perf_counter() - started) * 1000, 1),
            "error": str(error),
        }


def objective_check(case, text):
    normalized = text.casefold()
    check = case["check"]
    if check == "json_exact":
        try:
            parsed = json.loads(text)
            return {"passed": parsed == case["expected"], "observed": parsed}
        except (json.JSONDecodeError, TypeError):
            return {"passed": False, "observed": "invalid JSON"}
    if check == "summary_facts":
        facts = case["expected"]
        count = len(text.strip().split())
        found = [fact for fact in facts if fact in normalized]
        return {"passed": count <= 25 and len(found) == len(facts), "word_count": count, "facts_found": found}
    if check == "arithmetic_values":
        digits = re.sub(r"[,\s$]", "", normalized)
        found = [value for value in case["expected"] if value in digits]
        return {"passed": len(found) == len(case["expected"]), "values_found": found}
    if check == "exact_lines":
        observed = [line.strip().casefold() for line in text.strip().splitlines()]
        expected = [line.casefold() for line in case["expected"]]
        return {"passed": observed == expected, "observed_lines": observed}
    if check == "code_facts":
        names = bool(re.search(r"\[\s*['\"]mina['\"]\s*,\s*['\"]omar['\"]\s*\]", normalized))
        threshold = bool(re.search(
            r"(?:>=\s*70|at least 70|70 or (?:above|higher)|meet or exceed(?:s)?(?: the)? (?:threshold )?of 70)",
            normalized,
        ))
        return {"passed": names and threshold, "returned_names_exact": names, "threshold_explained": threshold}
    if check == "grounded_facts":
        found = [fact for fact in case["expected"] if fact in normalized]
        return {"passed": len(found) == len(case["expected"]), "facts_found": found}
    raise ValueError("unknown rubric check: " + check)


def quality_run(base_url, profile, timeout):
    cases = []
    for case in QUALITY_CASES:
        prompt = render_chat(case["system"], case["user"])
        count = tokenize(base_url, prompt, timeout)
        response = completion(base_url, prompt, 120, timeout)
        result = {key: value for key, value in case.items() if key != "check"}
        result["prompt"] = prompt
        result["input_tokens_tokenize_endpoint"] = count
        result["request"] = {"temperature": 0.0, "seed": 42, "max_tokens": 120, "chat_format": "explicit Qwen2.5-Instruct template"}
        result["response"] = response
        result["objective_check"] = objective_check(case, response.get("text", "")) if response["ok"] else {"passed": False, "not_run": True}
        cases.append(result)
        if not response["ok"]:
            break
    return {
        "profile": profile,
        "cases": cases,
        "automated_passes": sum(item["objective_check"]["passed"] for item in cases),
        "automated_total": len(cases),
        "human_scoring": "Not performed; review raw outputs using the per-task rubric.",
    }


def make_long_prompt(repetitions):
    left = repetitions // 2
    right = repetitions - left
    return (
        CHAT_PREFIX + FACT_BEGIN + FILLER * left + FACT_MIDDLE +
        FILLER * right + FACT_END + "\n" + QUESTION + CHAT_SUFFIX
    )


def tune_prompt(base_url, target, timeout):
    low, high = 0, max(1, target // max(1, tokenize(base_url, FILLER, timeout)))
    best = None
    while True:
        candidate = make_long_prompt(high)
        count = tokenize(base_url, candidate, timeout)
        if count > target:
            break
        best = (high, count, candidate)
        high *= 2
    while low <= high:
        middle = (low + high) // 2
        candidate = make_long_prompt(middle)
        count = tokenize(base_url, candidate, timeout)
        if count <= target:
            if best is None or count > best[1]:
                best = (middle, count, candidate)
            low = middle + 1
        else:
            high = middle - 1
    if best is None:
        raise RuntimeError("could not construct a prompt below target " + str(target))
    return best[1], best[2]


def gpu_sample(stop, samples):
    while not stop.is_set():
        try:
            output = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=index,uuid,memory.used,utilization.gpu", "--format=csv,noheader,nounits"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            for line in output.splitlines():
                index, uuid, memory, utilization = [field.strip() for field in line.split(",")]
                samples.append({"time": datetime.now(timezone.utc).isoformat(), "gpu": int(index), "uuid": uuid, "memory_used_mib": int(memory), "utilization_percent": int(utilization)})
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        stop.wait(0.5)


def workload_state(base_url, timeout):
    state = {"health": request_json(base_url + "/health", timeout=timeout)}
    props = request_json(base_url + "/props", timeout=timeout)
    state["props"] = {key: props.get(key) for key in ("model_alias", "model_ftype", "model_path", "total_slots")}
    state["props"]["n_ctx"] = props.get("n_ctx", (props.get("default_generation_settings") or {}).get("n_ctx"))
    state["slots"] = request_json(base_url + "/slots", timeout=timeout)
    pods = json.loads(subprocess.check_output(
        ["kubectl", "-n", "inference", "get", "pods", "-l", "app.kubernetes.io/name=llama-cpp-server", "-o", "json"],
        text=True,
        timeout=15,
    ))
    state["pods"] = [
        {"name": pod["metadata"]["name"], "phase": pod["status"].get("phase"), "restarts": sum(item.get("restartCount", 0) for item in pod["status"].get("containerStatuses", []))}
        for pod in pods["items"]
    ]
    return state


def relevant_logs(since_time):
    try:
        output = subprocess.check_output(
            ["kubectl", "-n", "inference", "logs", "deploy/llama-cpp-server", "--timestamps", "--since-time=" + since_time],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=45,
        )
        pattern = re.compile(r"new prompt|prompt eval time|eval time =|total time =|truncated|error|failed|out of memory|OOM|CUDA.*error|slot.*release", re.IGNORECASE)
        return [line for line in output.splitlines() if pattern.search(line)][-80:]
    except (OSError, subprocess.SubprocessError) as error:
        return ["log collection failed: " + str(error)]


def long_fact_check(text):
    normalized = text.casefold()
    facts = ["orbit-741", "r-12", "mica-3", "83a9", "imani"]
    found = [fact for fact in facts if fact in normalized]
    rollback = bool(re.search(r"(?:6|six)[\s-]*(?:minutes?|mins?)", normalized))
    return {"passed": len(found) == len(facts) and rollback, "facts_found": found, "rollback_six_minutes": rollback}


def context_within_limit(responses, max_tokens, context_limit=16384):
    if not responses or any(
        not response.get("ok")
        or response.get("truncated")
        or response.get("tokens_evaluated") is None
        for response in responses
    ):
        return False
    return sum(response["tokens_evaluated"] + max_tokens for response in responses) <= context_limit


def long_run(base_url, profile, timeout):
    targets = [4096, 8192, 12288, 15360]
    cases = []
    for target in targets:
        count, prompt = tune_prompt(base_url, target, timeout)
        max_tokens = 96
        if count + max_tokens > 16384:
            raise RuntimeError("prompt plus reserved output exceeds configured context")
        response = completion(base_url, prompt, max_tokens, timeout)
        response["objective_fact_check"] = long_fact_check(response.get("text", "")) if response["ok"] else {"passed": False}
        response["context_reserve_check"] = context_within_limit([response], max_tokens)
        logs = relevant_logs(response["started_at_utc"])
        response["runtime_error_in_logs"] = any(
            re.search(r"out of memory|CUDA error|failed to allocate|allocation failed", line, re.IGNORECASE)
            for line in logs
        )
        try:
            state = workload_state(base_url, timeout)
        except Exception as error:
            state = {"error": str(error)}
        cases.append({
            "target_prompt_tokens": target,
            "actual_prompt_tokens_tokenize_endpoint": count,
            "prompt": prompt,
            "max_output_tokens_reserved": max_tokens,
            "response": response,
            "relevant_runtime_logs": logs,
            "state_after": state,
        })
        if (
            not response["ok"]
            or response.get("truncated")
            or not response["context_reserve_check"]
            or response["runtime_error_in_logs"]
            or "pods" not in state
            or any(pod["restarts"] for pod in state["pods"])
        ):
            break
    if len(cases) == len(targets) and all(case["response"]["ok"] for case in cases):
        target = 6144
        count, prompt = tune_prompt(base_url, target, timeout)
        max_tokens = 64
        if count * 2 + max_tokens * 2 > 16384:
            raise RuntimeError("concurrent prompts plus output reserve exceed shared context cells")
        started = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: completion(base_url, prompt, max_tokens, timeout), range(2)))
        concurrent_context_safe = context_within_limit(responses, max_tokens)
        for response in responses:
            response["objective_fact_check"] = long_fact_check(response.get("text", "")) if response["ok"] else {"passed": False}
            response["context_reserve_check"] = concurrent_context_safe
        logs = relevant_logs(min(response["started_at_utc"] for response in responses))
        runtime_error = any(
            re.search(r"out of memory|CUDA error|failed to allocate|allocation failed", line, re.IGNORECASE)
            for line in logs
        )
        for response in responses:
            response["runtime_error_in_logs"] = runtime_error
        try:
            state = workload_state(base_url, timeout)
        except Exception as error:
            state = {"error": str(error)}
        cases.append({
            "concurrency": 2,
            "target_prompt_tokens_each": target,
            "actual_prompt_tokens_each_tokenize_endpoint": count,
            "aggregate_reserved_context_tokens": count * 2 + max_tokens * 2,
            "aggregate_context_check_passed": concurrent_context_safe,
            "max_output_tokens_each": max_tokens,
            "wall_ms": round((time.perf_counter() - started) * 1000, 1),
            "prompt": prompt,
            "responses": responses,
            "relevant_runtime_logs": logs,
            "state_after": state,
        })
    return {"profile": profile, "serial_targets": targets, "cases": cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("quality", "long"), required=True)
    parser.add_argument("--profile", choices=("q8-two-gpu-16k", "q8-two-gpu-4k", "q4-one-gpu-4k"), required=True)
    parser.add_argument("--base-url", default="http://10.43.125.168:8080")
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if Path(args.output).exists():
        parser.error("refusing to overwrite existing result artifact: " + args.output)

    base_url = args.base_url.rstrip("/")
    result = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "runtime": "llama.cpp b12019-1052f7e1d",
        "base_url": base_url,
        "context_limit": 4096 if args.profile in ("q8-two-gpu-4k", "q4-one-gpu-4k") else 16384,
        "mode": args.mode,
        "timeout_seconds_per_request": args.timeout,
        "initial_state": workload_state(base_url, args.timeout),
    }
    gpu_samples = []
    stop = threading.Event()
    sampler = threading.Thread(target=gpu_sample, args=(stop, gpu_samples), daemon=True)
    sampler.start()
    try:
        result["evaluation"] = quality_run(base_url, args.profile, args.timeout) if args.mode == "quality" else long_run(base_url, args.profile, args.timeout)
    except Exception as error:
        result["evaluation_error"] = str(error)
    finally:
        stop.set()
        sampler.join(timeout=6)
    result["gpu_samples"] = gpu_samples
    result["peak_gpu_memory_mib"] = {
        str(gpu): max(sample["memory_used_mib"] for sample in gpu_samples if sample["gpu"] == gpu)
        for gpu in sorted({sample["gpu"] for sample in gpu_samples})
    }
    try:
        result["final_state"] = workload_state(base_url, args.timeout)
    except Exception as error:
        result["final_state_error"] = str(error)
    output = Path(args.output)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"output": str(output), "mode": args.mode, "profile": args.profile, "peak_gpu_memory_mib": result["peak_gpu_memory_mib"], "final_state": result["final_state"]}, indent=2))
    evaluation = result.get("evaluation", {})
    failed = bool(result.get("evaluation_error") or result.get("final_state_error"))
    failed |= any(not case["response"]["ok"] for case in evaluation.get("cases", []) if "response" in case)
    failed |= any(not response["ok"] for case in evaluation.get("cases", []) for response in case.get("responses", []))
    failed |= any(case.get("response", {}).get("truncated") for case in evaluation.get("cases", []))
    if args.mode == "long":
        for case in evaluation.get("cases", []):
            responses = [case["response"]] if "response" in case else case.get("responses", [])
            failed |= any(
                not response.get("context_reserve_check")
                or response.get("runtime_error_in_logs")
                or not response.get("objective_fact_check", {}).get("passed")
                for response in responses
            )
            state = case.get("state_after", {})
            failed |= state.get("health", {}).get("status") != "ok"
            failed |= any(pod.get("restarts", 0) for pod in state.get("pods", []))
        failed |= not evaluation.get("cases", [{}])[-1].get("aggregate_context_check_passed", True)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()