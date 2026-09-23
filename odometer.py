# SPDX-License-Identifier: MIT
# Copyright (c) 2026 ZapTV.org
# Part of zaptv-lib (https://github.com/bitcoin3us/zaptv-lib): MIT-licensed
# modules shared by the ZapTV family of MicroPythonOS apps.

# odometer.py — rolling-counter number display for BlockTV.
#
# Renders a value string as a row of per-character cells. Digit cells are
# small clipped windows over a vertical strip of digits; when the value
# changes, each strip animates to its new digit like a mechanical
# odometer (always rolling forward, 9 wraps to 0 through a doubled
# strip). Non-digit characters (",", ":", ".", letters) are plain labels
# swapped in place.
#
# The row rebuilds (no animation) whenever the "shape" changes: different
# length, different non-digit characters, or a different font.

import lvgl as lv


def _safe(callback):
    """Skip callbacks whose widgets were deleted mid-animation."""
    try:
        callback()
    except Exception as e:
        if "LvReferenceError" in type(e).__name__ or "deleted" in str(e):
            pass
        else:
            raise


# Doubled digit run so 9 -> 0 can keep rolling forward.
_STRIP_TEXT = "\n".join("0123456789" * 2)


class Odometer:

    DURATION_MS = 600

    def __init__(self, parent):
        cont = lv.obj(parent)
        cont.set_style_bg_opa(lv.OPA.TRANSP, lv.PART.MAIN)
        cont.set_style_border_width(0, lv.PART.MAIN)
        cont.set_style_pad_all(0, lv.PART.MAIN)
        cont.set_style_radius(0, lv.PART.MAIN)
        cont.set_scrollbar_mode(lv.SCROLLBAR_MODE.OFF)
        cont.remove_flag(lv.obj.FLAG.SCROLLABLE)
        cont.remove_flag(lv.obj.FLAG.CLICKABLE)
        self.cont = cont
        self._cells = []      # [{"ch", "digit", "cell", "strip", "pos"}]
        self._font = None
        self._shape = None
        self._step = 0        # vertical distance between strip digits

    def align(self, align, x, y):
        self.cont.align(align, x, y)
        self._align = (align, x, y)

    def _measure(self, parent, font, text):
        """Width of `text` rendered in `font` (probe label, then delete)."""
        probe = lv.label(parent)
        probe.set_text(text)
        probe.set_style_text_font(font, lv.PART.MAIN)
        probe.update_layout()
        w = probe.get_width()
        h = probe.get_height()
        probe.delete()
        return w, h

    def set_value(self, text, font, color, animate=True):
        shape = tuple("#" if c.isdigit() else c for c in text)
        if font is not self._font or shape != self._shape:
            self._rebuild(text, font, color)
            return
        for cell, ch in zip(self._cells, text):
            if cell["digit"]:
                self._roll_to(cell, int(ch), animate)
            cell["ch"] = ch

    def _rebuild(self, text, font, color):
        for cell in self._cells:
            lv.anim_delete(cell["strip"] or cell["cell"], None)
        self.cont.clean()
        self._cells = []
        self._font = font
        self._shape = tuple("#" if c.isdigit() else c for c in text)

        digit_w, line_h = self._measure(self.cont, font, "8")
        # Line pitch for the rolling strip, from a two-line probe. Measured
        # once per rebuild rather than by giving every digit a 20-line
        # label — at large font sizes that glyph work dominated the whole
        # screen rebuild (a single full-screen number cost ~800 ms on the
        # ESP32; most of it was strips nobody could see yet).
        self._step = max(1, self._measure(self.cont, font, "8\n8")[1] - line_h)

        # Build cells left to right.
        x = 0
        for ch in text:
            if ch.isdigit():
                cell = lv.obj(self.cont)
                cell.set_style_bg_opa(lv.OPA.TRANSP, lv.PART.MAIN)
                cell.set_style_border_width(0, lv.PART.MAIN)
                cell.set_style_pad_all(0, lv.PART.MAIN)
                cell.set_style_radius(0, lv.PART.MAIN)
                cell.set_scrollbar_mode(lv.SCROLLBAR_MODE.OFF)
                cell.remove_flag(lv.obj.FLAG.SCROLLABLE)
                cell.remove_flag(lv.obj.FLAG.CLICKABLE)
                cell.set_size(digit_w, line_h)
                cell.set_pos(x, 0)

                # Just the digit that is actually visible. The full strip
                # is swapped in only while this digit is mid-roll.
                strip = lv.label(cell)
                strip.set_text(ch)
                strip.set_style_text_font(font, lv.PART.MAIN)
                strip.set_style_text_color(color, lv.PART.MAIN)
                strip.set_style_text_align(lv.TEXT_ALIGN.CENTER, lv.PART.MAIN)
                strip.set_width(digit_w)
                pos = int(ch)
                strip.set_pos(0, 0)

                self._cells.append({"ch": ch, "digit": True, "cell": cell,
                                    "strip": strip, "pos": pos})
                x += digit_w
            else:
                w, _h = self._measure(self.cont, font, ch)
                label = lv.label(self.cont)
                label.set_text(ch)
                label.set_style_text_font(font, lv.PART.MAIN)
                label.set_style_text_color(color, lv.PART.MAIN)
                label.set_pos(x, 0)
                self._cells.append({"ch": ch, "digit": False, "cell": label,
                                    "strip": None, "pos": None})
                x += w

        self.cont.set_size(max(x, 1), line_h)
        align = getattr(self, "_align", None)
        if align:
            self.cont.align(align[0], align[1], align[2])

    def _roll_to(self, cell, target, animate):
        if cell["pos"] == target:
            return
        strip = cell["strip"]
        step = self._step
        lv.anim_delete(strip, None)
        start = cell["pos"]
        # Always roll forward like a counter; the strip is doubled so
        # positions 10..19 are valid and snap back after the animation.
        end = target if target > start else target + 10
        cell["pos"] = target
        if not animate or step <= 0:
            strip.set_text(str(target))
            strip.set_y(0)
            return
        # Expand to the rolling strip for the duration of the animation,
        # then collapse back to the single visible digit.
        strip.set_text(_STRIP_TEXT)
        strip.set_y(-start * step)
        anim = lv.anim_t()
        anim.init()
        anim.set_var(strip)
        anim.set_duration(self.DURATION_MS)
        anim.set_values(-start * step, -end * step)
        anim.set_custom_exec_cb(
            lambda _a, v, s=strip: _safe(lambda: s.set_y(v)))
        anim.set_path_cb(lv.anim_t.path_ease_in_out)
        anim.set_completed_cb(
            lambda *_args, s=strip, t=target: _safe(
                lambda: (s.set_text(str(t)), s.set_y(0))))
        anim.start()
