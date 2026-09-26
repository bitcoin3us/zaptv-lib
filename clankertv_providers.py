# SPDX-License-Identifier: MIT
# Copyright (c) 2026 ZapTV.org
# Part of zaptv-lib (https://github.com/bitcoin3us/zaptv-lib): MIT-licensed
# modules shared by the ZapTV family of MicroPythonOS apps.
# Originally written for ClankerTV.

"""Usage sources polled by the badge over Wi-Fi.

Each source is a small class with an async `fetch()` that returns a list
of records (see clankertv_core). To add a provider that the badge polls
directly, subclass `Source`, implement `fetch`, and add it to
`SOURCE_TYPES` with the settings keys it needs; `build_sources` picks it
up automatically. Providers that need desktop credentials (or an admin
key you would rather not put on the badge) belong in the bridge instead.
"""

import json
import logging

import clankertv_core as core

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 20
USER_AGENT = "ClankerTV/0.1 (MicroPythonOS)"

CLAUDE_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_PROBE_MODEL = "claude-haiku-4-5-20251001"
# What Clawdmeter's daemon sends; known to be accepted for OAuth tokens.
CLAUDE_USER_AGENT = "claude-code/2.1.5"
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"
XAI_MANAGEMENT_URL = "https://management-api.x.ai"


async def http_request(method, url, headers=None, body=None, timeout=HTTP_TIMEOUT,
                       user_agent=USER_AGENT):
    """(status, lower-cased headers, body bytes). Raises on network errors.

    Uses aiohttp directly rather than DownloadManager because the Claude
    probe needs the response headers, which DownloadManager discards.
    The connect and header phases are bounded by `timeout`; the whole
    exchange by twice that, so a stalled body cannot hang the poll.
    """
    import asyncio
    return await asyncio.wait_for(
        _http_request(method, url, headers, body, timeout, user_agent), timeout * 2)


async def _http_request(method, url, headers, body, timeout, user_agent):
    import aiohttp

    sslctx = None
    if url.lower().startswith("https"):
        import ssl
        sslctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        sslctx.verify_mode = ssl.CERT_OPTIONAL
    # The session's base headers carry the User-Agent: aiohttp merges base
    # and per-request headers with dict(**a, **b), which raises on a key
    # present in both, so it cannot be overridden per request.
    session = aiohttp.ClientSession(headers={"User-Agent": user_agent})
    ctx = session.request(method, url, data=body, headers=headers or {}, ssl=sslctx,
                          timeout=timeout)
    async with ctx as resp:
        # A chunked response hands out one chunk per read(); a plain one
        # reads to EOF (the client always sends Connection: close).
        parts = []
        while True:
            part = await resp.read()
            if not part:
                break
            parts.append(part)
            if not hasattr(resp, "chunk_size"):
                break
        hdrs = resp.headers if isinstance(resp.headers, dict) else {}
        return resp.status, core.lower_keys(hdrs), b"".join(parts)


def _loads(data):
    if isinstance(data, (bytes, bytearray)):
        data = data.decode()
    return json.loads(data)


def describe_error(e):
    """'Timed out' for the empty-message exceptions MicroPython raises for
    connect/read timeouts, otherwise the exception's own text."""
    text = str(e)
    if text:
        return text
    name = type(e).__name__
    if "Timeout" in name:
        return "Timed out (is the host reachable?)"
    return name


def _http_error(status, body):
    detail = ""
    try:
        err = _loads(body).get("error")
        if isinstance(err, dict):
            detail = err.get("message") or err.get("type") or ""
        elif isinstance(err, str):
            detail = err
    except Exception:
        pass
    text = "HTTP %d" % status
    if status in (401, 403):
        text += " (key rejected)"
    if detail:
        text += ": " + str(detail)[:60]
    return text


class Source:
    """One place usage comes from. Subclasses set `id`/`name` and `fetch`."""

    id = "source"
    name = "Source"

    async def fetch(self):
        raise NotImplementedError


class BridgeSource(Source):
    """The ClankerTV bridge on your computer, or any server that speaks the
    same JSON (see bridge/README.md). One bridge can report many providers."""

    id = "bridge"
    name = "Bridge"

    def __init__(self, url, secret=None):
        self.url = url.strip()
        self.secret = (secret or "").strip()

    async def fetch(self):
        headers = {"Accept": "application/json"}
        if self.secret:
            headers["Authorization"] = "Bearer " + self.secret
        status, _, body = await http_request("GET", self.url, headers)
        if status != 200:
            return [core.error_record(self.id, self.name, _http_error(status, body))]
        records = core.parse_bridge_payload(_loads(body))
        if not records:
            return [core.error_record(self.id, self.name, "Bridge reported no providers")]
        return records


class ClaudeDirectSource(Source):
    """Claude subscription usage straight from api.anthropic.com.

    Sends a 1-token Haiku request with a long-lived OAuth token (from
    `claude setup-token`) and reads the unified rate-limit headers, the same
    probe Clawdmeter's daemon makes. Each poll costs one output token.
    """

    id = "claude"
    name = "Claude"

    def __init__(self, token):
        self.token = token.strip()

    async def fetch(self):
        if self.token.startswith("sk-ant-api"):
            # Console API keys are billed per token and never carry the
            # subscription utilisation headers.
            return [core.error_record(
                self.id, self.name,
                "That is an API key; subscription usage needs a `claude setup-token` token",
                kind="claude")]
        headers = {
            "Authorization": "Bearer " + self.token,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json",
        }
        body = json.dumps({
            "model": CLAUDE_PROBE_MODEL,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
        })
        status, hdrs, data = await http_request("POST", CLAUDE_MESSAGES_URL, headers, body,
                                                user_agent=CLAUDE_USER_AGENT)
        # A 429 still carries the utilisation headers: that is exactly the
        # moment the meter matters most.
        if status != 200 and status != 429:
            return [core.error_record(self.id, self.name, _http_error(status, data),
                                      kind="claude")]
        # Reset times are Unix epochs; measure them against the reply's own
        # Date header rather than the badge clock, which may be unset. With
        # no Date header the countdowns are simply omitted.
        now = core.parse_http_date(hdrs.get("date"))
        return [core.claude_from_headers(hdrs, now, self.id, self.name)]


class OpenRouterSource(Source):
    """OpenRouter credit usage for one API key."""

    id = "openrouter"
    name = "OpenRouter"

    def __init__(self, key):
        self.key = key.strip()

    async def fetch(self):
        headers = {"Authorization": "Bearer " + self.key}
        status, _, body = await http_request("GET", OPENROUTER_KEY_URL, headers)
        if status != 200:
            return [core.error_record(self.id, self.name, _http_error(status, body),
                                      kind="openrouter")]
        return [core.openrouter_from_key(_loads(body), self.id, self.name)]


class DeepSeekSource(Source):
    """DeepSeek remaining balance for one API key."""

    id = "deepseek"
    name = "DeepSeek"

    def __init__(self, key):
        self.key = key.strip()

    async def fetch(self):
        headers = {"Authorization": "Bearer " + self.key, "Accept": "application/json"}
        status, _, body = await http_request("GET", DEEPSEEK_BALANCE_URL, headers)
        if status != 200:
            return [core.error_record(self.id, self.name, _http_error(status, body),
                                      kind="deepseek")]
        return [core.deepseek_from_balance(_loads(body), self.id, self.name)]


class XAISource(Source):
    """xAI (Grok) spend this billing cycle, soft limit and prepaid balance,
    from the Management API. Needs a *management* key and the team id."""

    id = "xai"
    name = "xAI"

    def __init__(self, key, team):
        self.key = key.strip()
        self.team = team.strip()

    async def _get(self, path):
        headers = {"Authorization": "Bearer " + self.key, "Accept": "application/json"}
        status, _, body = await http_request("GET", XAI_MANAGEMENT_URL + path, headers)
        if status != 200:
            raise RuntimeError(_http_error(status, body))
        return _loads(body)

    async def fetch(self):
        if not self.team:
            return [core.error_record(self.id, self.name, "xAI team ID missing", kind="xai")]
        base = "/v1/billing/teams/" + self.team
        try:
            preview = await self._get(base + "/postpaid/invoice/preview")
        except Exception as e:
            return [core.error_record(self.id, self.name, str(e), kind="xai")]
        limits = balance = None
        try:
            limits = await self._get(base + "/postpaid/spending-limits")
            balance = await self._get(base + "/prepaid/balance")
        except Exception as e:
            logger.warning("xAI extras failed: %s", e)
        import time
        return [core.xai_from_billing(preview, limits, balance, time.time(), self.id, self.name)]


# settings key(s) -> source factory. The first key must be non-empty for the
# source to be enabled; the rest are passed as extra arguments.
SOURCE_TYPES = (
    (("bridge_url", "bridge_secret"), BridgeSource),
    (("claude_token",), ClaudeDirectSource),
    (("openrouter_key",), OpenRouterSource),
    (("deepseek_key",), DeepSeekSource),
    (("xai_key", "xai_team"), XAISource),
)


def build_sources(prefs):
    sources = []
    for keys, factory in SOURCE_TYPES:
        values = [core.normalize_secret(prefs.get_string(k, "")) for k in keys]
        if values[0]:
            sources.append(factory(*values))
    return sources


async def fetch_all(sources):
    """Poll every source in turn. Returns [(source_id, records), ...] for
    core.merge_records. A failing source yields an error record, never an
    exception, so one dead key cannot blank the whole dashboard."""
    results = []
    for source in sources:
        try:
            records = await source.fetch()
        except Exception as e:
            logger.warning("%s fetch failed: %s", source.name, e)
            records = [core.error_record(source.id, source.name,
                                         "Network error: " + describe_error(e))]
        results.append((source.id, records))
    return results


def offline_results(sources, reason="Not connected to Wi-Fi"):
    """What fetch_all would return if every source failed with `reason`."""
    return [(s.id, [core.error_record(s.id, s.name, reason)]) for s in sources]
