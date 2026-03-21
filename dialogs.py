"""Simple modal dialog widgets."""

import tkinter as tk

from constants import BG, BG_CARD, FG, FG_DIM, GREEN


class _InputDialog(tk.Toplevel):
    def __init__(self, parent, title, prompt, initial=""):
        super().__init__(parent)
        self.title(title)
        self.configure(bg=BG)
        self.resizable(False, False)
        self.grab_set()
        self.result = None

        tk.Label(self, text=prompt, bg=BG, fg=FG).pack(padx=16, pady=(12, 4))
        self._entry = tk.Entry(self, bg=BG_CARD, fg=FG, insertbackground=FG,
                               relief=tk.FLAT, width=36, font=("Consolas", 10))
        self._entry.pack(padx=16, pady=4)
        self._entry.insert(0, initial)
        self._entry.select_range(0, tk.END)
        self._entry.focus()

        btns = tk.Frame(self, bg=BG)
        btns.pack(pady=8)
        tk.Button(btns, text="OK",     command=self._ok,
                  bg=BG_CARD, fg=GREEN, relief=tk.FLAT,
                  padx=10).pack(side=tk.LEFT, padx=4)
        tk.Button(btns, text="Cancel", command=self.destroy,
                  bg=BG_CARD, fg=FG_DIM, relief=tk.FLAT,
                  padx=10).pack(side=tk.LEFT, padx=4)
        self._entry.bind("<Return>", lambda _: self._ok())
        self._entry.bind("<Escape>", lambda _: self.destroy())

        # centre over parent
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width()  - self.winfo_width())  // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")
        self.wait_window()

    def _ok(self):
        self.result = self._entry.get().strip()
        self.destroy()
