"""Pure-ASGI hardening middleware: request id, body cap (enforced while reading), content type, JSON depth and number
sanity, tiered rate limiting, security headers, metadata-only access log, generic 500s. Nothing user-supplied is logged."""
import json
import logging
import time
import traceback
import uuid

from app.hardening.config import HConfig
from app.hardening.errors import error_body
from app.hardening.limiter import SlidingWindowLimiter, client_ip

log = logging.getLogger("app.access")

CHEAP = {"/health", "/api/health", "/data/summary", "/data/places", "/data/cities", "/check-itinerary", "/version"}
EXPENSIVE = {"/copilot", "/parse", "/parse-replan", "/parse-repair", "/parse-profile"}
STANDARD = {"/optimise", "/compare-hotels", "/replan", "/evaluate-itinerary", "/explore", "/apply-repair"}
KNOWN = CHEAP | EXPENSIVE | STANDARD

SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-store"),
    (b"x-frame-options", b"DENY"),
]


class _BadConstant(ValueError):
    pass


def _reject_constant(name):
    raise _BadConstant(name)


def base_tier(path: str) -> str:
    if path in CHEAP:
        return "cheap"
    if path in EXPENSIVE:
        return "expensive"
    return "standard"


def json_depth_exceeds(body: bytes, limit: int) -> bool:
    depth, in_str, esc = 0, False, False
    for b in body:
        if in_str:
            if esc:
                esc = False
            elif b == 0x5C:
                esc = True
            elif b == 0x22:
                in_str = False
        elif b == 0x22:
            in_str = True
        elif b in (0x7B, 0x5B):
            depth += 1
            if depth > limit:
                return True
        elif b in (0x7D, 0x5D):
            depth -= 1
    return False


class HardeningMiddleware:
    def __init__(self, app, cfg: HConfig, limiter: SlidingWindowLimiter):
        self.app, self.cfg, self.limiter = app, cfg, limiter

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from app import runtime

        t_perf = time.perf_counter()
        rid = uuid.uuid4().hex[:16]  # never trust a client-supplied id (log injection)
        state = scope.setdefault("state", {})
        state["request_id"], state["t0"] = rid, runtime.now()
        path, method = scope["path"], scope["method"]
        ip = client_ip(scope, self.cfg.trusted_proxy_hops)
        meta = {"status": 0, "tier": base_tier(path), "limited": False, "started": False}

        async def send_wrap(msg):
            if msg["type"] == "http.response.start":
                meta["status"], meta["started"] = msg["status"], True
                headers = [h for h in msg.get("headers", []) if h[0].lower() not in (b"x-request-id",)]
                have = {h[0].lower() for h in headers}
                headers += [h for h in SECURITY_HEADERS if h[0] not in have]
                headers.append((b"x-request-id", rid.encode()))
                msg = {**msg, "headers": headers}
            await send(msg)

        async def reply(status, code, message, details=None, extra_headers=None):
            body = json.dumps(error_body(code, message, details, rid)).encode()
            hdrs = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
            hdrs += [(k.encode(), v.encode()) for k, v in (extra_headers or {}).items()]
            await send_wrap({"type": "http.response.start", "status": status, "headers": hdrs})
            await send_wrap({"type": "http.response.body", "body": body})

        async def limited_or(status, code, message, details=None, tier=None):
            """Early rejections still consume a token, so floods of bad requests are rate limited too."""
            ok, retry = self.limiter.check(ip, tier or meta["tier"])
            if not ok:
                meta["limited"] = True
                return await reply(429, "rate_limited", "Too many requests. Please slow down.", {"retry_after_seconds": retry},
                                   {"retry-after": str(retry)})
            await reply(status, code, message, details)

        try:
            body = b""
            if method in ("POST", "PUT", "PATCH"):
                ctype = next((v.decode("latin-1").lower() for k, v in scope["headers"] if k == b"content-type"), "")
                if not ctype.startswith("application/json"):
                    return await limited_or(415, "unsupported_media_type", "Content-Type must be application/json.")
                clen = next((v for k, v in scope["headers"] if k == b"content-length"), None)
                cap = self.cfg.body_max_bytes
                too_big = lambda: limited_or(413, "payload_too_large", f"Request body exceeds the {cap} byte limit.", {"limit_bytes": cap})
                try:
                    if clen is not None and int(clen) > cap:
                        return await too_big()
                except ValueError:
                    return await limited_or(400, "bad_request", "Invalid Content-Length header.")
                chunks, total = [], 0
                while True:  # enforced while reading: a client can lie about Content-Length
                    msg = await receive()
                    if msg["type"] == "http.disconnect":
                        return
                    part = msg.get("body", b"")
                    total += len(part)
                    if total > cap:
                        return await too_big()
                    chunks.append(part)
                    if not msg.get("more_body"):
                        break
                body = b"".join(chunks)
                if json_depth_exceeds(body, self.cfg.max_json_depth):
                    return await limited_or(422, "json_too_deep", f"JSON nesting is deeper than {self.cfg.max_json_depth} levels.")
                parsed = None
                if body:
                    try:
                        parsed = json.loads(body, parse_constant=_reject_constant)
                    except _BadConstant:
                        return await limited_or(422, "invalid_number", "NaN and Infinity are not allowed.")
                    except (ValueError, RecursionError):
                        return await limited_or(422, "invalid_json", "Request body is not valid JSON.")
            ok, retry = self.limiter.check(ip, meta["tier"])
            if not ok:
                meta["limited"] = True
                return await reply(429, "rate_limited", "Too many requests. Please slow down.", {"retry_after_seconds": retry},
                                   {"retry-after": str(retry)})
            if method in ("POST", "PUT", "PATCH"):
                sent = False

                async def replay():
                    nonlocal sent
                    if not sent:
                        sent = True
                        return {"type": "http.request", "body": body, "more_body": False}
                    return await receive()

                await self.app(scope, replay, send_wrap)
            else:
                await self.app(scope, receive, send_wrap)
        except Exception as exc:  # generic 500: no stack trace, paths or values to the client
            frames = [f"{f.filename}:{f.lineno} in {f.name}" for f in traceback.extract_tb(exc.__traceback__)][-8:]
            log.error("unhandled_exception", extra={"request_id": rid, "exc_type": type(exc).__name__, "frames": frames})
            if not meta["started"]:
                await reply(500, "internal_error", "Something went wrong on our side. Please try again; quote the request id if it persists.")
        finally:
            st = state
            stats = st.get("llm_stats")
            status = meta["status"] or 500
            outcome = ("rate_limited" if meta["limited"] else "server_error" if status >= 500 else "client_error" if status >= 400 else "ok")
            log.info("request", extra={
                "request_id": rid, "method": method, "route": path if path in KNOWN else "unmatched", "status": status,
                "latency_ms": round((time.perf_counter() - t_perf) * 1000, 1), "tier": meta["tier"], "rate_limited": meta["limited"],
                "llm_calls": getattr(stats, "calls", 0), "llm_input_tokens": getattr(stats, "input_tokens", 0),
                "llm_output_tokens": getattr(stats, "output_tokens", 0), "outcome": outcome})
