"""Minimal MCP client over streamable-HTTP for the naukri server."""
import json, time, urllib.request, urllib.error

URL = "http://127.0.0.1:8321/mcp"
HDR_BASE = {"Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"}


class MCP:
    def __init__(self, url=URL, timeout=180):
        self.url = url
        self.timeout = timeout
        self.sid = None
        self._id = 0
        self.initialize()

    def _next(self):
        self._id += 1
        return self._id

    def _post(self, payload, notify=False):
        hdr = dict(HDR_BASE)
        if self.sid:
            hdr["mcp-session-id"] = self.sid
        body = json.dumps(payload).encode()
        req = urllib.request.Request(self.url, data=body, headers=hdr, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            if self.sid is None:
                self.sid = r.headers.get("mcp-session-id")
            raw = r.read().decode("utf-8", "replace")
        if notify:
            return None
        # Parse SSE. Tools that take a Context emit `notifications/message`
        # progress frames BEFORE the result, so take the frame whose id matches
        # the request -- never simply the first data: line.
        want = payload.get("id")
        fallback = None
        for line in raw.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                frame = json.loads(line[5:].strip())
            except Exception:
                continue
            if isinstance(frame, dict):
                if frame.get("id") == want:
                    return frame
                if "method" in frame:      # a notification, not our answer
                    continue
                if fallback is None:
                    fallback = frame
        if fallback is not None:
            return fallback
        if raw.strip():
            return json.loads(raw)
        return None

    def initialize(self):
        self._post({"jsonrpc": "2.0", "id": self._next(), "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                               "clientInfo": {"name": "e2e-sweep", "version": "1"}}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, notify=True)

    def list_tools(self):
        out, cursor = [], None
        while True:
            params = {"cursor": cursor} if cursor else {}
            r = self._post({"jsonrpc": "2.0", "id": self._next(),
                            "method": "tools/list", "params": params})
            res = r.get("result", {})
            out.extend(res.get("tools", []))
            cursor = res.get("nextCursor")
            if not cursor:
                return out

    def call(self, name, args=None):
        t0 = time.time()
        try:
            r = self._post({"jsonrpc": "2.0", "id": self._next(), "method": "tools/call",
                            "params": {"name": name, "arguments": args or {}}})
        except urllib.error.HTTPError as e:
            return {"transport_error": f"HTTP {e.code}: {e.read()[:400].decode('utf-8','replace')}",
                    "elapsed": round(time.time() - t0, 2)}
        except Exception as e:
            return {"transport_error": f"{type(e).__name__}: {e}",
                    "elapsed": round(time.time() - t0, 2)}
        el = round(time.time() - t0, 2)
        if r is None:
            return {"transport_error": "empty response", "elapsed": el}
        if "error" in r:
            return {"rpc_error": r["error"], "elapsed": el}
        res = r.get("result", {})
        texts = [c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"]
        blob = "\n".join(texts)
        parsed = None
        try:
            parsed = json.loads(blob)
        except Exception:
            pass
        return {"isError": res.get("isError", False), "text": blob,
                "parsed": parsed, "structured": res.get("structuredContent"),
                "elapsed": el}
