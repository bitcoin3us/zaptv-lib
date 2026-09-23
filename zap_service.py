# SPDX-License-Identifier: MIT
# Copyright (c) 2026 ZapTV.org
# Part of zaptv-lib (https://github.com/bitcoin3us/zaptv-lib): MIT-licensed
# modules shared by the ZapTV family of MicroPythonOS apps.

# zap_service.py — ZapMonitor: glue between BlockTV and the shared
# NostrManager singleton (nostr_service.py).
#
# Provides:
#   * Nostr zap watching — subscribes to kind 9735 zap receipts that
#     p-tag the configured pubkey and reports the newest one.
#   * NWC integration — configures Nostr Wallet Connect on the manager
#     and reports wallet balance updates; incoming NWC payments also
#     surface as zap-style events (so a zap to the wallet's lightning
#     address shows up even without an npub configured).
#
# NOTE: the NostrManager singleton is shared device-wide (whichever app's
# copy of nostr_service.py imports first wins the sys.modules slot), and
# set_nwc_callbacks() is global — running BlockTV's NWC integration at
# the same time as another NWC app (e.g. Lightning Piggy) means the two
# fight over callbacks. Last one to resume wins.

import json
import time

from mpos import TaskManager

# nostr_service is ~56 KB of Python and MicroPython parses it on every
# launch. Importing it lazily keeps it off the startup path entirely for
# users with no nostr/NWC configured, and defers it behind the splash for
# those who have.
_NostrManager = None


def _manager():
    global _NostrManager
    if _NostrManager is None:
        from nostr_service import NostrManager
        _NostrManager = NostrManager
    return _NostrManager


ZAP_RECEIPT_KIND = 9735
SUBSCRIPTION_NAME = "blocktv_zaps"

# The subscription asks for the newest few zap receipts with no time
# window. An earlier 24-hour `since` filter meant an account that gets
# the occasional zap showed "no zaps yet" — the field is "Latest Zap",
# so the most recent one matters however old it is. Dedup by event id
# handles the relay replaying them on every reconnect, and the splash
# only fires for zaps that arrive while running.
ZAP_FETCH_LIMIT = 5


def pubkey_to_hex(pubkey_or_npub):
    pubkey_or_npub = pubkey_or_npub.strip()
    if pubkey_or_npub.startswith("npub1"):
        from nostr.key import PublicKey
        return PublicKey.from_npub(pubkey_or_npub).hex()
    hexkey = pubkey_or_npub.lower()
    if len(hexkey) == 64 and all(c in "0123456789abcdef" for c in hexkey):
        return hexkey
    raise ValueError("Not an npub or 64-char hex pubkey")


def bolt11_amount_sats(invoice):
    """Amount in sats encoded in a bolt11 invoice's human-readable part,
    or None if absent/unparseable."""
    try:
        inv = invoice.lower()
        pos = inv.rfind("1")
        if pos < 4:
            return None
        hrp = inv[:pos]
        amount = None
        for prefix in ("lnbcrt", "lntbs", "lntb", "lnbc"):
            if hrp.startswith(prefix):
                amount = hrp[len(prefix):]
                break
        if not amount:
            return None
        mult = amount[-1]
        if mult in "munp":
            digits = amount[:-1]
            if not digits.isdigit():
                return None
            d = int(digits)
            if mult == "m":
                msat = d * 100_000_000
            elif mult == "u":
                msat = d * 100_000
            elif mult == "n":
                msat = d * 100
            else:  # p — sub-msat precision
                msat = d // 10
            return msat // 1000
        if amount.isdigit():
            return int(amount) * 100_000_000
        return None
    except Exception:
        return None


def parse_zap_receipt(nostr_event):
    """Extract {sats, comment, at, id} from a kind 9735 zap receipt."""
    event = nostr_event.event
    bolt11 = None
    description = None
    for tag in event.tags or []:
        if not tag or len(tag) < 2:
            continue
        if tag[0] == "bolt11":
            bolt11 = tag[1]
        elif tag[0] == "description":
            description = tag[1]
    sats = bolt11_amount_sats(bolt11) if bolt11 else None
    comment = ""
    if description:
        try:
            zap_request = json.loads(description)
            comment = zap_request.get("content") or ""
        except Exception:
            pass
    return {
        "sats": sats,
        "comment": comment,
        "at": event.created_at,
        "id": getattr(event, "id", None),
        "source": "nostr",
    }


class ZapMonitor:

    def __init__(self):
        self._on_zap = None
        self._on_balance = None
        self._last_zap = None
        self._seen_ids = []
        self._active = False
        self._nwc_active = False
        self._balance = None

    @property
    def last_zap(self):
        return self._last_zap

    @property
    def balance(self):
        return self._balance

    def is_connected(self):
        """True while the shared NostrManager has a live relay connection."""
        if not self._active:
            return False
        try:
            return _manager().get_instance().is_connected()
        except Exception:
            return False

    def start(self, npub=None, relays=None, nwc_url=None,
              on_zap=None, on_balance=None):
        """Configure and start watching. All arguments optional — whatever
        is configured gets activated."""
        self._on_zap = on_zap
        self._on_balance = on_balance
        self._active = True

        mgr = _manager().get_instance()
        if not mgr.is_running():
            mgr.start()

        if npub and relays:
            try:
                pubkey_hex = pubkey_to_hex(npub)
            except Exception as e:
                print("BlockTV: invalid zap pubkey: {}".format(e))
                pubkey_hex = None
            if pubkey_hex:
                self._configure_relays(mgr, relays)
                from nostr.filter import Filter, Filters
                filters = Filters([Filter(
                    kinds=[ZAP_RECEIPT_KIND],
                    pubkey_refs=[pubkey_hex],
                )])
                mgr.add_subscription(
                    SUBSCRIPTION_NAME, filters, callback=self._zap_event_cb,
                    limit=ZAP_FETCH_LIMIT,
                )

        if nwc_url:
            mgr.set_nwc_callbacks(
                balance_cb=self._balance_cb,
                notification_cb=self._notification_cb,
            )
            try:
                mgr.configure_nwc(nwc_url)
                self._nwc_active = True
                TaskManager.create_task(self._initial_balance_fetch(mgr))
            except Exception as e:
                print("BlockTV: couldn't configure NWC: {}".format(e))

    def stop(self):
        """Detach callbacks. The shared manager keeps running for other apps."""
        self._active = False
        if _NostrManager is None:
            return      # never started, so nothing to detach — and no
                        # reason to pay for the import just to stop
        mgr = _manager().get_instance()
        try:
            mgr.close_subscription(SUBSCRIPTION_NAME)
        except Exception as e:
            print("BlockTV: error closing zap subscription: {}".format(e))
        if self._nwc_active:
            mgr.set_nwc_callbacks()
            self._nwc_active = False

    def _configure_relays(self, mgr, relays):
        """Add relays for the zap subscription without configuring an
        identity (no nsec, no relay-list publishing). Falls back to poking
        manager internals when an older nostr_service.py copy without
        configure_relays() won the sys.modules slot."""
        try:
            mgr.configure_relays(relays)
        except AttributeError:
            if isinstance(relays, str):
                relays = [r.strip() for r in relays.split(",") if r.strip()]
            added = False
            for url in relays:
                if url and url not in mgr._default_relays:
                    mgr._default_relays.append(url)
                    added = True
            mgr._configured_relays = list(mgr._default_relays)
            if added:
                mgr._relays_dirty = True
            mgr._ensure_main_task()

    async def _initial_balance_fetch(self, mgr):
        """The manager's poll loop covers steady-state; this covers the
        first display after connect."""
        for _ in range(60):
            if not self._active:
                return
            if mgr.is_connected():
                break
            await TaskManager.sleep(1)
        if self._active and mgr.is_connected():
            try:
                mgr.nwc_fetch_balance()
            except Exception as e:
                print("BlockTV: initial balance fetch failed: {}".format(e))

    # --- NostrManager callbacks ---

    def _zap_event_cb(self, nostr_event):
        if not self._active:
            return
        try:
            zap = parse_zap_receipt(nostr_event)
        except Exception as e:
            print("BlockTV: failed to parse zap receipt: {}".format(e))
            return
        zap_id = zap.get("id")
        if zap_id and zap_id in self._seen_ids:
            return
        if zap_id:
            self._seen_ids.append(zap_id)
            if len(self._seen_ids) > 50:
                self._seen_ids = self._seen_ids[-50:]
        is_new = self._last_zap is None or (zap.get("at") or 0) >= (self._last_zap.get("at") or 0)
        if not is_new:
            return
        first = self._last_zap is None
        self._last_zap = zap
        if self._on_zap:
            # Suppress the splash for the backlog zap loaded at startup:
            # only zaps that arrive while running are "live".
            live = not first or (time.time() - (zap.get("at") or 0)) < 120
            self._on_zap(zap, live)

    def _balance_cb(self, new_balance):
        if not self._active:
            return
        self._balance = new_balance
        if self._on_balance:
            self._on_balance(new_balance)

    def _notification_cb(self, notification):
        if not self._active:
            return
        if "static_receive_code" in notification:
            return
        try:
            amount = round(int(notification.get("amount", 0)) / 1000)
        except (TypeError, ValueError):
            amount = 0
        ntype = notification.get("type")
        if ntype == "incoming":
            if self._balance is not None:
                self._balance += amount
                if self._on_balance:
                    self._on_balance(self._balance)
            comment = self._comment_from_transaction(notification)
            zap = {
                "sats": amount,
                "comment": comment,
                "at": notification.get("created_at") or int(time.time()),
                "id": None,
                "source": "nwc",
            }
            self._last_zap = zap
            if self._on_zap:
                self._on_zap(zap, True)
        elif ntype == "outgoing":
            if self._balance is not None:
                self._balance -= amount
                if self._on_balance:
                    self._on_balance(self._balance)

    def _comment_from_transaction(self, transaction):
        """description may be plain text, LNURL metadata JSON, or a zap
        request JSON — dig out something human-readable."""
        comment = transaction.get("description") or ""
        try:
            parsed = json.loads(comment)
            if isinstance(parsed, dict):
                return parsed.get("content") or ""
            if isinstance(parsed, list):
                for entry in parsed:
                    if isinstance(entry, list) and len(entry) > 1 and entry[0] == "text/plain":
                        return entry[1]
                return ""
        except Exception:
            pass
        return comment
