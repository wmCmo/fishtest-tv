#!/usr/bin/env python3
"""
Live dashboard for the Fishtest worker.

Read-only. Parses api.log / log.txt / the lock file on every poll and serves a
self-refreshing web page on http://127.0.0.1:8787 .

Usage:
    python monitor.py            # then open http://127.0.0.1:8787
    python monitor.py --port 9000
"""

import argparse
import hashlib
import hmac
import json
import secrets
import subprocess
import math
import os
import re
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# HERE はこのプロジェクト（モニター・TV・キャッシュ）、WORKER は Fishtest ワーカー本体の置き場所。
# ワーカーの場所は環境変数 FISHTEST_WORKER か --worker で変えられる
HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.environ.get("FISHTEST_WORKER") or r"C:\fishtest\worker"


def _worker_paths(worker):
    global WORKER, API_LOG, TXT_LOG, LOCK, CFG, TESTING, FISH_EXIT
    WORKER = worker
    API_LOG = os.path.join(WORKER, "api.log")
    TXT_LOG = os.path.join(WORKER, "log.txt")
    LOCK = os.path.join(WORKER, "fishtest_worker.lock")
    CFG = os.path.join(WORKER, "fishtest.cfg")
    TESTING = os.path.join(WORKER, "testing")
    # ワーカーはこのファイルを見つけると、終わった対局を送ってから止まる
    FISH_EXIT = os.path.join(WORKER, "fish.exit")


_worker_paths(WORKER)

# 2026-07-17 15:22:01.799109+00:00 :   5.20 ms (s)   823.35 ms (w)  https://.../api/update_task
RE_API = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+\+\d{2}:\d{2}) :\s+"
    r"(?P<s>[\d.]+) ms \(s\)\s+(?P<w>[\d.]+) ms \(w\)\s+.*?/api/(?P<ep>\w+)"
)
# 2026-... : run: <id> task: 299 size: 66 tc: 60+0.6 concurrency: 7 threads: 1 [ sprt : 800000 ]
RE_RUN = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+\+\d{2}:\d{2}) : run: (?P<run>\S+) "
    r"task: (?P<task>\d+) size: (?P<size>\d+) tc: (?P<tc>\S+) concurrency: (?P<conc>\d+) "
    r"threads: (?P<threads>\d+) \[ (?P<kind>\w+) : (?P<budget>\d+) \]"
)
RE_GAMES = re.compile(
    r"Games: (?P<g>\d+), Wins: (?P<w>\d+), Losses: (?P<l>\d+), Draws: (?P<d>\d+), "
    r"Points: (?P<pts>[\d.]+) \((?P<pct>[\d.]+) %\)"
)
RE_PTNML = re.compile(r"Ptnml\(0-2\): \[(?P<p>[\d, ]+)\]")
RE_LOS = re.compile(r"LOS: (?P<los>[\d.]+) %, DrawRatio: (?P<dr>[\d.]+) %")


def parse_ts(s):
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def tail_lines(path, max_bytes=400_000):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # drop partial line
            return f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def worker_pid():
    try:
        with open(LOCK) as f:
            return f.readline().strip()
    except OSError:
        return None


def read_cfg():
    out = {}
    try:
        with open(CFG) as f:
            for line in f:
                line = line.strip()
                if line.startswith("username"):
                    out["username"] = line.split("=", 1)[1].split(";")[0].strip()
                elif line.startswith("concurrency"):
                    out["concurrency"] = line.split("=", 1)[1].split(";")[0].strip()
                elif line.startswith("host"):
                    out["host"] = line.split("=", 1)[1].split(";")[0].strip()
    except OSError:
        pass
    return out


VENDOR = os.path.join(HERE, "vendor")
RESULT_TOKENS = {"1-0", "0-1", "1/2-1/2", "*"}
PGN_NAME = re.compile(r"^results-[0-9a-f]+-\d+\.pgn$")


def parse_eval(comment):
    """First token of a fastchess eval comment -> pawns (white-relative sign added later)."""
    toks = comment.split()
    if not toks:
        return None
    m = re.match(r"([+-]?)(M?)(\d+(?:\.\d+)?)", toks[0])
    if not m:
        return None
    sign = -1.0 if m.group(1) == "-" else 1.0
    val = 10.0 if m.group(2) == "M" else float(m.group(3))
    return sign * min(val, 10.0)


def parse_games(text, limit=25):
    chunks = [
        c
        for c in re.split(r"(?=^\[Event )", text, flags=re.M)
        if c.strip().startswith("[Event")
    ]
    games = []
    for c in chunks:
        hdr = dict(re.findall(r'^\[(\w+)\s+"([^"]*)"\]', c, flags=re.M))
        mt = re.sub(r"^\[.*\]\s*$", "", c, flags=re.M).strip()
        comments = re.findall(r"\{([^}]*)\}", mt)
        no_comments = re.sub(r"\{[^}]*\}", " ", mt)
        no_nums = re.sub(r"\d+\.(\.\.)?", " ", no_comments)
        sans = [t for t in no_nums.split() if t not in RESULT_TOKENS]
        fen = hdr.get("FEN", "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1")
        stm = fen.split()[1] if len(fen.split()) > 1 else "w"
        evals = []
        for i, cm in enumerate(comments[: len(sans)]):
            ev = parse_eval(cm)
            if ev is None:
                evals.append(None)
                continue
            color = stm if i % 2 == 0 else ("b" if stm == "w" else "w")
            evals.append(round(ev if color == "w" else -ev, 2))
        games.append(
            {
                "event": hdr.get("Event", ""),
                "white": hdr.get("White", ""),
                "black": hdr.get("Black", ""),
                "result": hdr.get("Result", "*"),
                "fen": fen,
                "termination": hdr.get("Termination", ""),
                "sans": sans,
                "evals": evals,
                "plies": len(sans),
            }
        )
    return games[-limit:]


def load_games(name):
    """Safely load parsed games for a results-*.pgn file inside testing/."""
    if not PGN_NAME.match(name or ""):
        return None
    path = os.path.join(TESTING, name)
    if os.path.dirname(os.path.abspath(path)) != os.path.abspath(TESTING):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return parse_games(f.read())
    except OSError:
        return None


def count_games(path):
    """Number of games written to a PGN so far (one [Event per finished game)."""
    try:
        with open(path, "rb") as f:
            return f.read().count(b"[Event ")
    except OSError:
        return None


def collect_results(current):
    """Live W/L/D, points, LOS and pentanomial for the CURRENT batch, from its PGN.

    Scored from the patch ('New-') engine's perspective vs base. Returns None when
    the task writes no PGN (e.g. SPSA) — there is no local per-game result then.
    """
    if not current:
        return None
    path = os.path.join(TESTING, f"results-{current['run']}-{current['task']}.pgn")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    whites = re.findall(r'\[White "([^"]+)"\]', text)
    results = re.findall(r'\[Result "([^"]+)"\]', text)
    n = min(len(whites), len(results))
    if n == 0:
        return None
    w = l = d = 0
    scores = []  # patch's per-game score, in file order
    for i in range(n):
        new_white = whites[i].startswith("New")
        r = results[i]
        if r == "1/2-1/2":
            d += 1
            scores.append(0.5)
        elif r in ("1-0", "0-1"):
            white_won = r == "1-0"
            new_won = white_won == new_white  # patch won iff it played the winning side
            if new_won:
                w += 1
                scores.append(1.0)
            else:
                l += 1
                scores.append(0.0)
        else:
            scores.append(None)
    total = w + l + d
    if total == 0:
        return None
    points = w + 0.5 * d
    # pentanomial over complete game-pairs (fastchess writes paired games adjacently)
    ptnml = [0, 0, 0, 0, 0]
    for i in range(0, n - 1, 2):
        a, b = scores[i], scores[i + 1]
        if a is None or b is None:
            continue
        ptnml[int(round((a + b) * 2))] += 1
    los = None
    if w + l > 0:
        los = round(50 * (1 + math.erf((w - l) / math.sqrt(2.0 * (w + l)))), 2)
    return {
        "games": total, "wins": w, "losses": l, "draws": d,
        "points": points, "pct": round(100 * points / total, 2),
        "ptnml": ptnml, "los": los,
        "draw_ratio": round(100 * d / total, 2),
    }


# rolling (time, completed) samples for the current batch, to estimate rate
_PROG_LOCK = threading.Lock()
_PROG_HIST = {"key": None, "samples": []}


def progress_rate(key, completed, now_epoch, window=600):
    """Games/second over the last `window` seconds; None until we have a span."""
    with _PROG_LOCK:
        h = _PROG_HIST
        if h["key"] != key:
            h["key"] = key
            h["samples"] = []
        h["samples"].append((now_epoch, completed))
        cutoff = now_epoch - window
        h["samples"] = [s for s in h["samples"] if s[0] >= cutoff]
        if len(h["samples"]) >= 2:
            t0, c0 = h["samples"][0]
            t1, c1 = h["samples"][-1]
            if t1 > t0 and c1 > c0:
                return (c1 - c0) / (t1 - t0)
    return None


_PGN_TIME_FMT = "%Y-%m-%dT%H:%M:%S %z"


def machine_games_per_sec():
    """Real steady-state throughput (games/sec) measured from recent results PGNs.

    Uses first GameStartTime -> last GameEndTime span, so it captures actual
    parallelism/adjudication on THIS machine. Median over recent files. Used to
    estimate progress for tasks that write no PGN (SPSA). None if no sample.
    """
    try:
        pgns = sorted(
            (f for f in os.listdir(TESTING)
             if f.startswith("results-") and f.endswith(".pgn")),
            key=lambda n: os.path.getmtime(os.path.join(TESTING, n)),
            reverse=True,
        )
    except OSError:
        return None
    rates = []
    for name in pgns[:5]:
        try:
            with open(os.path.join(TESTING, name), encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        n = text.count("[Event ")
        starts = re.findall(r'\[GameStartTime "([^"]+)"\]', text)
        ends = re.findall(r'\[GameEndTime "([^"]+)"\]', text)
        if n < 10 or not starts or not ends:
            continue
        try:
            span = (datetime.strptime(ends[-1], _PGN_TIME_FMT)
                    - datetime.strptime(starts[0], _PGN_TIME_FMT)).total_seconds()
        except ValueError:
            continue
        if span > 0:
            rates.append(n / span)
    if not rates:
        return None
    rates.sort()
    return rates[len(rates) // 2]  # median


def collect_progress(current, now):
    """Percent + ETA for the current batch.

    SPRT tasks write a per-game PGN -> exact game-count progress + ETA.
    SPSA tasks write nothing locally -> fall back to a time-based estimate
    calibrated from recent games' wall-clock duration.
    """
    if not current:
        return None
    size = current.get("size") or 0
    elapsed = current.get("age_s") or 0
    kind = current.get("kind")
    pgn = f"results-{current['run']}-{current['task']}.pgn"
    completed = count_games(os.path.join(TESTING, pgn))  # None if no PGN (SPSA)

    if completed is not None:
        key = f"{current['run']}-{current['task']}"
        rate = progress_rate(key, completed, now.timestamp())      # windowed g/s
        if not rate and elapsed > 0 and completed > 0:
            rate = completed / elapsed                             # fallback avg
        remaining = max(size - completed, 0)
        eta = remaining / rate if rate else None
        return {
            "mode": "games", "kind": kind,
            "completed": completed, "size": size,
            "pct": round(100 * completed / size, 1) if size else 0,
            "rate_per_min": round(rate * 60, 1) if rate else None,
            "eta_s": round(eta) if eta else None,
            "elapsed_s": round(elapsed) if elapsed else None,
        }

    # time-based estimate (no local game count, e.g. SPSA), from measured throughput
    rate = machine_games_per_sec()  # games/sec
    pct = eta = est_done = None
    if rate and size:
        est_done = min(rate * elapsed, size)
        pct = round(min(99.0, 100 * est_done / size), 1)
        eta = max(size / rate - elapsed, 0)
    return {
        "mode": "time", "kind": kind,
        "completed": None, "size": size,
        "pct": pct,
        "est_done": round(est_done) if est_done is not None else None,
        "rate_per_min": round(rate * 60, 1) if rate else None,
        "eta_s": round(eta) if eta is not None else None,
        "elapsed_s": round(elapsed) if elapsed else None,
    }


CAT_COLORS = {
    "NNUE nets": "#4cb26a",
    "opening books": "#5aa9f0",
    "engine binaries": "#b98cff",
    "PGN results": "#e2a13a",
    "fastchess (tool)": "#5c6b63",
    "config": "#8a9691",
    "other": "#3a4c42",
}


def categorize(name):
    if name.startswith("stockfish-") and name.endswith(".exe"):
        return "engine binaries"
    if name.startswith("nn-") and name.endswith(".nnue"):
        return "NNUE nets"
    if name.startswith("results-") and name.endswith(".pgn"):
        return "PGN results"
    if name.endswith(".epd"):
        return "opening books"
    if name == "fastchess.exe":
        return "fastchess (tool)"
    if name == "config.json":
        return "config"
    return "other"


def collect_testing(current):
    """Size composition of the testing/ folder + keep-vs-deletable classification.

    'Keep' = files the current task actually references (from config.json and the
    live api.log run). Everything else is a leftover the worker will re-download
    on demand, so it is safe to delete when the worker is idle.
    """
    try:
        names = [
            n for n in os.listdir(TESTING) if os.path.isfile(os.path.join(TESTING, n))
        ]
    except OSError:
        return None

    keep = {"config.json", "fastchess.exe"}
    try:
        with open(os.path.join(TESTING, "config.json")) as f:
            cfg = json.load(f)
        for e in cfg.get("engines", []):
            if e.get("cmd"):
                keep.add(os.path.basename(e["cmd"]))
            for k, v in e.get("options", []):
                if k == "EvalFile":
                    keep.add(v)
        if cfg.get("opening", {}).get("file"):
            keep.add(cfg["opening"]["file"])
        if cfg.get("pgn", {}).get("file"):
            keep.add(cfg["pgn"]["file"])
    except (OSError, ValueError, KeyError):
        pass
    if current:
        keep.add(f"results-{current['run']}-{current['task']}.pgn")

    cats = {}
    files = []
    for n in names:
        s = os.path.getsize(os.path.join(TESTING, n))
        c = categorize(n)
        d = cats.setdefault(
            c, {"name": c, "size": 0, "count": 0, "color": CAT_COLORS.get(c, "#3a4c42")}
        )
        d["size"] += s
        d["count"] += 1
        files.append({"name": n, "size": s, "cat": c, "keep": n in keep})

    total = sum(d["size"] for d in cats.values())
    deletable = sorted((f for f in files if not f["keep"]), key=lambda x: -x["size"])
    keepable = sorted((f for f in files if f["keep"]), key=lambda x: -x["size"])
    return {
        "path": TESTING,
        "categories": sorted(cats.values(), key=lambda x: -x["size"]),
        "total": total,
        "reclaim": sum(f["size"] for f in deletable),
        "delcount": len(deletable),
        "filecount": len(files),
        "deletable": deletable[:50],
        "keepnames": [f["name"] for f in keepable],
    }


def collect():
    now = datetime.now(timezone.utc)
    lines = tail_lines(API_LOG)

    events = []  # recent timeline points
    latency = []  # {t, s, w, ep}
    ep_counts = {}
    last_beat = None
    current = None

    for line in lines:
        m = RE_RUN.match(line)
        if m:
            ts = parse_ts(m["ts"])
            current = {
                "run": m["run"],
                "task": int(m["task"]),
                "size": int(m["size"]),
                "tc": m["tc"],
                "concurrency": int(m["conc"]),
                "threads": int(m["threads"]),
                "kind": m["kind"],
                "budget": int(m["budget"]),
                "started": m["ts"],
                "age_s": (now - ts).total_seconds() if ts else None,
            }
            events.append({"t": m["ts"], "ep": "new_task"})
            continue
        m = RE_API.match(line)
        if m:
            ts = parse_ts(m["ts"])
            ep = m["ep"]
            ep_counts[ep] = ep_counts.get(ep, 0) + 1
            latency.append(
                {"t": m["ts"], "s": float(m["s"]), "w": float(m["w"]), "ep": ep}
            )
            events.append({"t": m["ts"], "ep": ep})
            if ep == "beat":
                last_beat = ts

    # live game results for the current batch, computed from its PGN
    res = collect_results(current)
    if res:
        game_stats = {
            "games": res["games"], "wins": res["wins"], "losses": res["losses"],
            "draws": res["draws"], "points": res["points"], "pct": res["pct"],
        }
        ptnml = res["ptnml"]
        los = {"los": res["los"], "draw_ratio": res["draw_ratio"]} if res["los"] is not None else None
    else:
        game_stats = ptnml = los = None

    # freshness / alive
    last_event_ts = (
        parse_ts(latency[-1]["t"])
        if latency
        else (parse_ts(events[-1]["t"]) if events else None)
    )
    last_age = (now - last_event_ts).total_seconds() if last_event_ts else None
    beat_age = (now - last_beat).total_seconds() if last_beat else None
    pid = worker_pid()
    alive = pid is not None and (last_age is not None and last_age < 300)

    # pgn result files (recent activity on disk)
    pgns = []
    try:
        for name in os.listdir(TESTING):
            if name.startswith("results-") and name.endswith(".pgn"):
                fp = os.path.join(TESTING, name)
                st = os.stat(fp)
                pgns.append({"name": name, "mtime": st.st_mtime, "size": st.st_size})
        pgns.sort(key=lambda x: x["mtime"], reverse=True)
        pgns = pgns[:6]
    except OSError:
        pass

    return {
        "now": now.isoformat(),
        "cfg": read_cfg(),
        "pid": pid,
        "alive": alive,
        "current": current,
        "last_age_s": last_age,
        "beat_age_s": beat_age,
        "ep_counts": ep_counts,
        "latency": latency[-60:],
        "events": events[-40:],
        "game_stats": game_stats,
        "ptnml": ptnml,
        "los": los,
        "pgns": pgns,
        "progress": collect_progress(current, now),
        "testing": collect_testing(current),
    }


# タブのアイコン（Stockfish にちなんだ魚）。モニターは暗い地に緑、TV は Duolingo 風の緑地に白で見分けられるようにする
_FISH = ('<path d="M7 32c7-11 20-15 33-10l14-9v38l-14-9C27 47 14 43 7 32z" fill="{fish}"/>'
         '<circle cx="18" cy="29" r="3.2" fill="{bg}"/>')
FAVICONS = {
    "/favicon.svg": ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
                     '<rect width="64" height="64" rx="14" fill="#0a0e0d"/>'
                     + _FISH.format(fish="#5fe08a", bg="#0a0e0d") + '</svg>'),
    "/favicon-tv.svg": ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
                        '<rect width="64" height="64" rx="14" fill="#58cc02"/>'
                        + _FISH.format(fish="#ffffff", bg="#58cc02") + '</svg>'),
}


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Stockfish · Worker Monitor</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<style>
:root{
  color-scheme:dark;
  --bg:#0a0e0d; --panel:#101613; --panel2:#0d1310; --line:#1d2a23;
  --text:#e7efe9; --muted:#7d8f85;
  --green:#4cb26a; --green-b:#5fe08a; --glow:rgba(76,178,106,.35);
  --red:#f0645b; --amber:#e2a13a; --blue:#5aa9f0; --violet:#b98cff;
  --font:"Segoe UI",-apple-system,BlinkMacSystemFont,Roboto,Helvetica,Arial,sans-serif;
}
*{box-sizing:border-box}
body{margin:0;font-family:var(--font);background:var(--bg);color:var(--text);
  background-image:radial-gradient(rgba(255,255,255,.06) 1px,transparent 1px);
  background-size:24px 24px}
a{color:var(--green-b);text-decoration:none}
/* スマホのブラウザが文字を勝手に拡大して横にはみ出さないように */
html{-webkit-text-size-adjust:100%;text-size-adjust:100%}

header{display:flex;flex-wrap:wrap;align-items:center;gap:12px 16px;padding:20px 28px;
  border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5;
  background:linear-gradient(180deg,rgba(10,14,13,.97),rgba(10,14,13,.82));
  backdrop-filter:blur(8px)}
.logo{width:34px;height:34px;border-radius:9px;display:grid;place-items:center;font-size:19px;
  background:radial-gradient(circle at 35% 30%,#183123,#0c1a12);
  border:1px solid #274a35;box-shadow:0 0 22px var(--glow)}
.brand{display:flex;flex-direction:column;line-height:1.15}
.brand b{font-size:16px;font-weight:700;color:var(--green-b);letter-spacing:.2px}
.brand small{font-size:11.5px;color:var(--muted);letter-spacing:.14em;text-transform:uppercase}
.livewrap{display:flex;align-items:center;gap:9px;margin-left:6px;padding:6px 13px;border-radius:20px;
  background:var(--panel);border:1px solid var(--line);font-size:13px;font-weight:600}
.dot{width:9px;height:9px;border-radius:50%}
.on{background:var(--green);box-shadow:0 0 0 4px rgba(76,178,106,.18),0 0 12px var(--green);animation:pulse 2s infinite}
.off{background:var(--red);box-shadow:0 0 0 4px rgba(240,100,91,.18)}
@keyframes pulse{50%{opacity:.45}}
.meta{margin-left:auto;min-width:0;text-align:right;font-size:12.5px;color:var(--muted);overflow-wrap:anywhere}
.meta b{color:var(--text);font-weight:600}

.helpbar{padding:14px 28px;border-bottom:1px solid var(--line);background:var(--panel2)}
.helpbar button{all:unset;cursor:pointer;color:var(--green-b);font-weight:600;font-size:13px}
.helpbar .body{max-height:0;overflow:hidden;transition:max-height .3s ease;color:var(--muted);font-size:13.5px;line-height:1.6}
.helpbar.open .body{max-height:340px;margin-top:10px}
.helpbar .body b{color:var(--text)}

main{padding:24px 28px 0}
#stats{display:grid;gap:16px;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));
  max-width:1280px;margin:0 auto}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px 18px 16px;
  position:relative;transition:border-color .2s,transform .2s}
.card:hover{border-color:#2c4536;transform:translateY(-2px)}
.card h2{font-size:11px;letter-spacing:.11em;text-transform:uppercase;color:var(--muted);
  margin:0 0 14px;display:flex;align-items:center;gap:8px}
.q{margin-left:auto;width:19px;height:19px;border-radius:50%;border:1px solid var(--line);
  color:var(--muted);font-size:12px;font-weight:700;cursor:pointer;display:grid;place-items:center;
  transition:.15s;background:transparent;font-family:var(--font)}
.q:hover{color:var(--green-b);border-color:var(--green);box-shadow:0 0 10px var(--glow)}
.exp{max-height:0;overflow:hidden;transition:max-height .3s ease;
  font-size:12.5px;line-height:1.6;color:var(--muted)}
.exp.open{max-height:420px;margin:2px 0 14px;padding:12px 13px;background:var(--panel2);
  border:1px solid var(--line);border-radius:9px}
.exp b{color:var(--text)}

.big{font-size:28px;font-weight:700;color:var(--text);letter-spacing:-.5px}
.big.g{color:var(--green-b)}
.sub{font-size:12.5px;color:var(--muted);margin-top:3px}
.kv{display:flex;justify-content:space-between;gap:10px;padding:5px 0;border-bottom:1px solid var(--line);font-size:13.5px}
.kv:last-child{border:0}
.kv span:first-child{color:var(--muted)}
.kv .term{border-bottom:1px dotted #3a4c42;cursor:help}
.pill{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:20px;font-size:12px;
  background:var(--panel2);border:1px solid var(--line);margin:3px 3px 0 0}
.pill i{width:7px;height:7px;border-radius:50%;font-style:normal}
.bar{height:24px;display:flex;border-radius:7px;overflow:hidden;margin-top:12px;border:1px solid var(--line)}
.bar>div{display:flex;align-items:center;justify-content:center;font-size:11.5px;color:#06120b;font-weight:700;min-width:0}
.w{background:var(--green)}.d{background:#5c6b63}.l{background:var(--red)}
.pbar{height:15px;border-radius:8px;background:var(--panel2);border:1px solid var(--line);overflow:hidden;margin:12px 0 6px}
.pbar>i{display:block;height:100%;width:0;border-radius:8px;
  background:linear-gradient(90deg,var(--green),var(--green-b));transition:width .6s ease}
svg{width:100%;height:66px;display:block}
.evt{font-size:12.5px;max-height:230px;overflow:auto}
.evt div{display:flex;align-items:center;gap:10px;padding:4px 0;border-bottom:1px solid var(--line)}
.evt .tm{margin-left:auto;color:var(--muted);font-variant-numeric:tabular-nums}
.tag{padding:1px 9px;border-radius:5px;font-size:11px;font-weight:600;background:var(--panel2);border:1px solid var(--line)}
.beat{color:var(--blue)}.update_task{color:var(--amber)}
.request_task,.new_task,.request_version{color:var(--green-b)}
.upload_pgn{color:var(--violet)}.failed_task,.request_spsa{color:var(--red)}
.wide{grid-column:1/-1}
#viewer{max-width:1280px;margin:16px auto 28px}
.tinytag{font-size:10px;letter-spacing:.06em;color:var(--amber);border:1px solid var(--line);
  border-radius:20px;padding:2px 9px;text-transform:none}
.vwrap{display:flex;gap:24px;flex-wrap:wrap;align-items:flex-start}
.board{width:min(384px,84vw);aspect-ratio:1;display:grid;
  grid-template-columns:repeat(8,1fr);grid-template-rows:repeat(8,1fr);
  border:1px solid var(--line);border-radius:10px;overflow:hidden;flex:none;
  box-shadow:0 0 24px rgba(0,0,0,.35)}
.board .sq{display:flex;align-items:center;justify-content:center;
  font-size:clamp(20px,4.6vw,32px);line-height:1;user-select:none}
.board .lt{background:#bcc6a9}.board .dk{background:#4d6c4b}
.board .wp{color:#fbfdf6;text-shadow:0 1px 2px rgba(0,0,0,.6)}
.board .bp{color:#141815;text-shadow:0 1px 1px rgba(255,255,255,.22)}
.vside{flex:1;min-width:248px;display:flex;flex-direction:column;gap:13px}
.vsel{display:flex;gap:8px}
.vsel select{flex:1;background:var(--panel2);color:var(--text);border:1px solid var(--line);
  border-radius:8px;padding:8px 10px;font-family:var(--font);font-size:12.5px}
.vsel button,.controls button{background:var(--panel2);color:var(--text);border:1px solid var(--line);
  border-radius:8px;padding:8px 11px;cursor:pointer;font-family:var(--font);font-size:12.5px}
.vsel button:hover,.controls button:hover{border-color:var(--green);color:var(--green-b)}
#playBtn{background:#16281c;border-color:#2c4a35;color:var(--green-b);font-weight:600}
.ginfo{font-size:13px;color:var(--muted);line-height:1.5}
.ginfo b{color:var(--text)}
.evalwrap{position:relative;background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:8px}
#evalGraph{width:100%;height:80px;display:block}
.evalnow{position:absolute;top:7px;right:11px;font-size:13px;font-weight:600;color:var(--text);
  font-variant-numeric:tabular-nums}
.controls{display:flex;flex-wrap:wrap;gap:6px}
.plabel{font-size:12.5px;color:var(--muted);font-variant-numeric:tabular-nums}
.disk{display:flex;gap:22px;flex-wrap:wrap;align-items:center}
.legend{flex:1;min-width:220px;display:flex;flex-direction:column;gap:6px}
.legend .row{display:flex;align-items:center;gap:9px;font-size:13px}
.legend .row i{width:11px;height:11px;border-radius:3px;flex:none}
.legend .row .sz{margin-left:auto;color:var(--muted);font-variant-numeric:tabular-nums}
.cleanup{margin-top:16px;padding:14px;border:1px solid var(--line);border-radius:10px;background:var(--panel2)}
.cleanup .hd{font-size:14px;font-weight:600;margin-bottom:8px}
.cleanup .hd b{color:var(--green-b)}
.cmd{background:#070b0a;border:1px solid var(--line);border-radius:8px;padding:11px 13px;
  font-family:ui-monospace,Consolas,monospace;font-size:12px;color:var(--green-b);
  white-space:pre-wrap;word-break:break-all;position:relative;margin-top:8px}
.cmd button{position:absolute;top:7px;right:7px;font-family:var(--font);cursor:pointer;
  background:var(--panel);border:1px solid var(--line);color:var(--text);border-radius:6px;
  font-size:11px;padding:3px 9px}
.cmd button:hover{border-color:var(--green)}
.warn{color:var(--amber);font-size:12px;margin-top:9px}
.dellist{max-height:220px;overflow:auto;margin-top:10px;font-size:12.5px}
.dellist div{display:flex;gap:10px;padding:3px 0;border-bottom:1px solid var(--line)}
.dellist .kept{color:var(--green-b)}
.dellist .sz{margin-left:auto;color:var(--muted);font-variant-numeric:tabular-nums}
code{color:var(--green-b);font-family:ui-monospace,Consolas,monospace;font-size:12.5px}
.empty{color:var(--muted);font-size:13px}
.trophy{max-width:1280px;margin:0 auto 16px;background:linear-gradient(135deg,#14201a 0%,var(--panel) 60%);border-color:#2a4a36}
.trophy .head{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
.trophy .cup{font-size:34px;line-height:1;filter:drop-shadow(0 0 12px rgba(226,161,58,.45))}
.trophy .big{font-size:30px}
.trophy .tiles{display:grid;gap:10px;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));margin-top:16px}
.trophy .tile{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.trophy .tile b{display:block;font-size:20px;color:var(--text)}
.trophy .tile .lbl{font-size:11px;letter-spacing:.09em;text-transform:uppercase;color:var(--muted);display:flex;align-items:center;gap:6px}
.trophy .tile .lbl i{width:8px;height:8px;border-radius:50%;font-style:normal}
.trophy .merged{margin-top:14px;display:flex;flex-direction:column;gap:8px}
.trophy .mrow{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;font-size:13.5px;padding:9px 12px;
  border-radius:10px;background:rgba(95,224,138,.07);border:1px solid #24402f}
.trophy .mrow code{color:var(--green-b)}
/* スマホ：余白を詰め、ヘッダーは固定せず、右上の情報は下の行に回す */
@media (max-width:640px){
  header{position:static;padding:14px 16px}
  .meta{margin-left:0;flex-basis:100%;text-align:left}
  .helpbar{padding:12px 16px}
  main{padding:16px 16px 0}
  #stats{grid-template-columns:1fr}
  .trophy .big{font-size:24px}
  .trophy .cup{font-size:28px}
  .trophy .tiles{grid-template-columns:1fr 1fr;gap:8px}
  .trophy .tile{padding:9px 10px}
  .trophy .tile b{font-size:17px}
  .vside{min-width:0}
}
::-webkit-scrollbar{width:9px;height:9px}::-webkit-scrollbar-thumb{background:#243428;border-radius:5px}
</style></head><body>

<header>
  <div class="logo">🐟</div>
  <div class="brand"><b>Stockfish</b><small>Worker Monitor</small></div>
  <div class="livewrap"><span id="dot" class="dot off"></span><span id="state">—</span></div>
  <div class="meta" id="meta"></div>
</header>

<div class="helpbar" id="helpbar">
  <button onclick="document.getElementById('helpbar').classList.toggle('open')">
    ❔ What am I looking at? &nbsp;<span style="color:var(--muted);font-weight:400">(click to explain)</span></button>
  <div class="body">
    This machine is a <b>Fishtest worker</b> — it volunteers your CPU cores to test proposed
    improvements to the <b>Stockfish</b> chess engine. The server hands your worker a <b>task</b>
    (a batch of games) from a larger <b>test run</b>; your worker plays them with
    <b>fastchess</b>, reports progress, and uploads the results. A test passes or fails by
    <b>SPRT</b>, a statistical stopping rule. Every card below has a <b>?</b> — click it for a
    plain-English explanation of that panel. Data refreshes every 2&nbsp;seconds from your local
    <code>api.log</code> and <code>log.txt</code>.
  </div>
</div>

<main>
  <section id="trophy" class="card trophy" style="display:none"></section>
  <div id="stats"></div>
  <section id="viewer" class="card wide">
    <h2>Game Viewer <span class="tinytag">eye-candy, not a metric</span>
      <button class="q" data-k="board">?</button></h2>
    <div class="exp" id="exp-board">A replay of the actual games your worker played (engine vs engine).
      <b>This is delight, not a metric</b> — one game tells you nothing about whether the patch is good;
      the aggregate stats do. Games begin from a Fishtest opening-book position (hence the mid-game start).
      The graph is each position's <b>evaluation</b> (white's perspective, in pawns) as the game progressed —
      watch it swing. Positions are computed locally by a vendored copy of chess.js.</div>
    <div class="vwrap">
      <div class="board" id="board"></div>
      <div class="vside">
        <div class="vsel">
          <select id="fileSel"></select>
          <button id="refreshFiles" title="reload PGN file list">&#8635;</button>
        </div>
        <div class="ginfo" id="ginfo">loading…</div>
        <div class="evalwrap"><svg id="evalGraph" viewBox="0 0 300 80" preserveAspectRatio="none"></svg>
          <div class="evalnow" id="evalNow">&mdash;</div></div>
        <div class="controls">
          <button data-g="-1" title="previous game">&#9198; game</button>
          <button data-p="first" title="start">&#9198;</button>
          <button data-p="prev" title="previous move">&#9194;</button>
          <button id="playBtn" data-p="play">&#9654; play</button>
          <button data-p="next" title="next move">&#9193;</button>
          <button data-p="last" title="end">&#9197;</button>
          <button data-g="1" title="next game">game &#9197;</button>
        </div>
        <div class="plabel" id="plabel">&mdash;</div>
      </div>
    </div>
  </section>
</main>

<script type="module">
import { Chess } from "/vendor/chess.js";

const EXP={
 progress:"How far the current batch (task) is from done. <b>SPRT</b> tasks write a per-game PGN, so this is an exact <b>completed / total</b> count with an ETA from the recent games/min rate. <b>SPSA</b> tuning tasks write nothing locally (games are reported only to the server), so there's no per-game count — the bar switches to an <b>amber time estimate</b>: elapsed vs. an expected duration calibrated from your recent games' wall-clock length. Time estimates are rough (game lengths vary, and tasks can stop early), so treat the SPSA percentage as a ballpark.",
 status:"<b>Running</b> means the worker process (PID) holds the lock file and the log has updated within the last 5 minutes. <b>Heartbeat</b> is the periodic <code>/api/beat</code> ping that tells the Fishtest server your worker is still alive — if it stops, the server reassigns your task to someone else.",
 task:"Fishtest breaks each <b>run</b> (one proposed engine change) into <b>tasks</b> — batches of games handed to individual workers. <b>size</b> = games in this batch. <b>tc</b> = time control: base seconds + increment per move (60+0.6 = 60s + 0.6s/move). <b>threads × concurrency</b> = engine threads per game × games played in parallel. <b>SPRT</b> = Sequential Probability Ratio Test; the budget is the max games before it decides.",
 games:"Results of the <b>current batch</b>, scored live from its PGN, from the patch's ('New') perspective vs base. <b>Points</b>: win=1, draw=0.5 — over 50% means the patch is scoring better. <b>LOS</b> (Likelihood of Superiority) = probability the patch is genuinely stronger. <b>Draw ratio</b> = share of games drawn. <b>Ptnml(0-2)</b> = the pentanomial tally: complete game-pairs bucketed by combined result [0, ½, 1, 1½, 2] — Fishtest's more accurate variance model. <span style='color:var(--amber)'>SPSA tasks write no PGN, so this card is hidden for them.</span>",
 api:"How many calls your worker made to the Fishtest server in the recent log window. <b>beat</b>=heartbeat, <b>update_task</b>=progress report, <b>request_task</b>=fetch next batch, <b>upload_pgn</b>=send finished games, <b>request_version</b>=version check, <b>failed_task</b>=a task that errored out.",
 lat:"Round-trip time of server calls. <b>Wall</b> = total time your worker waited (includes network). <b>Server</b> = time the Fishtest server itself spent. Occasional spikes are network hiccups, not worker problems.",
 events:"A live tail of every API call your worker makes, newest first — the raw pulse of the worker talking to the server.",
 pgn:"<b>PGN</b> files are the game records fastchess writes to disk as each batch finishes, then uploads to the server. Newest first, with file size and age.",
 disk:"What the <code>testing/</code> folder is made of, by size. <b>NNUE nets</b> (the engine's neural-network eval files) and <b>opening books</b> dominate. <b>Can I tell which file 'passed'?</b> No — the <code>stockfish-&lt;sha&gt;.exe</code> files are just candidate builds; whether a patch helps is an <b>SPRT verdict decided server-side</b> across all workers, never stored locally. <b>Safe to delete?</b> Yes — anything not referenced by the current task is a leftover the worker re-downloads on demand. Green-outlined files are in use (keep); the rest are reclaimable. <span style='color:var(--amber)'>Only delete while the worker is idle — a running task holds its files open.</span>"
};
const TIP={
 tc:"Time control: base seconds + increment added per move.",
 sprt:"Sequential Probability Ratio Test — stops as soon as the result is statistically clear.",
 los:"Likelihood of Superiority: chance the new version is truly stronger.",
 ptnml:"Pentanomial: game-pairs bucketed by combined score [0,½,1,1½,2].",
 conc:"Engine threads per game × games running in parallel.",
 size:"Number of games in this task batch.",
 trophy:"Every test your worker played games for since your local record starts, grouped by what happened to it. <b>In Stockfish</b> = the patch's branch was later merged into Stockfish master (matched by commit message, checked once a day). <b>Passed, not merged</b> = it passed SPRT but no matching master commit yet (often an STC pass waiting for LTC, or still in review). <b>Still undecided</b> = your worker already played its share of games for these tests, but the tests themselves are still running on Fishtest with other people's workers (your PC plays one batch at a time). <b>Not merged</b> = it failed or was stopped. <b>CPU-hours</b> = games × the time control's expected game length × threads, i.e. the compute your machine donated."
};
const fmtAge=s=>s==null?"—":s<90?`${s|0}s ago`:s<5400?`${(s/60)|0}m ago`:`${(s/3600).toFixed(1)}h ago`;
const fmtDur=s=>{if(s==null)return"—";s=Math.round(s);if(s<60)return s+"s";const m=Math.floor(s/60);if(m<60)return m+"m "+(s%60)+"s";const h=Math.floor(m/60);return h+"h "+(m%60)+"m";};
const esc=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const escAttr=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function card(key,title,body){
  return `<div class="card"><h2>${title}<button class="q" data-k="${key}">?</button></h2>
    <div class="exp" id="exp-${key}">${EXP[key]||""}</div>${body}</div>`;
}
function spark(pts,k,color,label){
  if(!pts.length)return`<div class="empty">no data yet</div>`;
  const v=pts.map(p=>p[k]),max=Math.max(...v,1),W=280,H=58,step=W/Math.max(pts.length-1,1);
  const line=v.map((y,i)=>`${i?"L":"M"}${(i*step).toFixed(1)},${(H-(y/max)*H).toFixed(1)}`).join(" ");
  const area=`M0,${H} `+v.map((y,i)=>`L${(i*step).toFixed(1)},${(H-(y/max)*H).toFixed(1)}`).join(" ")+` L${W},${H} Z`;
  const id="g"+k;
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    <defs><linearGradient id="${id}" x1="0" x2="0" y1="0" y2="1">
      <stop offset="0" stop-color="${color}" stop-opacity=".35"/>
      <stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient></defs>
    <path d="${area}" fill="url(#${id})"/>
    <path d="${line}" fill="none" stroke="${color}" stroke-width="1.8"/></svg>
    <div class="sub">${label} · peak ${max.toFixed(0)} ms · ${pts.length} calls</div>`;
}
const term=(t,k)=>`<span class="term" title="${TIP[k]}">${t}</span>`;
const mb=b=>b>=1e6?`${(b/1e6).toFixed(0)} MB`:`${(b/1e3).toFixed(0)} KB`;

function donut(cats,total){
  if(!total)return`<div class="empty">empty folder</div>`;
  const r=58,C=2*Math.PI*r;let off=0;
  const rings=cats.map(c=>{
    const len=c.size/total*C;
    const el=`<circle cx="80" cy="80" r="${r}" fill="none" stroke="${c.color}" stroke-width="22"
      stroke-dasharray="${len.toFixed(2)} ${(C-len).toFixed(2)}" stroke-dashoffset="${(-off).toFixed(2)}"
      transform="rotate(-90 80 80)"><title>${c.name}: ${mb(c.size)} (${(100*c.size/total).toFixed(1)}%)</title></circle>`;
    off+=len;return el;
  }).join("");
  return `<svg viewBox="0 0 160 160" style="width:150px;height:150px;flex:none">${rings}
    <text x="80" y="76" text-anchor="middle" fill="var(--text)" font-size="24" font-weight="700"
      font-family="var(--font)">${(total/1e6).toFixed(0)}</text>
    <text x="80" y="94" text-anchor="middle" fill="var(--muted)" font-size="10"
      font-family="var(--font)">MB total</text></svg>`;
}

async function tick(){
  let d;try{d=await(await fetch("/data")).json()}catch(e){return}
  document.getElementById("dot").className="dot "+(d.alive?"on":"off");
  document.getElementById("state").textContent=d.alive?"RUNNING":"IDLE";
  const c=d.cfg;
  document.getElementById("meta").innerHTML=
    `<b>${esc(c.username||"?")}</b> · ${esc(c.host||"?")}<br>${c.concurrency||"?"} cores · pid ${d.pid||"—"} · ${new Date(d.now).toLocaleTimeString()}`;

  const gs=d.game_stats,el=[];

  el.push(card("status","Status",
    `<div class="big ${d.alive?'g':''}">${d.alive?"RUNNING":"IDLE / STOPPED"}</div>
     <div class="sub">worker heartbeat & liveness</div>
     <div style="margin-top:12px">
       <div class="kv"><span>Last event</span><span>${fmtAge(d.last_age_s)}</span></div>
       <div class="kv"><span>Last heartbeat</span><span>${fmtAge(d.beat_age_s)}</span></div></div>`));

  if(d.progress){const pg=d.progress,isTime=pg.mode==="time",pct=pg.pct;
    const col=isTime?"var(--amber)":"var(--green-b)";
    const bar=(pct!=null)
      ?`<div class="big" style="color:${col}">${pct}%${isTime?'<span style="font-size:13px;color:var(--muted)"> est</span>':''}</div>
        <div class="pbar"><i style="width:${Math.min(pct,100)}%${isTime?';background:linear-gradient(90deg,#a2761f,var(--amber))':''}"></i></div>`
      :`<div class="big" style="color:var(--muted)">—</div>`;
    const rows=isTime
      ?`<div class="sub">${esc((pg.kind||"task").toUpperCase())} · estimate from measured throughput</div>
        <div style="margin-top:11px">
          <div class="kv"><span>Est. time left</span><span>${pg.eta_s!=null?"~"+fmtDur(pg.eta_s):"—"}</span></div>
          <div class="kv"><span>Est. games done</span><span>${pg.est_done!=null?"≈ "+pg.est_done+" / "+pg.size:"—"}</span></div>
          <div class="kv"><span>Rate</span><span>${pg.rate_per_min!=null?pg.rate_per_min+" games/min":"—"}</span></div>
          <div class="kv"><span>Elapsed</span><span>${fmtDur(pg.elapsed_s)}</span></div></div>`
      :`<div class="sub">${pg.completed} / ${pg.size} games done</div>
        <div style="margin-top:11px">
          <div class="kv"><span>Est. time left</span><span>${pg.eta_s!=null?"~"+fmtDur(pg.eta_s):"—"}</span></div>
          <div class="kv"><span>Rate</span><span>${pg.rate_per_min!=null?pg.rate_per_min+" games/min":"—"}</span></div>
          <div class="kv"><span>Elapsed</span><span>${fmtDur(pg.elapsed_s)}</span></div></div>`;
    el.push(card("progress","Batch Progress",bar+rows));
  }

  if(d.current){const c2=d.current;
    el.push(card("task","Current Task",
      `<div class="kv"><span>run</span><span><code>${esc(c2.run.slice(0,12))}…</code></span></div>
       <div class="kv"><span>task</span><span>#${c2.task}</span></div>
       <div class="kv"><span>${term("games (size)","size")}</span><span>${c2.size}</span></div>
       <div class="kv"><span>${term("time control","tc")}</span><span><code>${esc(c2.tc)}</code></span></div>
       <div class="kv"><span>${term("threads × conc","conc")}</span><span>${c2.threads} × ${c2.concurrency}</span></div>
       <div class="kv"><span>type</span><span>${term(c2.kind.toUpperCase(),"sprt")} ${c2.budget.toLocaleString()}</span></div>
       <div class="kv"><span>assigned</span><span>${fmtAge(c2.age_s)}</span></div>`));
  }else{
    el.push(card("task","Current Task",`<div class="empty">no task in the recent log window</div>`));
  }

  if(gs){const t=gs.games||1;
    el.push(card("games","Game Results",
      `<div class="big">${gs.games} <span style="font-size:15px;color:var(--muted)">games · ${gs.pct}%</span></div>
       <div class="bar">
         <div class="w" style="width:${100*gs.wins/t}%">${gs.wins}</div>
         <div class="d" style="width:${100*gs.draws/t}%">${gs.draws}</div>
         <div class="l" style="width:${100*gs.losses/t}%">${gs.losses}</div></div>
       <div class="sub" style="margin:8px 0 4px">wins · draws · losses</div>
       ${d.los?`<div class="kv"><span>${term("LOS","los")}</span><span>${d.los.los}%</span></div>
         <div class="kv"><span>Draw ratio</span><span>${d.los.draw_ratio}%</span></div>`:""}
       ${d.ptnml?`<div class="kv"><span>${term("Ptnml(0-2)","ptnml")}</span><span><code>[${d.ptnml.join(", ")}]</code></span></div>`:""}`));
  }

  const ec=d.ep_counts||{},dotc={beat:'var(--blue)',update_task:'var(--amber)',request_task:'var(--green-b)',upload_pgn:'var(--violet)',failed_task:'var(--red)'};
  const pills=Object.keys(ec).sort((a,b)=>ec[b]-ec[a]).map(k=>
    `<span class="pill"><i style="background:${dotc[k]||'var(--muted)'}"></i>${k} · ${ec[k]}</span>`).join("");
  el.push(card("api","API Activity",pills||`<div class="empty">no calls yet</div>`));

  el.push(card("lat","Latency",
    spark(d.latency,"w","var(--blue)","wall (round-trip)")+
    `<div style="height:8px"></div>`+
    spark(d.latency,"s","var(--green-b)","server processing")));

  const evs=(d.events||[]).slice().reverse().map(e=>
    `<div><span class="tag ${e.ep}">${e.ep}</span><span class="tm">${new Date(e.t).toLocaleTimeString()}</span></div>`).join("");
  el.push(`<div class="card wide"><h2>Event Stream<button class="q" data-k="events">?</button></h2>
    <div class="exp" id="exp-events">${EXP.events}</div><div class="evt">${evs||'<div class="empty">no events</div>'}</div></div>`);

  if(d.pgns&&d.pgns.length){
    const rows=d.pgns.map(p=>`<div class="kv"><span><code>${esc(p.name.replace("results-","").slice(0,28))}</code></span>
      <span>${(p.size/1024).toFixed(0)} KB · ${fmtAge((Date.now()/1000)-p.mtime)}</span></div>`).join("");
    el.push(`<div class="card wide"><h2>PGN Output<button class="q" data-k="pgn">?</button></h2>
      <div class="exp" id="exp-pgn">${EXP.pgn}</div>${rows}</div>`);
  }

  const t=d.testing;
  if(t){
    const legend=t.categories.map(c=>`<div class="row"><i style="background:${c.color}"></i>${c.name}
      <span class="sz">${mb(c.size)} · ${c.count}</span></div>`).join("");
    const keepArr=t.keepnames.map(n=>"'"+n.replace(/'/g,"''")+"'").join(",");
    const cmd=`cd "${t.path}"\n$keep=@(${keepArr})\n`+
      `Get-ChildItem -File | ? { $_.Name -notin $keep -and ($_.Name -like 'nn-*.nnue' -or `+
      `$_.Name -like 'stockfish-*.exe' -or $_.Name -like 'results-*.pgn') } | Remove-Item -Confirm:$false`;
    const dl=t.deletable.map(f=>`<div><span>${esc(f.name.slice(0,54))}</span><span class="sz">${mb(f.size)}</span></div>`).join("");
    el.push(`<div class="card wide"><h2>Testing Folder — ${mb(t.total)}<button class="q" data-k="disk">?</button></h2>
      <div class="exp" id="exp-disk">${EXP.disk}</div>
      <div class="disk">${donut(t.categories,t.total)}<div class="legend">${legend}</div></div>
      <div class="cleanup">
        <div class="hd">🧹 <b>${mb(t.reclaim)}</b> reclaimable · ${t.delcount} leftover files · ${t.filecount} total
          <span style="color:var(--muted);font-weight:400">(worker re-downloads on demand)</span></div>
        <div class="cmd" data-c="${escAttr(cmd)}"><button onclick="navigator.clipboard&&navigator.clipboard.writeText(this.parentNode.dataset.c);this.textContent='copied'">copy</button>${esc(cmd)}</div>
        <div class="warn">⚠ Run only while the worker is <b>idle</b> — a running task holds its files open. Deletes nets, old engine builds & uploaded PGNs; keeps the ${t.keepnames.length} in-use files.</div>
        <details style="margin-top:10px"><summary style="cursor:pointer;color:var(--green-b);font-size:13px">Show ${t.delcount} deletable files</summary>
          <div class="dellist">${dl}</div></details>
      </div></div>`);
  }

  // preserve which #stats explanations were open across refresh (viewer is untouched)
  const open=new Set([...document.querySelectorAll("#stats .exp.open")].map(e=>e.id));
  document.getElementById("stats").innerHTML=el.join("");
  open.forEach(id=>{const e=document.getElementById(id);if(e)e.classList.add("open")});
}
// "?" explanation toggles work for both the polled cards and the persistent viewer
document.addEventListener("click",e=>{
  const b=e.target.closest(".q");if(!b)return;
  const ex=document.getElementById("exp-"+b.dataset.k);if(ex)ex.classList.toggle("open");
});
tick();setInterval(tick,2000);

/* ---- Trophy: this PC's contribution (/impact, every 10 min — same data as the TV's trophy) ---- */
const OUTCOMES=[["merged","In Stockfish","var(--green-b)"],["passed","Passed, not merged","var(--blue)"],
  ["running","Still undecided","var(--amber)"],["not merged","Not merged","#55655d"]];
const num=n=>Number(n||0).toLocaleString("en-US");
async function loadTrophy(){
  let d;try{d=await (await fetch("/impact")).json()}catch{return}
  const T=d.totals||{},all=Object.values(T);
  const tests=all.reduce((n,t)=>n+t.tests,0),games=all.reduce((n,t)=>n+t.games,0),hours=all.reduce((n,t)=>n+t.cpu_hours,0);
  if(!tests)return;
  const n=d.merged_patches||0,m=T.merged;
  const since=d.since?new Date(d.since.replace(" ","T")+"Z").toLocaleDateString("en-US",{month:"short",day:"numeric",year:"numeric"}):"";
  const headline=n
    ?`<span class="big g">${n} patch${n===1?"":"es"} in Stockfish</span><span class="sub">you helped test ${n===1?"it":"them"} with ${Math.round(m.cpu_hours)} CPU-hours</span>`
    :`<span class="big">${tests} tests helped</span><span class="sub">no merges yet</span>`;
  const tiles=OUTCOMES.filter(([k])=>T[k]).map(([k,label,color])=>`<div class="tile">
      <div class="lbl"><i style="background:${color}"></i>${label}</div>
      <b>${T[k].tests} test${T[k].tests===1?"":"s"}</b>
      <div class="sub">${num(T[k].games)} games · ${num(Math.round(T[k].cpu_hours))} CPU-h</div></div>`).join("");
  const merged=(d.tests||[]).filter(t=>t.outcome==="merged").map(t=>{
    const c=t.commit||{},link=c.sha?`https://github.com/official-stockfish/Stockfish/commit/${c.sha}`:null;
    return `<div class="mrow">🏆 <code>${esc(t.branch)}</code>
      ${link?`<a href="${escAttr(link)}" target="_blank" rel="noopener">“${esc(c.subject||"commit")}”</a>`:""}
      <span class="sub">${esc(c.date||"")}${c.sha?" · "+esc(c.sha.slice(0,7)):""} · by ${esc(t.author||"?")} · your ${num(t.games)} games, ${num(Math.round(t.cpu_hours))} CPU-h</span></div>`;
  }).join("");
  const el=document.getElementById("trophy"),wasOpen=document.getElementById("exp-trophy")?.classList.contains("open");
  el.innerHTML=`<h2>Your contribution${since?` · since ${esc(since)}`:""}<button class="q" data-k="trophy">?</button></h2>
    <div class="exp${wasOpen?" open":""}" id="exp-trophy">${EXP.trophy}</div>
    <div class="head"><span class="cup">🏆</span>${headline}</div>
    <div class="tiles">${tiles}</div>
    ${merged?`<div class="merged">${merged}</div>`:""}
    <div class="sub" style="margin-top:12px">Total: ${tests} tests · ${num(games)} games · ${num(Math.round(hours))} CPU-hours donated</div>`;
  el.style.display="block";
}
loadTrophy();setInterval(loadTrophy,10*60*1000);

/* ---- Game Viewer: built once, holds its own state, never reset by polling ---- */
const V={games:[],gi:0,ply:0,timer:null};
const $=id=>document.getElementById(id);
const GLYPH={p:"♟",n:"♞",b:"♝",r:"♜",q:"♛",k:"♚"};

function boardAt(g,ply){
  const c=new Chess(g.fen);
  for(let i=0;i<ply&&i<g.sans.length;i++) c.move(g.sans[i],{sloppy:true});
  return c;
}
function renderBoard(c){
  const b=c.board();let h="";
  for(let r=0;r<8;r++)for(let f=0;f<8;f++){
    const p=b[r][f];const dark=(r+f)%2===1;
    const g=p?`<span class="${p.color==='w'?'wp':'bp'}">${GLYPH[p.type]}</span>`:"";
    h+=`<div class="sq ${dark?'dk':'lt'}">${g}</div>`;
  }
  $("board").innerHTML=h;
}
function renderEval(g,ply){
  const W=300,H=80,cap=6,ev=g.evals.map(e=>e==null?0:Math.max(-cap,Math.min(cap,e)));
  if(!ev.length){$("evalGraph").innerHTML="";return;}
  const step=W/Math.max(ev.length-1,1),y=v=>H/2-(v/cap)*(H/2-4);
  const line=ev.map((v,i)=>`${i?'L':'M'}${(i*step).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const cx=Math.min(ply,ev.length-1)*step;
  $("evalGraph").innerHTML=
    `<rect x="0" y="0" width="${W}" height="${H/2}" fill="rgba(255,255,255,.03)"/>`+
    `<line x1="0" y1="${H/2}" x2="${W}" y2="${H/2}" stroke="#2b3a30"/>`+
    `<path d="${line}" fill="none" stroke="var(--green-b)" stroke-width="1.6"/>`+
    `<line x1="${cx.toFixed(1)}" y1="0" x2="${cx.toFixed(1)}" y2="${H}" stroke="var(--amber)" stroke-width="1"/>`;
}
function renderGame(){
  const g=V.games[V.gi];
  if(!g){$("board").innerHTML="";$("ginfo").textContent="no games in this file";$("plabel").textContent="—";return;}
  V.ply=Math.max(0,Math.min(V.ply,g.plies));
  renderBoard(boardAt(g,V.ply));
  renderEval(g,V.ply);
  const ev=V.ply>0?g.evals[Math.min(V.ply-1,g.evals.length-1)]:null;
  $("evalNow").textContent=(ev==null)?"—":(ev>0?"+":"")+ev.toFixed(2);
  $("ginfo").innerHTML=`<b>${esc(g.event||"game")}</b><br>${esc(g.white)} &nbsp;vs&nbsp; ${esc(g.black)} · <b>${esc(g.result)}</b>`+
    (g.termination?` · ${esc(g.termination)}`:"");
  $("plabel").textContent=`ply ${V.ply} / ${g.plies}  ·  game ${V.gi+1} / ${V.games.length}`;
}
function stopPlay(){if(V.timer){clearInterval(V.timer);V.timer=null;}const b=$("playBtn");if(b)b.innerHTML="&#9654; play";}
function play(){
  if(V.timer){stopPlay();return;}
  const g=V.games[V.gi];if(!g)return;
  if(V.ply>=g.plies)V.ply=0;
  $("playBtn").innerHTML="&#9208; pause";
  V.timer=setInterval(()=>{
    const gg=V.games[V.gi];
    if(!gg||V.ply>=gg.plies){stopPlay();return;}
    V.ply++;renderGame();
  },700);
}
function step(to){
  const g=V.games[V.gi];if(!g)return;
  if(to==="first")V.ply=0;else if(to==="last")V.ply=g.plies;
  else if(to==="prev")V.ply=Math.max(0,V.ply-1);
  else if(to==="next")V.ply=Math.min(g.plies,V.ply+1);
  renderGame();
}
function changeGame(delta){
  stopPlay();
  V.gi=Math.max(0,Math.min(V.games.length-1,V.gi+delta));
  V.ply=0;renderGame();
}
async function loadFileList(){
  try{
    const d=await(await fetch("/data")).json();
    const sel=$("fileSel"),cur=sel.value;
    sel.innerHTML=(d.pgns||[]).map(p=>`<option value="${escAttr(p.name)}">${esc(p.name.replace("results-","").slice(0,30))} · ${(p.size/1024).toFixed(0)}KB</option>`).join("");
    if([...sel.options].some(o=>o.value===cur))sel.value=cur;
    return sel.value;
  }catch(e){return null;}
}
async function loadGames(file){
  stopPlay();
  if(!file)return;
  try{
    const r=await(await fetch("/games?file="+encodeURIComponent(file))).json();
    V.games=r.games||[];V.gi=Math.max(0,V.games.length-1);V.ply=0;renderGame();
  }catch(e){$("ginfo").textContent="could not load games";}
}
$("viewer").addEventListener("click",e=>{
  const b=e.target.closest("button");if(!b)return;
  if(b.dataset.p==="play")play();
  else if(b.dataset.p)step(b.dataset.p);
  else if(b.dataset.g)changeGame(parseInt(b.dataset.g,10));
});
$("fileSel").addEventListener("change",e=>loadGames(e.target.value));
$("refreshFiles").addEventListener("click",loadFileList);
(async()=>{const f=await loadFileList();await loadGames(f);})();
</script></body></html>"""


# ---------------------------------------------------------------------------
# 実況（/tv 用）：開発者のメモを Fishtest から取り、対局の山場をローカルの Ollama に渡して
# サッカー実況風の台本を作る。台本は手数（ply）に紐づいた短いセリフの並び。
# ---------------------------------------------------------------------------
FISHTEST = "https://tests.stockfishchess.org"
OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
NARRATOR_MODEL = os.environ.get("NARRATOR_MODEL", "qwen3:8b")
RUN_ID = re.compile(r"^[0-9a-f]{24}$")
PGN_RUN = re.compile(r"^results-([0-9a-f]{24})-\d+\.pgn$")
RUN_TTL_S = 600
MOMENT_GAP = 12  # セリフ同士の最低間隔（手数）。読み上げが重ならないように
MAX_LINES = 8

_run_cache = {}  # run -> (取得時刻, 情報)
_run_lock = threading.Lock()
_script_cache = {}  # 対局キー -> 台本
_script_lock = threading.Lock()


def run_info(run):
    """Fishtest の公開 API から、ブランチ名・開発者のメモ・全体の戦績を取る（10分キャッシュ）。"""
    if not RUN_ID.match(run or ""):
        return None
    with _run_lock:
        hit = _run_cache.get(run)
        if hit and time.time() - hit[0] < RUN_TTL_S:
            return hit[1]
    try:
        with urllib.request.urlopen(f"{FISHTEST}/api/get_run/{run}", timeout=15) as r:
            d = json.load(r)
    except (OSError, ValueError):
        return hit[1] if hit else None
    a = d.get("args", {})
    sprt = a.get("sprt") or {}
    res = d.get("results") or {}
    info = {
        "run": run,
        "branch": a.get("new_tag"),
        "base": a.get("base_tag"),
        "author": a.get("username"),
        "note": a.get("info") or a.get("msg_new") or "",
        "commit": a.get("msg_new") or "",
        "tc": a.get("tc"),
        "llr": sprt.get("llr"),
        "llr_upper": sprt.get("upper_bound"),
        "llr_lower": sprt.get("lower_bound"),
        "wins": res.get("wins"),
        "losses": res.get("losses"),
        "draws": res.get("draws"),
        "workers": d.get("workers"),
        "finished": d.get("finished"),
        "repo": a.get("tests_repo"),
        "new_sha": a.get("resolved_new"),
        "base_sha": a.get("resolved_base"),
        "elo0": sprt.get("elo0"),
        "elo1": sprt.get("elo1"),
        "threads": int(a.get("threads") or 1),
        "is_green": bool(d.get("is_green")),
        "is_yellow": bool(d.get("is_yellow")),
        "failed": bool(d.get("failed")),
        "start_time": d.get("start_time"),
    }
    with _run_lock:
        _run_cache[run] = (time.time(), info)
    return info


PIECE_WORDS = {"K": "king", "Q": "queen", "R": "rook", "B": "bishop", "N": "knight"}
SAN_TOKEN = re.compile(r"(?<![\w-])(?:O-O(?:-O)?[+#]?|[KQRBN][a-h]?[1-8]?x?[a-h][1-8](?:=[QRBN])?[+#]?|[a-h]x[a-h][1-8](?:=[QRBN])?[+#]?|[a-h][18]=[QRBN][+#]?|[a-h][1-8][+#])(?!\w)")


def san_to_words(san):
    """読み上げ用に指し手を言葉にする（Qxd2+ -> queen takes on d2, check）。"""
    tail = ", checkmate" if san.endswith("#") else ", check" if san.endswith("+") else ""
    core = san.rstrip("+#")
    if core.startswith("O-O-O"):
        return "castles queenside" + tail
    if core.startswith("O-O"):
        return "castles kingside" + tail
    promo = ""
    if "=" in core:
        core, p = core.split("=", 1)
        promo = f", promoting to a {PIECE_WORDS.get(p[:1], 'queen')}"
    piece = PIECE_WORDS.get(core[:1], "pawn")
    square = core[-2:]
    verb = "takes on" if "x" in core else "to"
    return f"{piece} {verb} {square}{promo}{tail}"


def situation(ev):
    """パッチ側から見た評価値（ポーン単位）を言葉に。"""
    if ev is None:
        return "unclear"
    if ev >= 3:
        return "patch winning"
    if ev >= 1.2:
        return "patch clearly better"
    if ev >= 0.4:
        return "patch slightly better"
    if ev > -0.4:
        return "level"
    if ev > -1.2:
        return "master slightly better"
    if ev > -3:
        return "master clearly better"
    return "master winning"


BRIEF_MODEL = os.environ.get("BRIEF_MODEL", NARRATOR_MODEL)
BRIEF_DIR = os.path.join(HERE, "tv-cache")
DIFF_MAX = 30_000
_brief_cache = {}
_brief_lock = threading.Lock()  # 同じテストの説明を二重に作らないよう、生成は一つずつ

GLOSSARY = """Stockfish glossary (use only what is relevant):
- Search: Stockfish looks ahead through a tree of possible moves. It cannot look at everything, so it tries promising moves first, searches unpromising ones less deeply (reductions) or skips them (pruning).
- History heuristics: running statistics of which moves worked well earlier in the search; used to decide which moves to try first and how much to reduce them.
- Continuation history: statistics of good follow-up moves, keyed by a move played N plies earlier (1 = the opponent's last move, 2 = our own previous move, and so on). Each distance has its own bonus weight when the statistics are updated.
- Ply: one move by one side.
- LMR (late move reductions), null move pruning, futility pruning, singular extensions: rules deciding how deeply to search a move.
- NNUE: the neural network that evaluates positions. Tuning: adjusting numeric parameters."""


# 境界 [-1.75, 0.25] は「弱くならないこと」を確かめる非退行テスト。コードを消す単純化に限らず、
# 新しいネットの確認や速度だけの変更にも使われるので、「コードを消す」とは言わない
STAKES = {
    "simplification": "Success means this change does NOT make Stockfish weaker. It only has to hold its ground, not gain.",
    "gainer": "Success means proving this change makes Stockfish stronger.",
    "test": "The test decides whether this change is good enough to keep.",
}


def test_kind(info):
    """SPRT の境界から、何をもって成功とするかを決める（ここはモデルに任せない）。"""
    elo0, elo1 = (info or {}).get("elo0"), (info or {}).get("elo1")
    if elo0 is not None and elo1 is not None and elo0 < 0 and elo1 <= 0.5:
        return "simplification", STAKES["simplification"]
    if elo0 is not None and elo0 >= 0:
        return "gainer", STAKES["gainer"]
    return "test", STAKES["test"]


def fetch_diff(info):
    """テスト対象のブランチと master の差分を GitHub から取る（GitHub 以外・取得失敗は None）。"""
    repo, new, base = (info or {}).get("repo") or "", (info or {}).get("new_sha"), (info or {}).get("base_sha")
    if not repo.startswith("https://github.com/") or not new or not base:
        return None
    try:
        with urllib.request.urlopen(f"{repo.rstrip('/')}/compare/{base}...{new}.diff", timeout=20) as r:
            return r.read(DIFF_MAX + 1).decode("utf-8", errors="replace")
    except OSError:
        return None


def diff_stats(diff):
    files = re.findall(r"^diff --git a/(\S+)", diff, flags=re.M)
    added = sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))
    return {"files": files, "added": added, "removed": removed}


def _clean(value, limit):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def briefing(run):
    """テスト1本ぶんのスカウティングレポート。ディスクに保存し、同じテストは二度作らない。"""
    if not RUN_ID.match(run or ""):
        return None
    if run in _brief_cache:
        return _brief_cache[run]
    path = os.path.join(BRIEF_DIR, f"{run}.json")
    with _brief_lock:
        if run in _brief_cache:
            return _brief_cache[run]
        try:
            with open(path, encoding="utf-8") as f:
                brief = json.load(f)
                # 保存済みのレポートも、成功条件の文言は今の定義で上書きする（実況の台本に使われる）
                brief["stakes"] = STAKES.get(brief.get("kind"), brief.get("stakes"))
                _brief_cache[run] = brief
                return brief
        except (OSError, ValueError):
            pass
        info = run_info(run)
        if info is None:
            return None
        kind, stakes = test_kind(info)
        diff = fetch_diff(info)
        prompt = (
            f"{GLOSSARY}\n\nTest: branch \"{info.get('branch')}\" by developer {info.get('author')} vs Stockfish "
            f"{info.get('base') or 'master'}. Developer's note: \"{info.get('note')}\". This is a {'non-regression (must not get weaker)' if kind == 'simplification' else kind} test.\n"
            + (f"The code diff:\n{diff[:DIFF_MAX]}\n\n" if diff else "The code diff is not available; rely on the note.\n\n")
            + "Write a scouting report for a TV audience who know football but not chess programming. Be accurate to "
            "the diff; never invent effects, numbers or results that are not in it.\nReply as JSON: "
            "{\"headline\": \"a catchy plain-English headline, max 10 words, no jargon\", "
            "\"explain\": \"2-3 plain sentences: what this part of Stockfish does, and exactly what the patch changes\", "
            "\"analogy\": \"1 football analogy sentence\"}"
        )
        body = {"model": BRIEF_MODEL, "stream": False, "think": False, "format": "json",
                "options": {"temperature": 0.5, "num_predict": 700},
                "messages": [{"role": "user", "content": prompt}]}
        try:
            req = urllib.request.Request(f"{OLLAMA}/api/chat", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=90) as r:
                raw = json.loads(json.load(r)["message"]["content"])
        except (OSError, ValueError, KeyError):
            return None  # 失敗は保存しない（次の呼び出しでやり直す）
        brief = {
            "run": run, "branch": info.get("branch"), "author": info.get("author"), "note": info.get("note"),
            "kind": kind, "stakes": stakes,
            "headline": _clean(raw.get("headline"), 90), "explain": _clean(raw.get("explain"), 420),
            "analogy": _clean(raw.get("analogy"), 220),
            "diff": diff_stats(diff) if diff else None, "model": BRIEF_MODEL,
        }
        try:
            os.makedirs(BRIEF_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(brief, f, ensure_ascii=False, indent=1)
        except OSError:
            pass
        _brief_cache[run] = brief
        return brief


def key_moments(game, patch_white):
    """台本を付ける手：開始、評価値が大きく動いた手（間隔を空けて最大5つ）、最後の手。"""
    evals, sans = game.get("evals") or [], game.get("sans") or []
    n = len(sans)
    if n == 0:
        return []
    sign = 1 if patch_white else -1  # 評価値をパッチ側から見た値にそろえる
    swings = []
    prev = evals[0] if evals and evals[0] is not None else 0.0
    for i in range(1, min(n, len(evals))):
        cur = evals[i] if evals[i] is not None else prev
        swings.append((abs(cur - prev), i, prev, cur))
        prev = cur
    picked = [0]
    for _, i, _, _ in sorted(swings, reverse=True):
        if len(picked) >= 6:
            break
        if i < n - 1 and all(abs(i - p) >= MOMENT_GAP for p in picked + [n - 1]):
            picked.append(i)
    picked.append(n - 1)
    fen = str(game.get("fen") or "")
    white_first = len(fen.split()) < 2 or fen.split()[1] != "b"
    out = []
    for ply in sorted(set(picked)):
        ev = evals[ply] if ply < len(evals) and evals[ply] is not None else None
        before = evals[ply - 1] if 0 < ply <= len(evals) and evals[ply - 1] is not None else None
        mover_white = (ply % 2 == 0) == white_first
        out.append({
            "ply": ply,
            "move": sans[ply],
            "spoken": san_to_words(sans[ply]),
            "by": "the patch" if mover_white == patch_white else "master",
            "before": situation(None if before is None else sign * before),
            "after": situation(None if ev is None else sign * ev),
        })
    return out


NARRATOR_SYSTEM = (
    "You are a British football TV commentator, live on air. Tonight's match is a chess game between two "
    "versions of the Stockfish chess engine, and you call it exactly like a football match: kick-off, midfield "
    "battle, counter-attack, pressure on the box, the dressing room, the gaffer's tactical change. Energetic, "
    "witty, short punchy sentences, and vary your phrasing from line to line. Football metaphors map naturally: a "
    "capture is a crunching tackle, a check is a shot on target, checkmate is the winning goal, a promotion is a "
    "super-sub coming on, castling is shoring up the defence. Every line is read aloud by text-to-speech, so: no "
    "emojis, no stage directions, no markdown, never chess notation (say the moves exactly as given in words), and "
    "never read out evaluation numbers. Only state chess facts you were given; do not invent moves or scores."
)


def build_prompt(game, info, moments, patch_white, brief=None):
    branch = (info or {}).get("branch") or "the patch"
    author = (info or {}).get("author") or "a developer"
    note = (info or {}).get("note") or "an unnamed tweak"
    scouting = (
        f" Scouting report on the change: {brief['explain']} {brief['analogy']} What is at stake: {brief['stakes']}"
        if brief and brief.get("explain") else ""
    )
    lines = [
        f"Home side: \"{branch}\", a modified Stockfish built by developer {author}. "
        f"The manager's tactical change, in the developer's own words: \"{note}\".{scouting}",
        "Away side: Stockfish master, the current official version.",
        f"The patch plays {'white' if patch_white else 'black'}.",
    ]
    if info and info.get("wins") is not None:
        lines.append(
            f"Season so far across all volunteer computers: patch {info['wins']} wins, master {info['losses']} wins, "
            f"{info['draws']} draws."
        )
    if info and info.get("llr") is not None and info.get("llr_upper"):
        pct = round(100 * info["llr"] / info["llr_upper"])
        lines.append(
            f"The statistical test needs to reach 100% to promote the patch; it currently stands at {pct}%."
        )
    lines.append("The game starts from a prepared opening position, as every test game does.")
    result = game.get("result")
    if result == "1/2-1/2":
        outcome = "a draw"
    elif result in ("1-0", "0-1"):
        outcome = "the patch WINS" if (result == "1-0") == patch_white else "master WINS (the patch loses)"
    else:
        outcome = "unfinished"
    lines.append("Key moments (ply index; who moved; the move in words; the state of the game before and after):")
    for i, m in enumerate(moments):
        final = f" FINAL WHISTLE: this is the last move, the result is {outcome}; say so clearly." if i == len(moments) - 1 else ""
        lines.append(f"- ply {m['ply']}: {m['by']} plays {m['spoken']}. Before: {m['before']}. After: {m['after']}.{final}")
    lines.append(f"Final result: {outcome}. Do not mention move counts, plies, stalemate or how the game was adjudicated.")
    lines.append(
        "Write one commentary line for EACH key moment, using exactly those ply numbers. The ply 0 line is the "
        "kick-off: welcome viewers and explain the manager's tactical change in plain words, based on the scouting "
        "report, and say what counts as success. The last line reacts "
        "to the final result. Each line 12 to 30 words. Reply as JSON: {\"lines\": [{\"ply\": number, \"text\": string}]}."
    )
    return "\n".join(lines)


SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"ply": {"type": "integer"}, "text": {"type": "string"}},
                "required": ["ply", "text"],
            },
        }
    },
    "required": ["lines"],
}


def commentary(file, game):
    """対局1局ぶんの実況台本を返す。同じ対局は2回目からキャッシュ。"""
    m = PGN_RUN.match(file or "")
    sans = game.get("sans") if isinstance(game, dict) else None
    if not m or not isinstance(sans, list) or not sans:
        return {"error": "bad request"}
    key = "|".join([file, str(game.get("white")), str(game.get("fen")), str(len(sans)), str(game.get("result"))])
    with _script_lock:
        if key in _script_cache:
            return _script_cache[key]
    patch_white = str(game.get("white", "")).startswith("New")
    info = run_info(m.group(1))
    brief = briefing(m.group(1))
    moments = key_moments(game, patch_white)
    body = {
        "model": NARRATOR_MODEL,
        "stream": False,
        "think": False,
        "format": "json",
        "options": {"temperature": 0.9, "num_predict": 900},
        "messages": [
            {"role": "system", "content": NARRATOR_SYSTEM},
            {"role": "user", "content": build_prompt(game, info, moments, patch_white, brief)},
        ],
    }
    started = time.time()
    try:
        req = urllib.request.Request(
            f"{OLLAMA}/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            content = json.load(r)["message"]["content"]
        raw = json.loads(content).get("lines", [])
    except (OSError, ValueError, KeyError) as e:
        return {"error": f"ollama: {e}"}
    # モデルの返答を検証：決めた手にだけセリフを付け、長すぎる文は切る
    allowed = {mo["ply"] for mo in moments}
    lines = []
    for item in raw:
        try:
            ply = int(item["ply"])
            text = re.sub(r"\s+", " ", str(item["text"])).strip()
            text = SAN_TOKEN.sub(lambda mt: san_to_words(mt.group(0)), text)
        except (KeyError, TypeError, ValueError):
            continue
        if ply in allowed and text and ply not in {x["ply"] for x in lines}:
            lines.append({"ply": ply, "text": text[:280]})
    lines.sort(key=lambda x: x["ply"])
    # 最後のセリフで結果がはっきり言われていなければ、決まった「試合終了」の一言を足す（ここだけはモデル任せにしない）
    last_ply = len(sans) - 1
    result = game.get("result")
    if result == "1/2-1/2":
        whistle, words = "And that's full time. Honours even, it's a draw!", ("draw", "honours even", "share the points")
    elif result in ("1-0", "0-1"):
        if (result == "1-0") == patch_white:
            whistle, words = "Full time! The patch takes all three points!", ("patch wins", "patch takes", "victory for the patch", "patch has won")
        else:
            whistle, words = "Full time, and master takes the points. Back to the drawing board for the patch.", ("master wins", "master takes", "patch loses", "defeat")
    else:
        whistle, words = "", ()
    if whistle:
        final = next((x for x in lines if x["ply"] == last_ply), None)
        if final is None:
            lines.append({"ply": last_ply, "text": whistle})
        elif not any(w in final["text"].lower() for w in words):
            final["text"] = f"{final['text']} {whistle}"
    script = {"lines": lines[:MAX_LINES], "model": NARRATOR_MODEL, "seconds": round(time.time() - started, 1),
              "branch": (info or {}).get("branch")}
    with _script_lock:
        _script_cache[key] = script
    return script


# ---------------------------------------------------------------------------
# 貢献の集計：この PC が担当したテストのうち、Stockfish 本体に取り込まれたもの。
# - どのテストを担当したか：ワーカーのログ（約2週間で上書きされる）→ tv-cache/ledger.json に恒久保存
# - どれだけ担当したか：Fishtest の get_task（この PC の担当分の対局数）
# - CPU 時間：Fishtest と同じ式（推定対局時間 × 対局数 × スレッド数 ÷ 3600）
# - 取り込まれたか：Stockfish の master のコミットメッセージに、そのテストの URL が載っているか
# ---------------------------------------------------------------------------
CACHE = os.path.join(HERE, "tv-cache")
LEDGER = os.path.join(CACHE, "ledger.json")
SF_GIT = os.path.join(CACHE, "stockfish.git")
SF_REPO = "https://github.com/official-stockfish/Stockfish.git"
SF_FETCH_EVERY_S = 24 * 3600  # 1日1回。マージは数日おきにまとめて入るので十分。履歴は消えないので、間隔を空けても取りこぼさない（遅れるだけ）
RE_TASK_OLD = re.compile(r"Working on task (\d+) from \S+/tests/view/([0-9a-f]{24})")
RE_RUN_LINE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\S* : run: ([0-9a-f]{24}) task: (\d+)")
_impact_lock = threading.Lock()
_merged = {"at": 0.0, "runs": {}}


def estimate_game_duration(tc):
    """Fishtest の server/fishtest/util.py と同じ推定（1局あたりの秒数）。"""
    scale, game_moves = 2 * 0.92, 68
    chunks = tc.split("+")
    increment = float(chunks[1]) if len(chunks) == 2 else 0.0
    chunks = chunks[0].split("/")
    num_moves = int(chunks[0]) if len(chunks) == 2 else 0
    parts = chunks[-1].split(":")
    time_tc = float(parts[0]) * 60 + float(parts[1]) if len(parts) == 2 else float(parts[0])
    if num_moves > 0:
        time_tc = time_tc * (game_moves / num_moves)
    return (time_tc + increment * game_moves) * scale


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def update_ledger():
    """ログに出てきた (テスト, タスク) を台帳に足す。ログが上書きされても台帳には残る。"""
    ledger = _read_json(LEDGER, {})
    changed = False
    for name in ("api.log.previous", "api.log"):
        for line in tail_lines(os.path.join(WORKER, name), max_bytes=5_000_000):
            m = RE_RUN_LINE.match(line)
            if not m:
                continue
            ts, run, task = m.group(1), m.group(2), int(m.group(3))
            entry = ledger.setdefault(run, {"tasks": [], "first_seen": ts})
            if task not in entry["tasks"]:
                entry["tasks"].append(task)
                changed = True
    try:
        with open(TXT_LOG, encoding="utf-8", errors="replace") as f:
            for task, run in RE_TASK_OLD.findall(f.read()):
                entry = ledger.setdefault(run, {"tasks": [], "first_seen": None})
                if int(task) not in entry["tasks"]:
                    entry["tasks"].append(int(task))
                    changed = True
    except OSError:
        pass
    if changed:
        _write_json(LEDGER, ledger)
    return ledger


def my_task(run, task, username):
    """この PC が担当したタスクの記録。終わったタスクは変わらないのでディスクに保存して二度と取りに行かない。"""
    path = os.path.join(CACHE, "tasks", f"{run}-{task}.json")
    cached = _read_json(path, None)
    if cached is not None:
        return cached
    try:
        with urllib.request.urlopen(f"{FISHTEST}/api/get_task/{run}/{task}", timeout=20) as r:
            t = json.load(r)
    except (OSError, ValueError):
        return None
    st = t.get("stats") or {}
    out = {
        "mine": (t.get("worker_info") or {}).get("username") == username,
        "games": sum(int(st.get(k) or 0) for k in ("wins", "losses", "draws")),
        "active": bool(t.get("active")),
    }
    if not out["active"]:
        _write_json(path, out)
    return out


def merged_runs():
    """Stockfish master のコミットメッセージに載っているテスト ID → コミット。6時間ごとに取り直す。"""
    if time.time() - _merged["at"] < SF_FETCH_EVERY_S and _merged["runs"]:
        return _merged["runs"]
    try:
        if not os.path.isdir(SF_GIT):
            subprocess.run(["git", "clone", "--quiet", "--bare", "--filter=blob:none", SF_REPO, SF_GIT],
                           check=True, timeout=300, capture_output=True)
        else:
            subprocess.run(["git", "--git-dir", SF_GIT, "fetch", "--quiet", "origin", "+refs/heads/master:refs/heads/master"],
                           check=True, timeout=300, capture_output=True)
        out = subprocess.run(["git", "--git-dir", SF_GIT, "log", "master", "--format=%H%x1f%cs%x1f%s%x1f%B%x1e"],
                             check=True, timeout=120, capture_output=True).stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return _merged["runs"]
    runs = {}
    for record in out.split("\x1e"):
        fields = record.strip().split("\x1f")
        if len(fields) < 4:
            continue
        sha, date, subject, body = fields
        for run in re.findall(r"tests/view/([0-9a-f]{24})", body):
            runs.setdefault(run, {"sha": sha, "date": date, "subject": subject})
    _merged.update(at=time.time(), runs=runs)
    return runs


def run_status(run):
    """テストの結果。終わったテストは変わらないのでディスクに保存する。"""
    path = os.path.join(CACHE, "runs", f"{run}.json")
    cached = _read_json(path, None)
    if cached is not None:
        return cached
    info = run_info(run)
    if info is None:
        return None
    keep = {k: info.get(k) for k in ("branch", "author", "note", "tc", "threads", "finished", "is_green",
                                     "is_yellow", "failed", "start_time")}
    if keep["finished"]:
        _write_json(path, keep)
    return keep


def impact():
    """担当したテストを「取り込まれた / 合格して取り込み待ち / 不合格 / 実行中」に分け、対局数と CPU 時間を合計する。"""
    with _impact_lock:
        username = read_cfg().get("username")
        ledger = update_ledger()
        merged = merged_runs()
        tests = []
        for run, entry in ledger.items():
            status = run_status(run)
            if status is None:
                continue
            games = 0
            for task in entry["tasks"]:
                t = my_task(run, task, username)
                if t and t["mine"]:
                    games += t["games"]
            hours = games * estimate_game_duration(status["tc"]) * int(status.get("threads") or 1) / 3600
            if run in merged:
                outcome = "merged"
            elif not status["finished"]:
                outcome = "running"
            elif status["is_green"]:
                outcome = "passed"
            else:
                outcome = "not merged"
            tests.append({
                "run": run, "branch": status["branch"], "author": status["author"], "note": status["note"],
                "tc": status["tc"], "outcome": outcome, "games": games, "cpu_hours": round(hours, 2),
                "first_seen": entry.get("first_seen"), "commit": merged.get(run),
            })
        tests.sort(key=lambda x: x["first_seen"] or "", reverse=True)
        totals = {}
        for t in tests:
            agg = totals.setdefault(t["outcome"], {"tests": 0, "games": 0, "cpu_hours": 0.0})
            agg["tests"] += 1
            agg["games"] += t["games"]
            agg["cpu_hours"] = round(agg["cpu_hours"] + t["cpu_hours"], 2)
        merged_patches = {t["commit"]["sha"] for t in tests if t["commit"]}
        return {
            "username": username,
            "since": min((t["first_seen"] for t in tests if t["first_seen"]), default=None),
            "totals": totals,
            "merged_patches": len(merged_patches),
            "tests": tests,
        }


# ---- スマホのリモコン（/remote） ----
# モニターは LAN にも開くので、127.0.0.1 以外からの接続は PIN でログインしたクッキーが無いと何も見せない。
# 操作は start-fishtest.ps1 に任せる（端末の `fishtest stop` 等と同じ動きになる）
REMOTE_PIN_FILE = os.path.join(CACHE, "remote-pin.txt")
REMOTE_LOG = os.path.join(CACHE, "remote-actions.log")
PS1 = os.path.join(HERE, "start-fishtest.ps1")
LOCAL_CLIENTS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
REMOTE_COOKIE = "fishtest_remote"
REMOTE_ACTIONS = {"start": "worker", "stop": "stop", "tv-on": "tv", "tv-off": "tvoff"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# PIN を外し続けたら 10 分締め出す（6 桁を総当たりされないように）
PIN_TRIES = 5
PIN_LOCK_S = 600


def remote_pin():
    """6 桁の PIN。初回に作って tv-cache に残す（消せば次の起動で作り直し、全端末がログアウトする）。"""
    try:
        with open(REMOTE_PIN_FILE, encoding="utf-8") as f:
            pin = f.readline().strip()
        if re.fullmatch(r"\d{6}", pin):
            return pin
    except OSError:
        pass
    pin = f"{secrets.randbelow(10 ** 6):06d}"
    os.makedirs(CACHE, exist_ok=True)
    with open(REMOTE_PIN_FILE, "w", encoding="utf-8") as f:
        f.write(pin + "\n")
    return pin


REMOTE_PIN = remote_pin()
# クッキーの値は PIN から作る。モニターを再起動してもログインが続き、PIN を変えれば無効になる
REMOTE_TOKEN = hashlib.sha256(f"fishtest-remote:{REMOTE_PIN}".encode()).hexdigest()
_pin_lock = threading.Lock()
_pin_fails = {"n": 0, "until": 0.0}


def check_pin(pin):
    with _pin_lock:
        now = time.time()
        if now < _pin_fails["until"]:
            return "locked"
        if hmac.compare_digest(str(pin), REMOTE_PIN):
            _pin_fails["n"] = 0
            return "ok"
        _pin_fails["n"] += 1
        if _pin_fails["n"] >= PIN_TRIES:
            _pin_fails.update(n=0, until=now + PIN_LOCK_S)
            return "locked"
        return "wrong"


_proc_lock = threading.Lock()
_proc_cache = {"t": 0.0, "v": {"worker": False, "tv": False}}


def remote_processes(fresh=False):
    """ワーカー（python3 worker.py）と TV（専用プロファイルの Edge）が動いているか。CIM は 1 秒ほどかかるので 3 秒キャッシュ。"""
    with _proc_lock:
        if not fresh and time.time() - _proc_cache["t"] < 3:
            return _proc_cache["v"]
        cmd = ("Get-CimInstance Win32_Process -Filter \"Name LIKE 'python3%' OR Name='msedge.exe'\" | "
               "ForEach-Object { $_.Name + '|' + $_.CommandLine }")
        try:
            out = subprocess.run(["powershell.exe", "-NoProfile", "-Command", cmd], capture_output=True,
                                 text=True, timeout=20, creationflags=NO_WINDOW).stdout
        except (OSError, subprocess.SubprocessError):
            return _proc_cache["v"]
        lines = out.splitlines()
        v = {
            "worker": any(l.lower().startswith("python3") and "worker.py" in l for l in lines),
            "tv": any(l.lower().startswith("msedge") and "fishtest-tv-edge" in l for l in lines),
        }
        _proc_cache.update(t=time.time(), v=v)
        return v


def remote_status():
    d = collect()
    procs = remote_processes()
    cur = d.get("current") or {}
    info = run_info(cur.get("run")) if procs["worker"] and cur.get("run") else None
    return {
        "worker": procs["worker"],
        "stopping": procs["worker"] and os.path.exists(FISH_EXIT),
        "tv": procs["tv"],
        "concurrency": (d.get("cfg") or {}).get("concurrency"),
        "run": cur.get("run"),
        "tc": cur.get("tc"),
        "branch": (info or {}).get("branch"),
        "note": (info or {}).get("note"),
        "llr": (info or {}).get("llr"),
        "llr_upper": (info or {}).get("llr_upper"),
        "progress": d.get("progress"),
        "games": d.get("game_stats"),
        "last_age_s": d.get("last_age_s"),
    }


def remote_action(action):
    """start-fishtest.ps1 を裏で動かす。止める（stop）はワーカーが次の報告で終わるまで数分かかるので待たない。"""
    mode = REMOTE_ACTIONS.get(action)
    if mode is None:
        return False
    os.makedirs(CACHE, exist_ok=True)
    log = open(REMOTE_LOG, "a", encoding="utf-8")
    log.write(f"\n== {datetime.now().isoformat(timespec='seconds')} {action} ({mode})\n")
    log.flush()
    subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", PS1, mode],
                     cwd=HERE, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                     creationflags=NO_WINDOW)
    log.close()
    with _proc_lock:
        _proc_cache["t"] = 0.0  # 次の状態取得で取り直す
    return True


REMOTE_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0a0e0d">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Fishtest Remote</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<style>
:root{color-scheme:dark;--bg:#0a0e0d;--panel:#101613;--line:#1d2a23;--text:#e7efe9;--muted:#7d8f85;
  --green:#4cb26a;--green-b:#5fe08a;--red:#f0645b;--amber:#e2a13a;
  --font:"Segoe UI",-apple-system,BlinkMacSystemFont,Roboto,Helvetica,Arial,sans-serif}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;font-family:var(--font);background:var(--bg);color:var(--text);
  padding:max(16px,env(safe-area-inset-top)) 16px max(24px,env(safe-area-inset-bottom))}
main{max-width:460px;margin:0 auto;display:flex;flex-direction:column;gap:14px}
header{display:flex;align-items:center;gap:10px;margin:4px 0 6px}
header img{width:30px;height:30px}
header h1{font-size:20px;margin:0;font-weight:700;flex:1}
.ago{font-size:12px;color:var(--muted)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:16px}
.row{display:flex;align-items:center;gap:10px}
.label{font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);flex:1}
.dot{width:10px;height:10px;border-radius:50%;background:#3a4741}
.dot.on{background:var(--green-b);box-shadow:0 0 10px var(--green)}
.dot.wait{background:var(--amber);box-shadow:0 0 10px var(--amber)}
.state{font-size:22px;font-weight:700;margin:10px 0 2px}
.sub{font-size:14px;color:var(--muted);line-height:1.45;overflow-wrap:anywhere}
.bar{height:8px;border-radius:4px;background:#18221d;margin:12px 0 6px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--green);border-radius:4px;transition:width .4s}
.stats{display:flex;gap:16px;font-size:14px;color:var(--muted);flex-wrap:wrap}
.stats b{color:var(--text);font-variant-numeric:tabular-nums}
button{width:100%;margin-top:14px;padding:16px;border:0;border-radius:14px;font:inherit;font-size:17px;
  font-weight:700;color:#06110a;background:var(--green-b);cursor:pointer}
button.stop{background:var(--red);color:#1a0605}
button.ghost{background:#1a2520;color:var(--text)}
button:disabled{opacity:.45}
a.more{display:block;text-align:center;color:var(--green-b);text-decoration:none;font-size:15px;padding:8px}
#login{display:none}
#login input{width:100%;margin-top:12px;padding:16px;border-radius:14px;border:1px solid var(--line);
  background:#0d1310;color:var(--text);font-size:28px;letter-spacing:.4em;text-align:center}
.err{color:var(--red);font-size:14px;min-height:20px;margin-top:8px}
</style></head><body><main>
<header><img src="/favicon.svg" alt=""><h1>Fishtest Remote</h1><span class="ago" id="ago"></span></header>

<section class="card" id="login">
  <div class="label">Enter PIN</div>
  <div class="sub" style="margin-top:6px">Run <b>fishtest remote</b> on the PC to see it.</div>
  <input id="pin" inputmode="numeric" autocomplete="one-time-code" maxlength="6" placeholder="••••••">
  <button id="go">Unlock</button>
  <div class="err" id="loginErr"></div>
</section>

<div id="app" style="display:none;flex-direction:column;gap:14px">
<section class="card">
  <div class="row"><span class="label">Worker</span><span class="dot" id="wDot"></span></div>
  <div class="state" id="wState">…</div>
  <div class="sub" id="wSub"></div>
  <div id="wProg" style="display:none">
    <div class="bar"><i id="wBar" style="width:0"></i></div>
    <div class="stats" id="wStats"></div>
  </div>
  <button id="wBtn" disabled>…</button>
</section>

<section class="card">
  <div class="row"><span class="label">TV on the PC</span><span class="dot" id="tDot"></span></div>
  <div class="state" id="tState">…</div>
  <button id="tBtn" class="ghost" disabled>…</button>
</section>

<div class="err" id="appErr" style="text-align:center"></div>
<a class="more" href="/">Full monitor →</a>
</div>
</main>
<script>
const $ = id => document.getElementById(id)
let st = null, busy = false, lastOk = 0

const post = (url, body) => fetch(url, {method: "POST", headers: {"Content-Type": "application/json", "X-Fishtest": "1"},
  body: JSON.stringify(body), credentials: "same-origin"})

function showLogin(on) {
  $("login").style.display = on ? "block" : "none"
  $("app").style.display = on ? "none" : "flex"
  if (on) setTimeout(() => $("pin").focus(), 50)
}

const fmtEta = s => s == null ? "" : s >= 3600 ? `${Math.floor(s/3600)}h ${Math.round(s%3600/60)}m` : `${Math.max(1, Math.round(s/60))}m`

function render() {
  if (!st) return
  const w = $("wBtn"), t = $("tBtn")
  if (st.stopping) {
    $("wDot").className = "dot wait"; $("wState").textContent = "Stopping…"
    $("wSub").textContent = "Finishing and uploading the current games. This can take a few minutes."
    w.textContent = "Stopping…"; w.className = "stop"; w.disabled = true
  } else if (st.worker) {
    $("wDot").className = "dot on"; $("wState").textContent = st.branch ? st.branch : "Running"
    $("wSub").textContent = [st.note, [st.tc, st.concurrency && `${st.concurrency} cores`].filter(Boolean).join(" · ")].filter(Boolean).join("\n")
    w.textContent = "Stop worker"; w.className = "stop"; w.disabled = busy
  } else {
    $("wDot").className = "dot"; $("wState").textContent = "Stopped"
    $("wSub").textContent = "Not playing any games."
    w.textContent = "Start worker"; w.className = ""; w.disabled = busy
  }
  $("wSub").style.whiteSpace = "pre-line"
  const p = st.progress, g = st.games
  if (st.worker && p && p.size) {
    $("wProg").style.display = "block"
    $("wBar").style.width = Math.min(100, p.pct || 0) + "%"
    const parts = [`<span>Games <b>${p.completed}/${p.size}</b></span>`]
    if (g && g.games) parts.push(`<span>W-L-D <b>${g.wins}-${g.losses}-${g.draws}</b></span>`)
    if (p.eta_s != null) parts.push(`<span>Left <b>${fmtEta(p.eta_s)}</b></span>`)
    if (st.llr != null) parts.push(`<span>LLR <b>${st.llr.toFixed(2)}</b> / ${(st.llr_upper || 2.94).toFixed(2)}</span>`)
    $("wStats").innerHTML = parts.join("")
  } else $("wProg").style.display = "none"

  $("tDot").className = st.tv ? "dot on" : "dot"
  $("tState").textContent = st.tv ? "On" : "Off"
  t.textContent = st.tv ? "Turn TV off" : "Turn TV on"; t.disabled = busy
}

async function refresh() {
  try {
    const r = await fetch("/remote/status", {credentials: "same-origin", cache: "no-store"})
    if (r.status === 401) { showLogin(true); return }
    if (!r.ok) throw new Error(r.status)
    st = await r.json(); lastOk = Date.now(); $("appErr").textContent = ""
    showLogin(false); render()
  } catch (e) {
    $("appErr").textContent = "Can't reach the PC. Same Wi-Fi? Is the monitor running?"
  }
}

async function act(action, confirmText) {
  if (confirmText && !confirm(confirmText)) return
  busy = true; render()
  try {
    const r = await post("/remote/action", {action})
    if (r.status === 401) { showLogin(true); return }
    if (!r.ok) throw new Error(r.status)
  } catch (e) { $("appErr").textContent = "That didn't go through. Try again." }
  // 起動・停止が反映されるまで少し速めに確かめる
  for (let i = 0; i < 8; i++) { await new Promise(res => setTimeout(res, 1500)); await refresh() }
  busy = false; render()
}

$("wBtn").onclick = () => st && (st.worker
  ? act("stop", "Stop the worker? It finishes and uploads the games it's playing first.")
  : act("start"))
$("tBtn").onclick = () => st && act(st.tv ? "tv-off" : "tv-on")

async function login() {
  const pin = $("pin").value.trim()
  if (!/^\d{6}$/.test(pin)) { $("loginErr").textContent = "The PIN is 6 digits."; return }
  $("go").disabled = true
  try {
    const r = await post("/remote/login", {pin})
    const j = await r.json().catch(() => ({}))
    if (r.ok) { $("pin").value = ""; $("loginErr").textContent = ""; await refresh() }
    else $("loginErr").textContent = j.error === "locked" ? "Too many tries. Wait 10 minutes." : "Wrong PIN."
  } catch (e) { $("loginErr").textContent = "Can't reach the PC." }
  $("go").disabled = false
}
$("go").onclick = login
$("pin").addEventListener("keydown", e => { if (e.key === "Enter") login() })

setInterval(() => {
  if (lastOk) { const s = Math.round((Date.now() - lastOk) / 1000); $("ago").textContent = s < 6 ? "live" : `${s}s ago` }
}, 1000)
setInterval(() => { if (!document.hidden && !busy && $("login").style.display !== "block") refresh() }, 4000)
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh() })
refresh()
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def _send(self, body, ctype):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, status=200, headers=()):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _remote_ok(self):
        """この PC からの接続か、PIN でログイン済みのクッキーを持っているか。"""
        if self.client_address[0] in LOCAL_CLIENTS:
            return True
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie") or "")
        except Exception:
            return False
        c = cookie.get(REMOTE_COOKIE)
        return c is not None and hmac.compare_digest(c.value, REMOTE_TOKEN)

    def _host_ok(self):
        # DNS リバインディング対策：IP アドレス・localhost・Tailscale の名前で来たものだけ受け付ける
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return (host == "localhost" or host.endswith(".ts.net")
                or re.fullmatch(r"[\d.]+|[0-9a-f:]+", host) is not None)

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        if not self._host_ok():
            self.send_error(403)
            return
        public = route in FAVICONS or route in ("/favicon.ico", "/remote")
        if not public and not self._remote_ok():
            if route == "/":
                self.send_response(302)
                self.send_header("Location", "/remote")
                self.end_headers()
            else:
                self.send_error(401)
            return
        if route == "/remote":
            self._send(REMOTE_PAGE, "text/html; charset=utf-8")
        elif route == "/remote/status":
            self._json(remote_status())
        elif route in FAVICONS or route == "/favicon.ico":
            # /favicon.ico はリンクを読まない場面（ブックマーク等）向けに、モニターの SVG を返す
            self._send(FAVICONS.get(route, FAVICONS["/favicon.svg"]), "image/svg+xml")
        elif route == "/data":
            self._send(json.dumps(collect()), "application/json")
        elif route == "/games":
            name = parse_qs(parsed.query).get("file", [""])[0]
            games = load_games(name)
            if games is None:
                self.send_error(404, "unknown pgn file")
                return
            self._send(json.dumps({"file": name, "games": games}), "application/json")
        elif route == "/runinfo":
            run = parse_qs(parsed.query).get("run", [""])[0]
            info = run_info(run)
            if info is None:
                self.send_error(404, "unknown run")
                return
            self._send(json.dumps(info), "application/json")
        elif route == "/briefing":
            run = parse_qs(parsed.query).get("run", [""])[0]
            brief = briefing(run)
            if brief is None:
                self.send_error(503, "briefing unavailable (ollama or github unreachable?)")
                return
            self._send(json.dumps(brief), "application/json")
        elif route == "/impact":
            self._send(json.dumps(impact()), "application/json")
        elif route == "/tv":
            # 部屋の向こうから眺める全画面ビュー。毎回ディスクから読むので、tv.html の手直しは再読み込みだけで反映される
            try:
                with open(os.path.join(HERE, "tv.html"), "rb") as f:
                    self._send(f.read(), "text/html; charset=utf-8")
            except OSError:
                self.send_error(404, "tv.html not found next to monitor.py")
        elif route == "/vendor/chess.js":
            try:
                with open(os.path.join(VENDOR, "chess.js"), "rb") as f:
                    self._send(f.read(), "text/javascript; charset=utf-8")
            except OSError:
                self.send_error(404, "chess.js not vendored")
        else:
            self._send(PAGE, "text/html; charset=utf-8")


    def do_POST(self):
        route = urlparse(self.path).path
        if not self._host_ok():
            self.send_error(403)
            return
        if route in ("/remote/login", "/remote/action"):
            self._remote_post(route)
            return
        if not self._remote_ok():
            self.send_error(401)
            return
        # 実況台本の生成。対局データは /tv が /games から受け取ったものをそのまま送ってくる
        if route != "/commentary":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 2_000_000:
            self.send_error(413, "bad size")
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except ValueError:
            self.send_error(400, "bad json")
            return
        self._send(json.dumps(commentary(payload.get("file"), payload.get("game") or {})), "application/json")

    def _remote_post(self, route):
        # 独自ヘッダーが無い POST は断る：他のサイトのページからこの PC のブラウザ経由で押されないように
        # （独自ヘッダー付きのクロスオリジン要求はプリフライトで止まる）
        if self.headers.get("X-Fishtest") != "1":
            self.send_error(403)
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 10_000:
            self.send_error(413, "bad size")
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except ValueError:
            self.send_error(400, "bad json")
            return
        if route == "/remote/login":
            result = check_pin(payload.get("pin", ""))
            if result != "ok":
                self._json({"error": result}, 429 if result == "locked" else 403)
                return
            cookie = f"{REMOTE_COOKIE}={REMOTE_TOKEN}; Max-Age=31536000; Path=/; HttpOnly; SameSite=Strict"
            self._json({"ok": True}, headers=[("Set-Cookie", cookie)])
            return
        if not self._remote_ok():
            self.send_error(401)
            return
        if not remote_action(payload.get("action")):
            self.send_error(400, "unknown action")
            return
        self._json({"ok": True})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--worker", default=WORKER, help="Fishtest worker folder (default: $FISHTEST_WORKER or C:/fishtest/worker)")
    args = ap.parse_args()
    _worker_paths(args.worker)
    if not os.path.exists(os.path.join(WORKER, "worker.py")):
        print(f"warning: no worker.py in {WORKER} (set FISHTEST_WORKER or --worker)")
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"Fishtest worker monitor -> {url}  (Ctrl+C to stop)")
    print(f"Across-the-room view    -> {url}/tv")
    if args.host not in ("127.0.0.1", "localhost"):
        print(f"Phone remote            -> http://<this PC's IP>:{args.port}/remote  (PIN {REMOTE_PIN}; `fishtest remote` shows the URL)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
