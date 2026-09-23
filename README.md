# zaptv-lib

Shared, MIT-licensed modules for the ZapTV family of
[MicroPythonOS](https://github.com/MicroPythonOS/MicroPythonOS) apps
([ZapTV](https://github.com/bitcoin3us/ZapTV),
[BlockTV](https://github.com/bitcoin3us/blocktv),
[ClipTV](https://github.com/bitcoin3us/cliptv)).

The apps themselves are GPL-3.0-or-later. The plumbing that other MPOS
apps might want to borrow lives here instead, under MIT, so it can be
reused without licence friction.

| module | what it is |
|---|---|
| `nostr_service.py` | Nostr relay manager + NWC (Nostr Wallet Connect) client service. Derived from Lightning Piggy's, see the file header. |
| `zap_service.py` | `ZapMonitor`: watches zap receipts for an npub and an NWC wallet balance, with callbacks. |
| `market_data.py` | mempool.space client: block height, spot price in twelve currencies (five derived from USD via ECB rates), fees, streaming price history with per-range thinning, all-time-high scan. |
| `odometer.py` | Rolling-counter number display widget for LVGL. |
| `field_picker.py` | `FieldPickerActivity`: categorised field picker with drag-to-reorder; the app supplies its field registry. |

## Using it in an app

MPOS apps ship as self-contained `.mpk` packages with flat module
layout, so there is no shared-library install step: apps **vendor** the
modules they use. Each consuming app carries a `tools/sync-lib.sh` that
copies the wanted files from a checkout of this repo and records the
library commit it took them from in `zaptv-lib.lock`. Edit the modules
here, commit, then re-run the sync in each app — never edit the vendored
copies in place.

```sh
# in an app repo, with ../dev-zaptv-lib checked out beside it
./tools/sync-lib.sh
```

## Licence

MIT — see [LICENSE](LICENSE). `nostr_service.py` incorporates code from
Lightning Piggy (MicroPythonOS, Copyright (c) 2025 MicroPythonOS, MIT);
its notice is preserved in the file header.
