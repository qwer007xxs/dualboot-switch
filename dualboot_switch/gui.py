"""Tkinter GUI. Runs unchanged on Windows and Linux."""

from __future__ import annotations

import platform
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import List, Optional

from .backends import available_backends, inspect_system, pick_backend
from .core import (
    BootEntry,
    BootSwitchError,
    is_elevated,
    reboot_now,
    relaunch_elevated,
)

APP_TITLE = "Dual Boot Switch"

# Palette — deliberately flat and neutral so it does not look out of place on
# either a Windows 11 or a GNOME/KDE desktop.
BG = "#1e1f26"
CARD = "#2a2c36"
CARD_SEL = "#3a4a6b"
FG = "#e8eaf0"
MUTED = "#9aa0b0"
ACCENT = "#4f8cff"
WARN_BG = "#4a3a1e"
WARN_FG = "#ffd479"


class EntryCard(tk.Frame):
    """One clickable boot target."""

    def __init__(self, master, entry: BootEntry, on_select):
        super().__init__(master, bg=CARD, cursor="hand2", padx=14, pady=10)
        self.entry = entry
        self.on_select = on_select
        self.selected = False

        badges = []
        if entry.is_current:
            badges.append("running now")
        if entry.is_default:
            badges.append("default")
        subtitle = " · ".join(badges) or entry.os_family

        self.icon = tk.Label(
            self, text=entry.icon, bg=CARD, fg=FG, font=("Segoe UI Emoji", 20)
        )
        self.title = tk.Label(
            self, text=entry.label, bg=CARD, fg=FG,
            font=("Segoe UI", 11, "bold"), anchor="w", justify="left",
        )
        self.sub = tk.Label(
            self, text=subtitle, bg=CARD, fg=MUTED,
            font=("Segoe UI", 9), anchor="w", justify="left",
        )

        self.icon.grid(row=0, column=0, rowspan=2, padx=(0, 12))
        self.title.grid(row=0, column=1, sticky="w")
        self.sub.grid(row=1, column=1, sticky="w")
        self.grid_columnconfigure(1, weight=1)

        for widget in (self, self.icon, self.title, self.sub):
            widget.bind("<Button-1>", self._clicked)

    def _clicked(self, _event=None):
        self.on_select(self)

    def set_selected(self, value: bool):
        self.selected = value
        colour = CARD_SEL if value else CARD
        for widget in (self, self.icon, self.title, self.sub):
            widget.configure(bg=colour)


class App(tk.Tk):
    def __init__(self, preferred_backend: Optional[str] = None):
        super().__init__()
        self.preferred_backend = preferred_backend
        self.selected_card: Optional[EntryCard] = None
        self.cards: List[EntryCard] = []
        self.backend = None

        self.title(APP_TITLE)
        self.configure(bg=BG)
        self.geometry("520x600")
        self.minsize(440, 460)

        self._build()
        self.after(80, self.refresh)

    # ---------------------------------------------------------------- layout

    def _build(self):
        header = tk.Frame(self, bg=BG, padx=20, pady=16)
        header.pack(fill="x")

        tk.Label(
            header, text=APP_TITLE, bg=BG, fg=FG, font=("Segoe UI", 16, "bold")
        ).pack(anchor="w")
        self.subtitle = tk.Label(
            header, text="Detecting…", bg=BG, fg=MUTED,
            font=("Segoe UI", 9), anchor="w", justify="left",
        )
        self.subtitle.pack(anchor="w", pady=(2, 0))

        # Elevation / warning banner (packed only when needed).
        self.banner = tk.Frame(self, bg=WARN_BG, padx=16, pady=10)
        self.banner_label = tk.Label(
            self.banner, text="", bg=WARN_BG, fg=WARN_FG,
            font=("Segoe UI", 9), anchor="w", justify="left", wraplength=380,
        )
        self.banner_label.pack(side="left", fill="x", expand=True)
        self.banner_button = tk.Button(
            self.banner, text="Restart elevated", command=self.elevate,
            bg=WARN_FG, fg="#1e1f26", relief="flat", padx=10, cursor="hand2",
        )

        # Scrollable list of entries.
        body = tk.Frame(self, bg=BG, padx=16)
        body.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(body, bg=BG, highlightthickness=0)
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        self.list_frame = tk.Frame(self.canvas, bg=BG)
        self.list_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self._window = self.canvas.create_window(
            (0, 0), window=self.list_frame, anchor="nw"
        )
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfig(self._window, width=e.width),
        )
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)
        self.canvas.bind_all("<Button-4>", self._on_wheel)
        self.canvas.bind_all("<Button-5>", self._on_wheel)

        # Footer.
        footer = tk.Frame(self, bg=BG, padx=20, pady=16)
        footer.pack(fill="x")

        self.status = tk.Label(
            footer, text="", bg=BG, fg=MUTED, font=("Segoe UI", 9),
            anchor="w", justify="left", wraplength=460,
        )
        self.status.pack(fill="x", pady=(0, 10))

        buttons = tk.Frame(footer, bg=BG)
        buttons.pack(fill="x")

        self.refresh_btn = tk.Button(
            buttons, text="Refresh", command=self.refresh,
            bg=CARD, fg=FG, relief="flat", padx=14, pady=8, cursor="hand2",
        )
        self.refresh_btn.pack(side="left")

        self.reboot_btn = tk.Button(
            buttons, text="Reboot into selected", command=self.confirm_reboot,
            bg=ACCENT, fg="white", relief="flat", padx=16, pady=8,
            cursor="hand2", state="disabled",
        )
        self.reboot_btn.pack(side="right")

    def _on_wheel(self, event):
        delta = 0
        if event.num == 4:
            delta = -1
        elif event.num == 5:
            delta = 1
        elif event.delta:
            delta = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(delta, "units")

    # ------------------------------------------------------------- behaviour

    def show_banner(self, text: str, with_button: bool):
        self.banner_label.configure(text=text)
        if with_button:
            self.banner_button.pack(side="right", padx=(10, 0))
        else:
            self.banner_button.pack_forget()
        self.banner.pack(fill="x", before=self.canvas.master)

    def refresh(self):
        self.status.configure(text="Scanning boot configuration…")
        self.update_idletasks()

        for card in self.cards:
            card.destroy()
        self.cards.clear()
        self.selected_card = None
        self.reboot_btn.configure(state="disabled")
        self.banner.pack_forget()

        info = inspect_system(self.preferred_backend)
        backends = ", ".join(b.name for b in available_backends()) or "none"
        self.subtitle.configure(
            text=(
                f"{info.os_name} · firmware: {info.firmware.upper()} · "
                f"using: {info.backend_name}\navailable backends: {backends}"
            )
        )

        try:
            self.backend = pick_backend(self.preferred_backend)
        except BootSwitchError:
            self.backend = None

        if not is_elevated():
            self.show_banner(
                "Not running with elevated rights. You can browse entries, but "
                "setting the next boot target needs "
                + ("Administrator" if platform.system() == "Windows" else "root")
                + ".",
                with_button=True,
            )
        elif info.notes:
            self.show_banner(" ".join(info.notes), with_button=False)

        if not info.entries:
            note = " ".join(info.notes) or "No boot entries found."
            self.status.configure(text=note)
            return

        for entry in info.entries:
            card = EntryCard(self.list_frame, entry, self.select)
            card.pack(fill="x", pady=4)
            self.cards.append(card)

        self.status.configure(
            text=f"Found {len(info.entries)} boot entries. "
                 "Selecting one sets a ONE-TIME boot target; your normal "
                 "default is left untouched."
        )

    def select(self, card: EntryCard):
        for other in self.cards:
            other.set_selected(other is card)
        self.selected_card = card
        self.reboot_btn.configure(
            state="normal" if is_elevated() else "disabled",
            text=f"Reboot into {card.entry.label}"[:34],
        )
        if not is_elevated():
            self.status.configure(
                text="Selected. Restart the app with elevated rights to apply."
            )

    def elevate(self):
        if relaunch_elevated(sys.argv[1:]):
            self.destroy()
        else:
            messagebox.showerror(
                APP_TITLE,
                "Could not relaunch with elevated rights.\n\n"
                + (
                    "Right-click the app and choose 'Run as administrator'."
                    if platform.system() == "Windows"
                    else "Install pkexec (polkit), or run: sudo dualboot-switch"
                ),
            )

    def confirm_reboot(self):
        if not self.selected_card or not self.backend:
            return
        entry = self.selected_card.entry
        if not messagebox.askyesno(
            APP_TITLE,
            f"Set the next boot to:\n\n    {entry.label}\n\n"
            "and restart the computer now?\n\n"
            "This affects the next boot only. Save your work first.",
            icon="warning",
        ):
            return

        try:
            self.backend.set_next_boot(entry)
        except BootSwitchError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return

        self.status.configure(text=f"Next boot set to {entry.label}. Rebooting…")
        self.update_idletasks()
        threading.Thread(target=self._do_reboot, daemon=True).start()

    def _do_reboot(self):
        try:
            reboot_now()
        except BootSwitchError as exc:
            self.after(0, lambda: messagebox.showerror(
                APP_TITLE,
                f"The next boot target was set, but the reboot command failed:\n\n"
                f"{exc}\n\nRestart manually to boot into your selection.",
            ))


def main(preferred_backend: Optional[str] = None) -> int:
    try:
        app = App(preferred_backend)
    except tk.TclError as exc:
        print(f"Cannot open a window: {exc}", file=sys.stderr)
        print("Use the command line instead: dualboot-switch --list", file=sys.stderr)
        return 1
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
