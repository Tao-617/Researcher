"""researcher 采集模块 —— 轻后端（http.server，零额外框架依赖）。

模式沿用 search_eval/server.py：POST 触发 → 后台线程跑管线 → 内存记状态 → 前端轮询。
因 researcher 自依赖，可直接 import run_pipeline，不必 subprocess。

启动：
  PYTHONIOENCODING=utf-8 python scratch/researcher/server.py   # 默认 :8780
浏览器打开 http://127.0.0.1:8780
"""

import asyncio
import io
import json
import logging
import sys
import threading
import uuid
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from pipeline.run import run_pipeline, load_config
import platforms._backends  # noqa: F401  容错加载所有可用平台后端（含 MediaCrawler）
from platforms.registry import all_platforms

PORT = 8780
RUNS_DIR = _HERE / "runs"
RUNS_DIR.mkdir(exist_ok=True)

# run_id -> {status, query, error, log:[lines], result}
_JOBS = {}


class _LogCapture(io.StringIO):
    """把管线 print 落到 job 的 log 列表，前端轮询展示。"""
    def __init__(self, job):
        super().__init__()
        self.job = job

    def write(self, s):
        if s.strip():
            self.job["log"].append(s.rstrip("\n"))
        return len(s)


def _run_job(run_id, query, requirement, config, platforms_):
    job = _JOBS[run_id]
    job["status"] = "running"
    cap = _LogCapture(job)
    try:
        with redirect_stdout(cap):
            result = asyncio.run(run_pipeline(
                query=query, requirement=requirement, config=config,
                platforms=platforms_, output_dir=str(RUNS_DIR / run_id),
            ))
        job["result"] = result
        job["status"] = "success"
    except Exception as e:
        import traceback
        job["error"] = f"{type(e).__name__}: {e}"
        job["log"].append("❌ " + job["error"])
        job["log"].append(traceback.format_exc())
        job["status"] = "failed"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静默默认访问日志
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json")

    # ── GET ──
    def do_GET(self):
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)

        if path in ("/", "/index.html"):
            html = (_HERE / "web" / "index.html").read_text(encoding="utf-8")
            return self._send(200, html, "text/html")

        if path == "/api/config":
            return self._json(load_config(None))

        if path == "/api/platforms":
            return self._json([{"id": p.id, "name": p.name} for p in all_platforms()])

        if path == "/api/runs":
            runs = []
            for d in sorted(RUNS_DIR.glob("*/result.json")):
                try:
                    r = json.loads(d.read_text(encoding="utf-8"))
                    runs.append({"run_id": d.parent.name, "query": r.get("query"),
                                 "stats": r.get("stats"), "platforms": r.get("platforms")})
                except Exception:
                    pass
            return self._json(runs)

        if path == "/api/status":
            rid = (qs.get("run_id") or [""])[0]
            job = _JOBS.get(rid)
            if not job:
                return self._json({"status": "unknown"}, 404)
            return self._json({"status": job["status"], "error": job["error"],
                               "log_len": len(job["log"])})

        if path == "/api/log":
            rid = (qs.get("run_id") or [""])[0]
            job = _JOBS.get(rid)
            if not job:
                return self._json({"log": ""}, 404)
            return self._json({"log": "\n".join(job["log"]), "status": job["status"]})

        if path == "/api/result":
            rid = (qs.get("run_id") or [""])[0]
            job = _JOBS.get(rid)
            if job and job.get("result"):
                return self._json(job["result"])
            f = RUNS_DIR / rid / "result.json"
            if f.exists():
                return self._json(json.loads(f.read_text(encoding="utf-8")))
            return self._json({"error": "no result"}, 404)

        return self._json({"error": "not found"}, 404)

    # ── POST ──
    def do_POST(self):
        if urlparse(self.path).path != "/api/run":
            return self._json({"error": "not found"}, 404)

        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._json({"error": "bad json"}, 400)

        query = (payload.get("query") or "").strip()
        if not query:
            return self._json({"error": "query 不能为空"}, 400)

        config = load_config(None)
        # 前端可覆盖的字段
        for k in ("filters", "expand_query", "eval_model", "max_count_per_query"):
            if k in payload:
                config[k] = payload[k]
        if any(k in payload for k in ("weights", "top_n", "rerank", "rerank_pool")):
            rank = config.setdefault("rank", {})
            for k in ("weights", "top_n", "rerank", "rerank_pool"):
                if k in payload:
                    rank[k] = payload[k]

        platforms_ = payload.get("platforms") or config.get("platforms") or ["xhs"]
        run_id = uuid.uuid4().hex[:10]
        _JOBS[run_id] = {"status": "queued", "query": query, "error": None,
                         "log": [], "result": None}
        threading.Thread(
            target=_run_job,
            args=(run_id, query, payload.get("requirement", ""), config, platforms_),
            daemon=True,
        ).start()
        return self._json({"run_id": run_id})


def main():
    logging.basicConfig(level=logging.WARNING)
    n_platforms = len(all_platforms())
    print(f"researcher server: http://127.0.0.1:{PORT}  ({n_platforms} 平台已注册)")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()


if __name__ == "__main__":
    main()
