#!/usr/bin/env python3
"""Claude Code usage per model, from the transcripts in ~/.claude/projects.

  claude-usage [--since YYYY-MM-DD] [--by month|day|repo|branch]   print a table
  claude-usage serve [--port 8766] [--budget USD]                   live dashboard

Transcripts record tokens, not money, so cost is tokens times the API list price.
On a Pro or Max subscription that is what the usage would have cost, not what you paid.
"""
import argparse
import glob
import json
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOME = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude"))
PROJECTS = os.path.join(HOME, "projects")
LIVE = os.path.join(HOME, "sessions")
HERE = os.path.dirname(os.path.realpath(__file__))
BURN_MINUTES = 15
BUDGET = None  # monthly USD budget, set by serve --budget
MTOK = 1e6

# USD per million tokens: (input, output, cache read). First match wins.
# Checked against https://platform.claude.com/docs/en/about-claude/pricing on 2026-11-17.
# Haiku 3 is not on that page, so its row is unverified.
PRICES = [
    (r"fable-5-2|mythos-5-2", (8.0, 40.0, 0.20)),
    (r"fable-5-1|mythos-5-1", (10.0, 50.0, 0.25)),
    (r"fable|mythos", (10.0, 50.0, 1.0)),
    (r"opus-4-[01]\b|opus-4-2025|opus-3", (15.0, 75.0, 1.5)),
    (r"opus-5-5", (4.0, 20.0, 0.20)),
    (r"opus", (5.0, 25.0, 0.5)),
    (r"sonnet-5-5", (2.0, 10.0, 0.10)),
    (r"sonnet-5", (2.0, 10.0, 0.2)),
    (r"sonnet", (3.0, 15.0, 0.3)),
    (r"haiku-3", (0.8, 4.0, 0.08)),
    (r"haiku-5-5", (0.10, 0.50, 0.01)),
    (r"haiku", (1.0, 5.0, 0.1)),
    (r"", (5.0, 25.0, 0.5)),
]
LONG_PROMPT_PRICES = [
    (r"haiku-5-5", 100_000, (0.50, 2.50, 0.05)),
]
CACHE_WRITE_5M, CACHE_WRITE_1H = 1.25, 2.0  # multiples of the input price
FAST_MODE = 2.0
US_RESIDENCY_MULTIPLIER = 1.1

FAMILIES = ("fable", "opus", "sonnet", "haiku")
FAMILY_RULES = [("fable", r"fable|mythos"), ("sonnet", r"sonnet"), ("haiku", r"haiku"), ("opus", r"")]

RELEVANT = tuple('"type":"%s"' % kind for kind in
                 ("assistant", "user", "system", "ai-title", "agent-name", "pr-link", "cost-state"))


def family_of(model):
    return next(family for family, pattern in FAMILY_RULES if re.search(pattern, model))


def prices_of(model, prompt_tokens):
    for pattern, threshold, prices in LONG_PROMPT_PRICES:
        if prompt_tokens > threshold and re.search(pattern, model):
            return prices
    return next(prices for pattern, prices in PRICES if re.search(pattern, model))


def call_costs(model, usage):
    """USD per token kind, plus what the cache reads would have cost extra at the input price."""
    reads = usage.get("cache_read_input_tokens") or 0
    written = usage.get("cache_creation_input_tokens") or 0
    writes = usage.get("cache_creation") or {}
    write_1h = writes.get("ephemeral_1h_input_tokens", 0)
    write_5m = written - write_1h
    prompt_tokens = (usage.get("input_tokens") or 0) + reads + written
    price_in, price_out, price_read = (p / MTOK for p in prices_of(model, prompt_tokens))
    scale = 1.0
    if usage.get("speed") == "fast":
        scale *= FAST_MODE
    if usage.get("inference_geo") == "us":
        scale *= US_RESIDENCY_MULTIPLIER
    price_in, price_out, price_read = price_in * scale, price_out * scale, price_read * scale
    return {
        "c_input": (usage.get("input_tokens") or 0) * price_in,
        "c_cache_read": reads * price_read,
        "c_cache_write": (write_5m * CACHE_WRITE_5M + write_1h * CACHE_WRITE_1H) * price_in,
        "c_output": (usage.get("output_tokens") or 0) * price_out,
        "saved": reads * (price_in - price_read),
    }


SUMS = ("cost", "calls", "prompts", "in", "out", "cache", "cache_write", "reasoning", "web",
        "c_input", "c_cache_read", "c_cache_write", "c_output", "saved")
KEYS = ("day", "hour", "session", "model", "initiator", "effort", "entrypoint", "finish", "speed")


def call_record(event):
    message = event["message"]
    usage = message.get("usage") or {}
    costs = call_costs(message["model"], usage)
    tools = usage.get("server_tool_use") or {}
    reads, written = usage.get("cache_read_input_tokens") or 0, usage.get("cache_creation_input_tokens") or 0
    return {
        "id": message.get("id") or event.get("uuid"), "session": event.get("sessionId", "unknown"),
        "ts": event.get("timestamp", ""), "model": message["model"],
        "initiator": "subagent" if event.get("isSidechain") else "main",
        "effort": event.get("effort") or "unknown", "entrypoint": event.get("entrypoint") or "unknown",
        "finish": message.get("stop_reason") or "unknown", "speed": usage.get("speed") or "standard",
        "cost": sum(costs[kind] for kind in costs if kind != "saved"), "calls": 1, "prompts": 0,
        # Claude reports fresh input apart from cached input; the dashboard wants the whole prompt size.
        "in": (usage.get("input_tokens") or 0) + reads + written,
        "out": usage.get("output_tokens") or 0, "cache": reads, "cache_write": written,
        "reasoning": (usage.get("output_tokens_details") or {}).get("thinking_tokens", 0),
        "web": tools.get("web_search_requests", 0) + tools.get("web_fetch_requests", 0),
        **costs,
    }


def is_prompt(event):
    if event.get("isMeta") or event.get("isSidechain") or event.get("promptSource") == "system":
        return False
    content = event.get("message", {}).get("content")
    if isinstance(content, str):
        return not content.startswith(("<command-name>", "<local-command", "<task-notification"))
    return isinstance(content, list) and not any(block.get("type") == "tool_result" for block in content)


def parse_transcript(path):
    """Model calls and session facts from one transcript.

    A response is written once per content block, each line repeating the same usage,
    so lines are collapsed by message id and the largest token count wins.
    """
    calls, facts = {}, defaultdict(lambda: defaultdict(int))
    pending_prompt = set()
    agent = os.path.basename(path)[:-6] if os.sep + "subagents" + os.sep in path else None

    with open(path, errors="replace") as lines:
        for line in lines:
            if not any(marker in line for marker in RELEVANT):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind, session = event.get("type"), event.get("sessionId", "unknown")
            fact = facts[session]

            if kind == "assistant":
                message = event.get("message") or {}
                if not message.get("usage") or message.get("model", "<synthetic>") == "<synthetic>":
                    continue
                record = call_record(event)
                known = calls.get(record["id"])
                if known is None or record["in"] + record["out"] > known["in"] + known["out"]:
                    record["prompts"] = known["prompts"] if known else 0
                    calls[record["id"]] = record
                if session in pending_prompt and not event.get("isSidechain"):
                    pending_prompt.discard(session)
                    calls[record["id"]]["prompts"] = 1
                fact["cwd"], fact["branch"] = event.get("cwd") or fact["cwd"], event.get("gitBranch") or fact["branch"]
                if agent:
                    fact.setdefault("agents", set()).add(agent)
            elif kind == "user" and is_prompt(event):
                pending_prompt.add(session)
            elif kind == "system" and event.get("subtype") == "turn_duration":
                fact["turns"] += 1
                fact["active_ms"] += event.get("durationMs", 0)
            elif kind == "system" and event.get("subtype") == "compact_boundary":
                fact["compactions"] += 1
            elif kind in ("ai-title", "agent-name"):
                fact["label"] = event.get("aiTitle") or event.get("agentName") or fact["label"]
            elif kind == "pr-link":
                fact.setdefault("pr_urls", set()).add(event.get("prUrl"))
            elif kind == "cost-state":
                fact["lines_added"], fact["lines_removed"] = event.get("totalLinesAdded", 0), event.get("totalLinesRemoved", 0)

    return {"calls": list(calls.values()), "facts": {k: dict(v) for k, v in facts.items()}}


_cache = {}


def load_transcripts():
    parsed = []
    for path in sorted(glob.glob(os.path.join(PROJECTS, "**", "*.jsonl"), recursive=True)):
        try:
            stat = os.stat(path)
        except OSError:
            continue
        stamp = (stat.st_mtime_ns, stat.st_size)
        if _cache.get(path, (None,))[0] != stamp:
            _cache[path] = (stamp, parse_transcript(path))
        parsed.append(_cache[path][1])
    for path in set(_cache) - set(glob.glob(os.path.join(PROJECTS, "**", "*.jsonl"), recursive=True)):
        del _cache[path]
    return parsed


def all_records(parsed):
    """Resumed and forked sessions copy their history into a new file, so dedupe across files too."""
    records = {}
    for transcript in parsed:
        for record in transcript["calls"]:
            known = records.get(record["id"])
            if known is None or record["in"] + record["out"] > known["in"] + known["out"]:
                records[record["id"]] = record
    return sorted(records.values(), key=lambda r: r["ts"])


def repo_of(cwd):
    # Worktrees live under <repo>/.claude/worktrees/<name>; report them as the repo.
    return os.path.basename(re.sub(r"/\.claude/worktrees/.*", "", cwd or "").rstrip("/")) or "-"


def session_info(parsed, records):
    merged = defaultdict(dict)
    for transcript in parsed:
        for session, fact in transcript["facts"].items():
            into = merged[session]
            for name, value in fact.items():
                if not value:
                    continue
                if isinstance(value, set):
                    into[name] = into.get(name, set()) | value
                elif isinstance(value, int) and name not in ("lines_added", "lines_removed"):
                    into[name] = into.get(name, 0) + value
                elif value:
                    into[name] = value
    span = {}
    for record in records:
        first, last = span.get(record["session"], (record["ts"], record["ts"]))
        span[record["session"]] = (min(first, record["ts"]), max(last, record["ts"]))

    info = {}
    for session, (first, last) in span.items():
        fact = merged.get(session, {})
        info[session] = {
            "label": fact.get("label") or session[:8], "repo": repo_of(fact.get("cwd")),
            "branch": fact.get("branch") or "-", "start": epoch(first), "end": epoch(last),
            "turns": fact.get("turns", 0), "active_ms": fact.get("active_ms", 0),
            "compactions": fact.get("compactions", 0), "subagents": len(fact.get("agents", ())),
            "prs": len(fact.get("pr_urls", ())),
            "lines_added": fact.get("lines_added", 0), "lines_removed": fact.get("lines_removed", 0),
        }
    return info


def epoch(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() if ts else None


def local_day_hour(ts):
    moment = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    return moment.strftime("%Y-%m-%d"), moment.hour


def usage_rows(records):
    rows = {}
    for record in records:
        if not record["ts"]:
            continue
        key = (*local_day_hour(record["ts"]), *(record[k] for k in KEYS[2:]))
        row = rows.get(key)
        if row is None:
            row = rows[key] = dict.fromkeys(SUMS, 0)
        for name in SUMS:
            row[name] += record[name]
    return rows


def pid_alive(pid):
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def active_sessions(records, info):
    recent_from = (datetime.now(timezone.utc) - timedelta(minutes=BURN_MINUTES)).strftime("%Y-%m-%dT%H:%M:%S")
    spend, recent, latest = defaultdict(float), defaultdict(float), {}
    for record in records:
        session = record["session"]
        spend[session] += record["cost"]
        if record["ts"] >= recent_from:
            recent[session] += record["cost"]
        latest[session] = (record["ts"], record["model"])

    active = []
    for path in glob.glob(os.path.join(LIVE, "*.json")):
        try:
            with open(path) as f:
                live = json.load(f)
        except (OSError, ValueError):
            continue
        session = live.get("sessionId")
        if not pid_alive(live.get("pid")) or session not in latest:
            continue
        last, model = latest[session]
        details = info.get(session, {})
        active.append({
            "id": session, "name": details.get("label") or live.get("name") or session[:8],
            "repo": details.get("repo") or repo_of(live.get("cwd")), "branch": details.get("branch", "-"),
            "model": model, "family": family_of(model), "status": live.get("status") or "",
            "cost": spend[session], "burn": recent[session] * 60 / BURN_MINUTES, "last": epoch(last),
        })
    return sorted(active, key=lambda s: -s["last"])


_built = {}


def usage_data():
    parsed = load_transcripts()
    version = str(hash(tuple(sorted((path, stamp) for path, (stamp, _) in _cache.items()))))
    if _built.get("version") != version:
        records = all_records(parsed)
        rows = sorted(usage_rows(records).items())
        in_usd = ("cost", "c_input", "c_cache_read", "c_cache_write", "c_output", "saved")
        _built.update(version=version, records=records, data={
            "fields": list(KEYS) + list(SUMS),
            "rows": [[*key, *(round(row[name], 5) if name in in_usd else row[name] for name in SUMS)]
                     for key, row in rows],
            "sessions": session_info(parsed, records),
            "families": {model: family_of(model) for model in {key[3] for key, _ in rows}},
        })
    return _built


def api_payload(known_version=""):
    built = usage_data()
    payload = {"now": time.time(), "version": built["version"], "budget": BUDGET, "burn_minutes": BURN_MINUTES,
               "active": active_sessions(built["records"], built["data"]["sessions"])}
    if known_version != built["version"]:
        payload.update(built["data"])
    return payload


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/api/usage":
            known = parse_qs(url.query).get("v", [""])[0]
            self.reply(json.dumps(api_payload(known), separators=(",", ":")).encode(), "application/json")
        elif url.path == "/":
            with open(os.path.join(HERE, "dashboard.html"), "rb") as f:
                self.reply(f.read(), "text/html; charset=utf-8")
        else:
            self.send_error(404)

    def reply(self, body, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def serve(port):
    print("Indexing transcripts...")
    usage_data()
    print(f"Dashboard at http://localhost:{port}  (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def print_table(since, by):
    parsed = load_transcripts()
    records = all_records(parsed)
    info = session_info(parsed, records)
    detail = lambda session, field: info.get(session, {}).get(field, "-")
    group_of = {"month": lambda day, session: day[:7], "day": lambda day, session: day,
                "repo": lambda day, session: detail(session, "repo"),
                "branch": lambda day, session: f"{detail(session, 'repo')} @ {detail(session, 'branch')}"}[by]
    groups = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    for (day, _hour, session, model, *_rest), row in usage_rows(records).items():
        if day < since:
            continue
        for field in ("cost", "calls", "in", "out", "cache"):
            groups[group_of(day, session)][model][field] += row[field]

    for key in sorted(groups):
        models = groups[key]
        total = sum(m["cost"] for m in models.values()) or 1
        print(f"\n{key}   ${total:,.2f}")
        print(f"  {'model':<28}{'family':<9}{'USD':>10}{'share':>8}{'calls':>8}{'in(M)':>9}{'cached':>8}{'out(M)':>8}")
        for model, m in sorted(models.items(), key=lambda kv: -kv[1]["cost"]):
            cached = f"{m['cache'] / m['in']:.0%}" if m["in"] else "-"
            print(f"  {model:<28}{family_of(model):<9}{m['cost']:>10,.2f}{m['cost'] / total:>8.0%}"
                  f"{m['calls']:>8,.0f}{m['in'] / 1e6:>9.1f}{cached:>8}{m['out'] / 1e6:>8.2f}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("command", nargs="?", choices=["serve"])
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--since", default="", help="YYYY-MM-DD")
    parser.add_argument("--by", choices=["month", "day", "repo", "branch"], default="month")
    parser.add_argument("--budget", type=float, help="monthly USD budget to track on the dashboard")
    args = parser.parse_args()

    if args.command == "serve":
        global BUDGET
        BUDGET = args.budget
        try:
            serve(args.port)
        except KeyboardInterrupt:
            pass
    else:
        print_table(args.since, args.by)


if __name__ == "__main__":
    main()
