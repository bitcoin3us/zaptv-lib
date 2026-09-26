# SPDX-License-Identifier: MIT
# Copyright (c) 2026 ZapTV.org
# Part of zaptv-lib (https://github.com/bitcoin3us/zaptv-lib): MIT-licensed
# modules shared by the ZapTV family of MicroPythonOS apps.

"""Categorised field picker with drag-to-reorder, for MicroPythonOS apps.

One screen: the chosen fields sit at the top in the order they will be
laid out, draggable by their handles (or nudged along from a keypad), and
the rest are listed below by category. Changes are held in memory and
only persisted by the Save button; backing out without saving discards
them.

Subclass FieldPickerActivity and supply the app's own field registry:

    CATEGORIES     ((section name, (field_id, ...)), ...)
    TITLES         {field_id: display name}
    MAX_FIELDS     how many fields one screen may hold (default 8)
    DEFAULT_FIELD  the field a brand-new screen starts with (default:
                   the first field of the first category)

    def load_screens(self, prefs):           -> [[field_id, ...], ...]
    def save_screens(self, prefs, screens):  persist that list

A screen entry may also be a dict {"fields": [...], ...}: any other keys
(a layout name, say) ride along untouched in `self._extra`, which the
app may edit before Save. `extra_buttons(row)` lets the app add its own
buttons to the Cancel/Save row.

Launch it with Intent extras `prefs` (the app's SharedPreferences) and
`index` (which screen to edit; an index past the end means "new screen").
"""

import lvgl as lv
from mpos import Activity, DisplayMetrics, FontManager, add_focus_border


def button_row(parent):
    """Transparent horizontal container so two buttons share one line
    instead of each eating a row of scroll space."""
    row = lv.obj(parent)
    row.set_width(lv.pct(100))
    row.set_height(lv.SIZE_CONTENT)
    row.set_style_bg_opa(lv.OPA.TRANSP, lv.PART.MAIN)
    row.set_style_border_width(0, lv.PART.MAIN)
    row.set_style_pad_all(0, lv.PART.MAIN)
    row.set_style_pad_column(6, lv.PART.MAIN)
    row.set_flex_flow(lv.FLEX_FLOW.ROW)
    row.set_scrollbar_mode(lv.SCROLLBAR_MODE.OFF)
    row.remove_flag(lv.obj.FLAG.SCROLLABLE)
    return row


def row_button(row, text, on_click, grow=1):
    btn = lv.button(row)
    btn.set_flex_grow(grow)
    btn.add_event_cb(lambda e: on_click(), lv.EVENT.CLICKED, None)
    # Joins the focus group: on a device with no touchscreen this is the
    # only way the button can be reached at all.
    add_focus_border(btn)
    label = lv.label(btn)
    label.set_text(text)
    label.center()
    return btn


def no_scroll_chain(obj):
    """Stop a drag on this widget from scrolling the page behind it.

    LVGL hands an unhandled scroll gesture up to the nearest scrollable
    ancestor; the picker scrolls, so without this a slide meant to
    reorder would take the whole list with it."""
    for flag in ("SCROLL_CHAIN_VER", "SCROLL_CHAIN"):
        value = getattr(lv.obj.FLAG, flag, None)
        if value is not None:
            obj.remove_flag(value)
            return


class FieldPickerActivity(Activity):
    """Which fields appear on one screen, and in what order."""

    CATEGORIES = ()
    TITLES = {}
    MAX_FIELDS = 8
    DEFAULT_FIELD = None
    ROW_H = 30

    # --- the app supplies these ---

    def load_screens(self, prefs):
        raise NotImplementedError

    def save_screens(self, prefs, screens):
        raise NotImplementedError

    def extra_buttons(self, row):
        """Add app-specific buttons between Cancel and Save."""

    @staticmethod
    def _entry_fields(entry):
        return list(entry.get("fields") or []) if isinstance(entry, dict) else list(entry)

    # --- lifecycle ---

    def onCreate(self):
        extras = self.getIntent().extras or {}
        self.prefs = extras.get("prefs")
        self.index = extras.get("index", 0)
        self._selected = []
        self._extra = {}
        self._dict_entries = False
        self._is_new = False
        self._loaded = False
        self._rows = []
        self._drag_idx = None
        self._drag_target = 0
        self._drag_y0 = 0
        screen = lv.obj()
        screen.set_style_pad_all(DisplayMetrics.pct_of_width(2), lv.PART.MAIN)
        screen.set_flex_flow(lv.FLEX_FLOW.COLUMN)
        screen.set_style_border_width(0, lv.PART.MAIN)
        self._screen = screen
        self.setContentView(screen)

    def onResume(self, screen):
        super().onResume(screen)
        self._screen = screen
        if not self._loaded:
            # Only on the way in: re-reading here would throw away the
            # edits made before switching modes.
            screens = self.load_screens(self.prefs)
            self._is_new = self.index >= len(screens)
            self._dict_entries = bool(screens) and isinstance(screens[0], dict)
            if self._is_new:
                self._selected = [self._default_field()]
            else:
                entry = screens[self.index]
                self._selected = self._entry_fields(entry)
                if isinstance(entry, dict):
                    self._extra = {k: v for k, v in entry.items() if k != "fields"}
            self._loaded = True
        self._render()

    def _default_field(self):
        if self.DEFAULT_FIELD is not None:
            return self.DEFAULT_FIELD
        for _name, ids in self.CATEGORIES:
            if ids:
                return ids[0]
        return None

    def _title(self, field_id):
        return self.TITLES.get(field_id, field_id)

    # --- rendering ---

    def _render(self):
        screen = self._screen
        # Selecting a field rebuilds the list; without this the view would
        # jump back to the top on every tap.
        screen.update_layout()
        keep_scroll = screen.get_scroll_y()
        screen.clean()
        self._drag_idx = None
        self._rows = []

        header = lv.label(screen)
        header.set_text("New screen" if self._is_new
                        else "Screen {} fields".format(self.index + 1))
        header.set_style_text_font(FontManager.getFont(size=18), lv.PART.MAIN)

        self._render_selected(screen)
        for name, field_ids in self.CATEGORIES:
            available = [f for f in field_ids if f not in self._selected]
            if not available:
                continue
            self._section(screen, name)
            for field_id in available:
                self._available_row(screen, field_id)

        actions = button_row(screen)
        row_button(actions, lv.SYMBOL.CLOSE + "  Cancel", self.finish, grow=1)
        self.extra_buttons(actions)
        row_button(actions, lv.SYMBOL.OK + "  Save", self._save, grow=2)

        if not self._is_new and len(self.load_screens(self.prefs)) > 1:
            delete_btn = lv.button(screen)
            delete_btn.set_width(lv.pct(100))
            delete_btn.add_event_cb(self._delete_screen, lv.EVENT.CLICKED, None)
            add_focus_border(delete_btn)
            delete_label = lv.label(delete_btn)
            delete_label.set_text(lv.SYMBOL.TRASH + "  Delete Screen")
            delete_label.center()

        screen.update_layout()
        screen.scroll_to_y(keep_scroll, 0)

    def _section(self, screen, text):
        label = lv.label(screen)
        label.set_text(text.upper())
        label.set_style_text_font(FontManager.getFont(size=12), lv.PART.MAIN)
        label.set_style_text_opa(lv.OPA._50, lv.PART.MAIN)
        label.set_style_text_letter_space(1, lv.PART.MAIN)
        label.set_style_pad_top(4, lv.PART.MAIN)

    def _available_row(self, screen, field_id):
        """An unselected field: tap to move it up into Selected."""
        row = lv.obj(screen)
        row.set_width(lv.pct(100))
        row.set_height(self.ROW_H - 3)
        row.set_style_pad_all(0, lv.PART.MAIN)
        row.set_style_border_width(0, lv.PART.MAIN)
        row.set_style_bg_opa(lv.OPA.TRANSP, lv.PART.MAIN)
        row.set_scrollbar_mode(lv.SCROLLBAR_MODE.OFF)
        row.remove_flag(lv.obj.FLAG.SCROLLABLE)
        row.add_flag(lv.obj.FLAG.CLICKABLE)
        row.add_event_cb(lambda e, f=field_id: self._select(f),
                         lv.EVENT.CLICKED, None)
        add_focus_border(row)
        name = lv.label(row)
        name.set_text(lv.SYMBOL.PLUS + "  " + self._title(field_id))
        name.set_style_text_font(FontManager.getFont(size=14), lv.PART.MAIN)
        name.align(lv.ALIGN.LEFT_MID, 6, 0)

    def _render_selected(self, screen):
        """The chosen fields, in the order they will be laid out.

        Rows live in a fixed-height container with scroll chaining off, so
        sliding one reorders instead of scrolling the page behind it — the
        reason this can share a screen with the full field list at all."""
        self._section(screen, "Selected")
        # The grip and the cross sit on transparent buttons, and LVGL's
        # default button text is white: right on a dark theme, invisible
        # on a light one. Use the colour the screen paints its own text.
        ink = screen.get_style_text_color(lv.PART.MAIN)
        n = len(self._selected)
        cont = lv.obj(screen)
        cont.set_width(lv.pct(100))
        cont.set_height(self.ROW_H * n)
        cont.set_style_bg_opa(lv.OPA.TRANSP, lv.PART.MAIN)
        cont.set_style_border_width(0, lv.PART.MAIN)
        cont.set_style_pad_all(0, lv.PART.MAIN)
        cont.set_scrollbar_mode(lv.SCROLLBAR_MODE.OFF)
        cont.remove_flag(lv.obj.FLAG.SCROLLABLE)
        no_scroll_chain(cont)

        for i, field_id in enumerate(self._selected):
            row = lv.obj(cont)
            row.set_size(lv.pct(100), self.ROW_H - 3)
            row.set_pos(0, i * self.ROW_H)
            row.set_style_border_width(1, lv.PART.MAIN)
            row.set_style_radius(4, lv.PART.MAIN)
            row.set_style_pad_all(0, lv.PART.MAIN)
            row.set_scrollbar_mode(lv.SCROLLBAR_MODE.OFF)
            row.remove_flag(lv.obj.FLAG.SCROLLABLE)
            row.add_flag(lv.obj.FLAG.CLICKABLE)
            no_scroll_chain(row)

            name = lv.label(row)
            name.set_text("{}  {}".format(i + 1, self._title(field_id)))
            name.set_style_text_font(FontManager.getFont(size=14), lv.PART.MAIN)
            name.align(lv.ALIGN.LEFT_MID, 6, 0)

            # A button, not just an ornament: tapping or ENTERing it moves
            # the field one place down (wrapping at the end), which is the
            # only way to reorder without a touchscreen to drag on. A drag
            # started on the row body still works as before.
            grip = lv.button(row)
            grip.set_size(26, self.ROW_H - 5)
            grip.align(lv.ALIGN.RIGHT_MID, -2, 0)
            grip.set_style_bg_opa(lv.OPA.TRANSP, lv.PART.MAIN)
            grip.set_style_shadow_width(0, lv.PART.MAIN)
            grip.add_event_cb(lambda e, f=field_id: self._nudge(f),
                              lv.EVENT.CLICKED, None)
            add_focus_border(grip)
            grip_icon = lv.label(grip)
            grip_icon.set_text(lv.SYMBOL.LIST)
            grip_icon.set_style_text_font(FontManager.getFont(size=14), lv.PART.MAIN)
            grip_icon.set_style_text_color(ink, lv.PART.MAIN)
            grip_icon.set_style_text_opa(lv.OPA._50, lv.PART.MAIN)
            grip_icon.center()

            # Its own button, so pressing it never starts a drag: LVGL
            # delivers the press to the topmost object under the finger.
            drop = lv.button(row)
            drop.set_size(28, self.ROW_H - 5)
            drop.align(lv.ALIGN.RIGHT_MID, -30, 0)
            drop.set_style_bg_opa(lv.OPA.TRANSP, lv.PART.MAIN)
            drop.set_style_shadow_width(0, lv.PART.MAIN)
            drop.add_event_cb(lambda e, f=field_id: self._deselect(f),
                              lv.EVENT.CLICKED, None)
            add_focus_border(drop)
            cross = lv.label(drop)
            cross.set_text(lv.SYMBOL.CLOSE)
            cross.set_style_text_font(FontManager.getFont(size=14), lv.PART.MAIN)
            cross.set_style_text_color(ink, lv.PART.MAIN)
            cross.center()

            row.add_event_cb(lambda e, i=i: self._drag_start(i),
                             lv.EVENT.PRESSED, None)
            row.add_event_cb(lambda e: self._drag_move(),
                             lv.EVENT.PRESSING, None)
            row.add_event_cb(lambda e: self._drag_end(),
                             lv.EVENT.RELEASED, None)
            # Without this a drag that slips off the row leaves the list
            # stuck mid-reorder.
            row.add_event_cb(lambda e: self._drag_end(),
                             lv.EVENT.PRESS_LOST, None)
            self._rows.append(row)

        if n > 1:
            hint = lv.label(screen)
            hint.set_text("Drag to reorder or tap " + lv.SYMBOL.LIST
                          + " to move down, " + lv.SYMBOL.CLOSE + " to remove.")
            hint.set_style_text_font(FontManager.getFont(size=12), lv.PART.MAIN)
            hint.set_style_text_opa(lv.OPA._50, lv.PART.MAIN)

    # --- selection ---

    def _select(self, field_id):
        if field_id in self._selected:
            return
        if len(self._selected) >= self.MAX_FIELDS:
            return
        # Appended, not sorted: the order is the user's to set.
        self._selected = list(self._selected) + [field_id]
        self._render()

    def _deselect(self, field_id):
        if len(self._selected) <= 1:
            return                     # a screen must keep at least one field
        self._selected = [f for f in self._selected if f != field_id]
        self._render()

    def _nudge(self, field_id):
        """Move a field one place down, wrapping to the top from the end.

        Repeated presses walk it to any position, so the whole ordering is
        reachable from a keypad without a drag."""
        if field_id not in self._selected or len(self._selected) < 2:
            return
        i = self._selected.index(field_id)
        self._selected.pop(i)
        self._selected.insert((i + 1) % (len(self._selected) + 1), field_id)
        self._render()

    # --- drag to reorder ---

    def _pointer_y(self):
        indev = lv.indev_active()
        if indev is None:
            return None
        point = lv.point_t()
        indev.get_point(point)
        return point.y

    def _drag_start(self, index):
        y = self._pointer_y()
        if y is None or index >= len(self._rows):
            return
        self._drag_idx = index
        self._drag_target = index
        self._drag_y0 = y
        self._rows[index].move_foreground()

    def _drag_move(self):
        if self._drag_idx is None:
            return
        y = self._pointer_y()
        if y is None:
            return
        offset = y - self._drag_y0
        self._rows[self._drag_idx].set_y(self._drag_idx * self.ROW_H + offset)
        target = self._drag_idx + int(round(offset / float(self.ROW_H)))
        target = max(0, min(len(self._rows) - 1, target))
        if target != self._drag_target:
            self._drag_target = target
            self._open_slot()

    def _open_slot(self):
        """Lay the untouched rows out around an empty slot at the target,
        so the gap shows where the field will land."""
        slot = 0
        for i, row in enumerate(self._rows):
            if i == self._drag_idx:
                continue
            if slot == self._drag_target:
                slot += 1
            row.set_y(slot * self.ROW_H)
            slot += 1

    def _drag_end(self):
        if self._drag_idx is None:
            return
        source, target = self._drag_idx, self._drag_target
        self._drag_idx = None
        if target != source:
            self._selected.insert(target, self._selected.pop(source))
        self._render()          # renumbers and snaps everything back

    # --- persistence ---

    def _entry(self):
        if self._extra or self._dict_entries:
            entry = dict(self._extra)
            entry["fields"] = list(self._selected)
            return entry
        return list(self._selected)

    def _save(self, event=None):
        screens = self.load_screens(self.prefs)
        if self._is_new:
            screens.append(self._entry())
        elif self.index < len(screens):
            screens[self.index] = self._entry()
        self.save_screens(self.prefs, screens)
        self.finish()

    def _delete_screen(self, event=None):
        screens = self.load_screens(self.prefs)
        if len(screens) > 1 and self.index < len(screens):
            screens.pop(self.index)
            self.save_screens(self.prefs, screens)
        self.finish()
