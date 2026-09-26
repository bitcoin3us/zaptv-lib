# SPDX-License-Identifier: MIT
# Copyright (c) 2026 ZapTV.org
# Part of zaptv-lib (https://github.com/bitcoin3us/zaptv-lib): MIT-licensed
# modules shared by the ZapTV family of MicroPythonOS apps.
# Originally written for ClankerTV.

"""Pure logic shared by the ClankerTV app and the desktop bridge.

No lvgl, no networking, no MicroPython-only modules: this file runs
unchanged on the badge and under CPython (the bridge imports it, and the
unit tests exercise it on the desktop).

Every provider, wherever it is polled, is normalised to a *record*:

    {
      "id": "claude",            # unique per source
      "name": "Claude",          # shown in the header
      "kind": "claude",          # picks the accent colour
      "ok": True,                # False: show `error` instead of meters
      "plan": "Max",             # optional subtitle
      "status": "allowed",       # optional provider status string
      "error": None,
      "meters": [ <meter>, ... ],
    }

and each meter is

    {
      "label": "Session",
      "pct": 45.0,               # 0..100, or None when there is no limit
      "reset_in": 7200,          # seconds until the window resets, or None
      "window": 18000,           # window length in seconds, or None
      "used": 12.3,              # optional absolute amount (spend meters)
      "limit": 50.0,             # optional absolute limit
      "unit": "$",               # optional unit for used/limit
    }

`reset_in` is relative to the moment the record was fetched, so the badge
never needs a correct wall clock: it counts down from its own fetch time.
"""

SCHEMA_VERSION = 1

WINDOW_5H = 5 * 3600
WINDOW_7D = 7 * 86400

# Bar colour thresholds (same split as Clawdmeter: green < 50 <= amber < 80 <= red).
LEVEL_AMBER_PCT = 50
LEVEL_RED_PCT = 80

# Within this many percentage points of the linear pace counts as "on pace".
PACE_TOLERANCE_PCT = 5


# ---------------------------------------------------------------------------
# Records and meters
# ---------------------------------------------------------------------------

def make_meter(label, pct=None, reset_in=None, window=None, used=None,
               limit=None, unit=None):
    if pct is not None:
        pct = max(0.0, min(100.0, float(pct)))
    elif used is not None and limit:
        pct = max(0.0, min(100.0, 100.0 * float(used) / float(limit)))
    if reset_in is not None:
        reset_in = max(0, int(reset_in))
    return {
        "label": label,
        "pct": pct,
        "reset_in": reset_in,
        "window": window,
        "used": used,
        "limit": limit,
        "unit": unit,
    }


def make_record(pid, name, meters=None, kind=None, ok=True, plan=None,
                status=None, error=None):
    return {
        "id": pid,
        "name": name,
        "kind": kind or pid,
        "ok": bool(ok),
        "plan": plan,
        "status": status,
        "error": error,
        "meters": meters or [],
    }


def error_record(pid, name, error, kind=None):
    return make_record(pid, name, kind=kind, ok=False, error=str(error))


def _num(value, default=None):
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_meter(obj):
    """Validate a meter that arrived as JSON (bridge or custom server)."""
    if not isinstance(obj, dict):
        return None
    label = obj.get("label")
    if not isinstance(label, str) or not label:
        return None
    window = _num(obj.get("window"))
    reset_in = _num(obj.get("reset_in"))
    unit = obj.get("unit")
    return make_meter(
        label[:24],
        pct=_num(obj.get("pct")),
        reset_in=reset_in,
        window=int(window) if window else None,
        used=_num(obj.get("used")),
        limit=_num(obj.get("limit")),
        unit=unit[:4] if isinstance(unit, str) else None,
    )


def normalize_record(obj, fallback_id="custom"):
    """Validate a record that arrived as JSON. Returns None if unusable."""
    if not isinstance(obj, dict):
        return None
    pid = obj.get("id") or fallback_id
    name = obj.get("name") or pid
    if not isinstance(pid, str) or not isinstance(name, str):
        return None
    ok = obj.get("ok", True) is not False
    meters = []
    for m in obj.get("meters") or ():
        meter = normalize_meter(m)
        if meter is not None:
            meters.append(meter)
    plan = obj.get("plan")
    status = obj.get("status")
    error = obj.get("error")
    return make_record(
        pid[:32], name[:24], meters,
        kind=obj.get("kind") if isinstance(obj.get("kind"), str) else pid,
        ok=ok,
        plan=plan[:16] if isinstance(plan, str) else None,
        status=status[:24] if isinstance(status, str) else None,
        error=str(error)[:120] if error else (None if ok else "Unknown error"),
    )


def parse_bridge_payload(obj):
    """Records from a bridge/custom-server JSON document.

    Accepts the full envelope {"v": 1, "providers": [...]}, a bare list of
    records, or a single record.
    """
    if isinstance(obj, dict) and "providers" in obj:
        items = obj.get("providers") or []
    elif isinstance(obj, list):
        items = obj
    elif isinstance(obj, dict):
        items = [obj]
    else:
        raise ValueError("not a ClankerTV payload")
    records = []
    for i, item in enumerate(items):
        rec = normalize_record(item, fallback_id="custom%d" % i)
        if rec is not None:
            records.append(rec)
    return records


def primary_meter(record):
    """The meter that represents the record on the overview page."""
    meters = record.get("meters") or []
    return meters[0] if meters else None


def merge_records(previous, results, now):
    """Combine one poll's results with the last known records.

    `results` is a list of (source_id, records) pairs, one per source that
    was polled this round; `previous` is the list this function returned
    last time. Every record gets `src` (the source it came from) and
    `age_base` (when its numbers were read, for the countdowns).

    A source that only reports errors this round keeps the readings it
    produced last time, flagged `stale` and carrying the new error, so one
    failed poll (or a bridge that went away) does not blank the meters.
    Records from sources that were not polled this round are dropped.
    """
    old_by_src = {}
    for rec in previous:
        old_by_src.setdefault(rec.get("src"), []).append(rec)

    def keep_stale(old, error):
        keep = dict(old)
        keep["error"] = error or "No data"
        keep["stale"] = True
        return keep

    merged = []
    for src, records in results:
        errors = {}
        for r in records:
            if not r.get("ok"):
                errors[r.get("id")] = r.get("error")
        old_good = [r for r in old_by_src.get(src, []) if r.get("meters")]
        if len(errors) == len(records) and old_good:
            # The whole source failed (unreachable bridge, dead key, no
            # Wi-Fi): keep everything it last reported.
            fallback = records[0].get("error") if records else None
            for old in old_good:
                merged.append(keep_stale(old, errors.get(old.get("id"), fallback)))
            continue
        for rec in records:
            if not rec.get("ok"):
                old = None
                for candidate in old_good:
                    if candidate.get("id") == rec.get("id"):
                        old = candidate
                        break
                if old is not None:
                    merged.append(keep_stale(old, rec.get("error")))
                    continue
            rec = dict(rec)
            rec["src"] = src
            rec["age_base"] = now
            rec["stale"] = False
            merged.append(rec)
    return merged


def normalize_secret(value):
    """Clean a pasted or QR-scanned credential.

    Removes all whitespace (a token copied from a terminal often carries a
    line wrap: "...\n  ..."), and decodes a value that arrived as a hex
    dump of the token's bytes ("73 6b 2d 61 ..." or "736b2d61..."), which
    is how a byte-mode QR code can come out of the scanner. Credentials
    and URLs never legitimately contain whitespace, so this is safe for
    every secret setting.
    """
    if not value:
        return ""
    compact = "".join(str(value).split())
    if len(compact) >= 8 and len(compact) % 2 == 0:
        try:
            raw = bytes.fromhex(compact)
        except ValueError:
            raw = None
        if raw and all(32 < b < 127 or b in (9, 10, 13, 32) for b in raw):
            return "".join(raw.decode().split())
    return compact


# ---------------------------------------------------------------------------
# Time parsing (no reliance on the local clock or time zone)
# ---------------------------------------------------------------------------

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "oct", "nov", "dec")


def days_from_civil(y, m, d):
    """Days since 1970-01-01 for a proleptic Gregorian date (H. Hinnant)."""
    if m <= 2:
        y -= 1
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def utc_epoch(y, mo, d, h=0, mi=0, s=0):
    return days_from_civil(y, mo, d) * 86400 + h * 3600 + mi * 60 + s


def parse_http_date(value):
    """'Tue, 23 Sep 2026 10:00:00 GMT' -> Unix seconds, or None."""
    if not value:
        return None
    try:
        parts = value.replace(",", " ").split()
        # [weekday] day month year hh:mm:ss GMT
        if not parts[0][0].isdigit():
            parts = parts[1:]
        day = int(parts[0])
        month = _MONTHS.index(parts[1][:3].lower()) + 1
        year = int(parts[2])
        hh, mm, ss = [int(p) for p in parts[3].split(":")]
        return utc_epoch(year, month, day, hh, mm, ss)
    except (ValueError, IndexError, AttributeError):
        return None


def parse_iso8601(value):
    """'2026-09-23T15:00:00.123+00:00' / '...Z' -> Unix seconds, or None."""
    if not isinstance(value, str) or len(value) < 19:
        return None
    try:
        year = int(value[0:4])
        month = int(value[5:7])
        day = int(value[8:10])
        hh = int(value[11:13])
        mm = int(value[14:16])
        ss = int(value[17:19])
        rest = value[19:]
        if rest.startswith("."):
            i = 1
            while i < len(rest) and rest[i].isdigit():
                i += 1
            rest = rest[i:]
        offset = 0
        if rest and rest[0] in "+-":
            sign = -1 if rest[0] == "-" else 1
            tz = rest[1:].replace(":", "")
            offset = sign * (int(tz[0:2]) * 3600 + int(tz[2:4] or 0) * 60)
        return utc_epoch(year, month, day, hh, mm, ss) - offset
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Provider parsers
# ---------------------------------------------------------------------------

def lower_keys(headers):
    out = {}
    for k in headers:
        out[str(k).lower()] = headers[k]
    return out


_CLAUDE_PREFIX = "anthropic-ratelimit-unified-"


def claude_from_headers(headers, now_epoch, pid="claude", name="Claude"):
    """Record from the unified rate-limit headers of a /v1/messages reply.

    Pro/Max subscriptions report a 5-hour session window and a 7-day window
    as utilisation fractions (0..1) with Unix reset times. Enterprise and
    overage accounts report a single spend-limit utilisation instead.
    `now_epoch` should come from the reply's Date header so reset times do
    not depend on the badge's clock.
    """
    h = lower_keys(headers)

    def util(key):
        v = _num(h.get(_CLAUDE_PREFIX + key + "-utilization"))
        return None if v is None else v * 100.0

    def reset(key):
        ts = _num(h.get(_CLAUDE_PREFIX + key + "-reset"))
        if ts is None or now_epoch is None:
            return None
        return max(0, int(ts - now_epoch))

    session = util("5h")
    if session is not None:
        meters = [make_meter("Session", session, reset("5h"), WINDOW_5H)]
        weekly = util("7d")
        if weekly is not None:
            meters.append(make_meter("Weekly", weekly, reset("7d"), WINDOW_7D))
        status = h.get(_CLAUDE_PREFIX + "5h-status") or h.get(_CLAUDE_PREFIX + "status")
        return make_record(pid, name, meters, kind="claude", plan="Subscription",
                           status=status)

    overage = util("overage")
    if overage is not None:
        meters = [make_meter("Spend", overage, reset("overage"))]
        return make_record(pid, name, meters, kind="claude", plan="Enterprise",
                           status=h.get(_CLAUDE_PREFIX + "status"))

    return error_record(pid, name, "No usage headers (not a subscription token?)",
                        kind="claude")


# Buckets reported by the OAuth usage endpoint, in display order.
_CLAUDE_USAGE_BUCKETS = (
    ("five_hour", "Session", WINDOW_5H),
    ("seven_day", "Weekly", WINDOW_7D),
    ("seven_day_opus", "Weekly Opus", WINDOW_7D),
    ("seven_day_sonnet", "Weekly Sonnet", WINDOW_7D),
)


def claude_from_usage_api(obj, now_epoch, pid="claude", name="Claude"):
    """Record from the OAuth usage endpoint that backs Claude Code's /usage.

    Utilisation there is already a percentage and resets are ISO-8601.
    Returns None when the document has none of the expected buckets, so
    the caller can fall back to the rate-limit headers.
    """
    if not isinstance(obj, dict):
        return None
    meters = []
    for key, label, window in _CLAUDE_USAGE_BUCKETS:
        bucket = obj.get(key)
        if not isinstance(bucket, dict):
            continue
        pct = _num(bucket.get("utilization"))
        if pct is None:
            continue
        reset_at = parse_iso8601(bucket.get("resets_at"))
        reset_in = None
        if reset_at is not None and now_epoch is not None:
            reset_in = max(0, int(reset_at - now_epoch))
        meters.append(make_meter(label, pct, reset_in, window))
    extra = obj.get("extra_usage")
    if isinstance(extra, dict) and extra.get("is_enabled"):
        used = _num(extra.get("used_credits"))
        limit = _num(extra.get("monthly_limit"))
        pct = _num(extra.get("utilization"))
        if used is not None or pct is not None:
            # Credits are reported in cents; the percentage comes straight
            # from the endpoint so the bar is right even if that changes.
            meters.append(make_meter(
                "Extra usage", pct=pct,
                used=(used / 100.0) if used is not None else None,
                limit=(limit / 100.0) if limit else None, unit="$"))
    if not meters:
        return None
    return make_record(pid, name, meters, kind="claude", plan="Subscription")


# Provider status strings that deserve a word in the header.
_STATUS_TEXT = {
    "rejected": "Limit reached",
    "allowed_warning": "Near limit",
}


def status_text(record):
    """Short warning for a record's provider status, or ''."""
    status = record.get("status")
    if not status:
        return ""
    return _STATUS_TEXT.get(str(status).lower(), "")


def openrouter_from_key(obj, pid="openrouter", name="OpenRouter"):
    """Record from OpenRouter's GET /api/v1/key."""
    data = obj.get("data") if isinstance(obj, dict) else None
    if not isinstance(data, dict):
        return error_record(pid, name, "Unexpected OpenRouter reply", kind="openrouter")
    usage = _num(data.get("usage"), 0.0)
    limit = _num(data.get("limit"))
    meters = []
    if limit:
        meters.append(make_meter("Key credit", used=usage, limit=limit, unit="$"))
    else:
        meters.append(make_meter("Total spend", used=usage, unit="$"))
    for key, label in (("usage_daily", "Today"), ("usage_weekly", "This week"),
                       ("usage_monthly", "This month")):
        v = _num(data.get(key))
        if v is not None:
            meters.append(make_meter(label, used=v, unit="$"))
    plan = "Free tier" if data.get("is_free_tier") else None
    return make_record(pid, name, meters, kind="openrouter", plan=plan)


def month_start_epoch(now_epoch):
    """Unix seconds of 00:00 UTC on the first day of now's month."""
    days = int(now_epoch // 86400)
    # Invert days_from_civil to get the month (civil_from_days).
    z = days + 719468
    era = (z if z >= 0 else z - 146096) // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    y = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    m = mp + (3 if mp < 10 else -9)
    if m <= 2:
        y += 1
    return utc_epoch(y, m, 1), y, m


def next_month_start_epoch(now_epoch):
    _, y, m = month_start_epoch(now_epoch)
    if m == 12:
        return utc_epoch(y + 1, 1, 1)
    return utc_epoch(y, m + 1, 1)


def month_spend_meters(total, today, now_epoch, budget=None, unit="$"):
    """'This month' (against an optional budget, resetting at month end)
    and 'Today' spend meters shared by the pay-per-use providers."""
    reset_in = next_month_start_epoch(now_epoch) - int(now_epoch)
    start, _, _ = month_start_epoch(now_epoch)
    window = next_month_start_epoch(now_epoch) - start
    return [
        make_meter("This month", used=total, limit=budget or None, unit=unit,
                   reset_in=reset_in, window=window if budget else None),
        make_meter("Today", used=today, unit=unit),
    ]


def openai_from_costs(buckets, now_epoch, budget=None, pid="openai", name="OpenAI"):
    """Record from the OpenAI organization costs API (list of day buckets)."""
    total = 0.0
    today = 0.0
    day_start = int(now_epoch // 86400) * 86400
    for bucket in buckets or ():
        amount = 0.0
        for result in bucket.get("results") or ():
            value = (result.get("amount") or {}).get("value")
            amount += _num(value, 0.0)
        total += amount
        if _num(bucket.get("start_time"), 0) >= day_start:
            today += amount
    return make_record(pid, name, month_spend_meters(total, today, now_epoch, budget),
                       kind="openai", plan="API")


def anthropic_from_cost_report(buckets, now_epoch, budget=None, pid="anthropic",
                               name="Anthropic API"):
    """Record from the Anthropic Admin API cost report (day buckets).

    Amounts are decimal strings in cents ("123.45" USD = $1.23), per the
    reference; the buckets carry RFC 3339 `starting_at` times.
    """
    total = 0.0
    today = 0.0
    day_start = int(now_epoch // 86400) * 86400
    for bucket in buckets or ():
        amount = 0.0
        for result in bucket.get("results") or ():
            amount += _num(result.get("amount"), 0.0) / 100.0
        total += amount
        started = parse_iso8601(bucket.get("starting_at"))
        if started is not None and started >= day_start:
            today += amount
    return make_record(pid, name, month_spend_meters(total, today, now_epoch, budget),
                       kind="anthropic", plan="Console")


# Only ASCII units: the badge's built-in fonts have no currency glyphs.
_CURRENCY_UNITS = {"USD": "$", "CNY": "CNY"}


def deepseek_from_balance(obj, pid="deepseek", name="DeepSeek"):
    """Record from DeepSeek's GET /user/balance: remaining credit."""
    infos = obj.get("balance_infos") if isinstance(obj, dict) else None
    if not isinstance(infos, list) or not infos:
        return error_record(pid, name, "Unexpected DeepSeek reply", kind="deepseek")
    meters = []
    for info in infos:
        unit = _CURRENCY_UNITS.get(info.get("currency"), str(info.get("currency") or "")[:4])
        total = _num(info.get("total_balance"))
        if total is None:
            continue
        label = "Balance" if len(infos) == 1 else "Balance " + str(info.get("currency"))
        meters.append(make_meter(label, used=total, unit=unit))
        granted = _num(info.get("granted_balance"))
        if granted:
            meters.append(make_meter("Granted", used=granted, unit=unit))
    if not meters:
        return error_record(pid, name, "No balance in DeepSeek reply", kind="deepseek")
    status = "rejected" if obj.get("is_available") is False else "allowed"
    return make_record(pid, name, meters, kind="deepseek", plan="API", status=status)


def _cents(obj, *path):
    """Follow `path` through nested dicts to a {"val": "<cents>"} and
    return dollars, or None."""
    node = obj
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    if isinstance(node, dict):
        node = node.get("val")
    value = _num(node)
    return None if value is None else value / 100.0


def xai_from_billing(preview, limits, balance, now_epoch, pid="xai", name="xAI"):
    """Record from xAI's Management API billing endpoints (all USD cents).

    preview: postpaid invoice preview for the current cycle;
    limits: postpaid spending limits (soft limit becomes the budget);
    balance: prepaid balance. Any of them may be None.
    """
    meters = []
    spent = _cents(preview, "coreInvoice", "amountBeforeVat")
    soft = _cents(limits, "spendingLimits", "softSl")
    if spent is not None:
        meters.extend(month_spend_meters(spent, 0.0, now_epoch, soft or None)[:1])
    credit = _cents(balance, "total")
    if credit is not None:
        meters.append(make_meter("Prepaid balance", used=credit, unit="$"))
    if not meters:
        return error_record(pid, name, "No billing data in xAI reply", kind="xai")
    return make_record(pid, name, meters, kind="xai", plan="API")


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return "<1m"
    minutes = seconds // 60
    if minutes < 60:
        return "%dm" % minutes
    if minutes < 1440:
        return "%dh %dm" % (minutes // 60, minutes % 60)
    return "%dd %dh" % (minutes // 1440, (minutes % 1440) // 60)


def remaining(meter, elapsed):
    """Seconds left in the meter's window, `elapsed` seconds after fetch."""
    reset_in = meter.get("reset_in")
    if reset_in is None:
        return None
    return max(0, int(reset_in - elapsed))


def format_reset(meter, elapsed=0):
    left = remaining(meter, elapsed)
    if left is None:
        return ""
    if left <= 0:
        return "Resetting now"
    return "Resets in " + format_duration(left)


def format_money(value):
    value = float(value)
    if value >= 1000:
        return "%.0f" % value
    if value >= 100:
        return "%.1f" % value
    return "%.2f" % value


def format_amount(meter):
    """'$4.12 of $20.00', '$12.40', or '' for a pure percentage meter."""
    used = meter.get("used")
    if used is None:
        return ""
    unit = meter.get("unit") or ""
    if len(unit) > 1:
        unit += " "          # "CNY 110.0", but "$4.12"
    text = unit + format_money(used)
    limit = meter.get("limit")
    if limit:
        text += " of " + unit + format_money(limit)
    return text


def format_pct(pct):
    if pct is None:
        return "--"
    return "%d%%" % int(pct + 0.5)


def level(pct):
    """0 = green, 1 = amber, 2 = red; None when there is no percentage."""
    if pct is None:
        return None
    if pct >= LEVEL_RED_PCT:
        return 2
    if pct >= LEVEL_AMBER_PCT:
        return 1
    return 0


def expected_pct(meter, elapsed=0):
    """Where usage would be if spent evenly across the window, or None."""
    window = meter.get("window")
    left = remaining(meter, elapsed)
    if not window or left is None:
        return None
    return max(0.0, min(100.0, 100.0 * (1.0 - float(left) / window)))


def pace_text(meter, elapsed=0):
    """'On pace', '12% ahead of pace', '8% under pace' or ''."""
    pct = meter.get("pct")
    expected = expected_pct(meter, elapsed)
    if pct is None or expected is None:
        return ""
    diff = pct - expected
    if diff > PACE_TOLERANCE_PCT:
        return "%d%% ahead of pace" % int(diff + 0.5)
    if diff < -PACE_TOLERANCE_PCT:
        return "%d%% under pace" % int(-diff + 0.5)
    return "On pace"


def format_age(seconds):
    seconds = int(seconds)
    if seconds < 5:
        return "just now"
    if seconds < 60:
        return "%ds ago" % seconds
    return format_duration(seconds) + " ago"


# ---------------------------------------------------------------------------
# Usage rate (drives the mascot's mood, like Clawdmeter's splash groups)
# ---------------------------------------------------------------------------

MOOD_IDLE, MOOD_NORMAL, MOOD_ACTIVE, MOOD_HEAVY = 0, 1, 2, 3
MOOD_NAMES = ("Idle", "Steady", "Busy", "Flat out")

# %/min thresholds. A 5-hour window burns 100% in 300 min at 0.33 %/min, so
# "Flat out" starts where you would exhaust the session before it resets.
_RATE_NORMAL = 0.10
_RATE_ACTIVE = 0.20
_RATE_HEAVY = 0.33


class RateTracker:
    """Smoothed %/min of one meter over the last few samples.

    Samples are (seconds, pct). A drop of more than 5 points means the
    window reset, so the history restarts. The rate is only trusted once
    the samples span `min_span` seconds, so one noisy step between two
    polls cannot flip the mood to "Flat out".
    """

    def __init__(self, size=6, min_span=240):
        self.size = size
        self.min_span = min_span
        self.samples = []

    def reset(self):
        self.samples = []

    def sample(self, t, pct):
        if pct is None:
            return
        if self.samples:
            last_t, last_pct = self.samples[-1]
            if t < last_t or pct + 5 < last_pct:
                self.samples = []
        self.samples.append((t, pct))
        if len(self.samples) > self.size:
            self.samples.pop(0)

    def rate(self):
        """%/min, or None until enough history has accumulated."""
        if len(self.samples) < 2:
            return None
        t0, p0 = self.samples[0]
        t1, p1 = self.samples[-1]
        span = t1 - t0
        if span < self.min_span:
            return None
        return max(0.0, p1 - p0) * 60.0 / span

    def mood(self):
        r = self.rate()
        if r is None or r < _RATE_NORMAL:
            return MOOD_IDLE
        if r < _RATE_ACTIVE:
            return MOOD_NORMAL
        if r < _RATE_HEAVY:
            return MOOD_ACTIVE
        return MOOD_HEAVY


# Status-line verbs while a refresh is in flight. ClankerTV's own robot
# flavoured list.
WHIMSY = (
    "Clanking", "Whirring", "Beeping", "Booping", "Tokenizing", "Defragging",
    "Overclocking", "Calibrating", "Rewiring", "Oiling gears", "Buffering",
    "Bit-twiddling", "Soldering", "Polling", "Degaussing", "Rebooting nothing",
    "Counting tokens", "Tuning in", "Adjusting antenna", "Warming valves",
    "Compiling", "Humming", "Blinking", "Sprocketing", "Servo-ing",
)

SPINNER = ("|", "/", "-", "\\")


# ---------------------------------------------------------------------------
# Demo data (first launch without configuration)
# ---------------------------------------------------------------------------

def demo_records():
    return [
        make_record("claude", "Claude", [
            make_meter("Session", 46, 2 * 3600 + 14 * 60, WINDOW_5H),
            make_meter("Weekly", 31, 3 * 86400 + 7 * 3600, WINDOW_7D),
            make_meter("Weekly Opus", 83, 3 * 86400 + 7 * 3600, WINDOW_7D),
        ], kind="claude", plan="Max", status="allowed"),
        make_record("openai", "OpenAI", [
            make_meter("This month", used=18.42, limit=50, unit="$",
                       reset_in=9 * 86400, window=30 * 86400),
            make_meter("Today", used=1.37, unit="$"),
        ], kind="openai", plan="API"),
        make_record("openrouter", "OpenRouter", [
            make_meter("Key credit", used=4.12, limit=20, unit="$"),
            make_meter("Today", used=0.18, unit="$"),
        ], kind="openrouter"),
    ]
