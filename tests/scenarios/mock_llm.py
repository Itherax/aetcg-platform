"""
Controllable OpenAI-compatible mock LLM used by the scenario suite.

The server answers ``POST /v1/chat/completions``.  Its behaviour is chosen by
``MockLLM.mode`` so a test can make the "model" fail, hang, or reply in a
slightly wrong shape without touching the application code.

Modes (triage agent):
    valid:<n>     well-formed JSON with risk_score n
    fenced:<n>    the same JSON wrapped in a ```json code fence
    noscore       JSON without the risk_score key
    strscore:<n>  risk_score sent as the string "n"
    fail          HTTP 500
    timeout       sleep longer than the client timeout
Modes (critic agent): the critic answers audit_passed = (score < 7) unless the
mode is ``critic_disagree``, in which case it returns audit_passed = False.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


class MockLLM:
    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self.mode = "valid:3"
        self.requests = []  # every prompt the mock received, in order
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence
                pass

            def do_POST(self):
                length = int(self.headers.get("content-length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                system_msg = body["messages"][0]["content"]
                user_msg = body["messages"][1]["content"]
                is_critic = "risk auditor" in system_msg
                outer.requests.append({"agent": "critic" if is_critic else "triage", "user": user_msg})
                mode = outer.mode
                if mode == "fail":
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b"boom")
                    return
                if mode == "timeout":
                    time.sleep(10)
                kind, _, arg = mode.partition(":")
                if is_critic:
                    score = json.loads(user_msg)["risk_score"]
                    try:
                        score_num = int(score)
                    except (TypeError, ValueError):
                        score_num = 5
                    if mode == "critic_disagree":
                        content = json.dumps({"audit_passed": False,
                                              "feedback": "DISAGREE: triage under-scored this; it should not pass."})
                    else:
                        content = json.dumps({"audit_passed": score_num < 7,
                                              "feedback": "Audit passed; routine." if score_num < 7 else "Audit failed; high risk."})
                else:
                    n = int(arg) if arg else 3
                    obj = {"risk_score": n, "category": "ACCESS", "verdict": f"mock verdict score {n}"}
                    if kind == "fenced":
                        content = "```json\n" + json.dumps(obj) + "\n```"
                    elif kind == "noscore":
                        content = json.dumps({"risk": n, "category": "ACCESS", "verdict": "wrong shape"})
                    elif kind == "strscore":
                        content = json.dumps({"risk_score": str(n), "category": "ACCESS", "verdict": "string score"})
                    else:
                        content = json.dumps(obj)
                resp = {"id": "mock", "object": "chat.completion", "created": 0, "model": body.get("model"),
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                     "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
                out = json.dumps(resp).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.server = HTTPServer((host, port), Handler)
        self.port = self.server.server_address[1]
        self.base_url = f"http://{host}:{self.port}/v1"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def triage_prompts(self):
        return [r["user"] for r in self.requests if r["agent"] == "triage"]
