"""Confirmation dialog for unwrapping selected special blocks."""
import tkinter as tk
from tkinter import messagebox, ttk

from window_layout import size_window


class DissolveGroupsDialog(tk.Toplevel):
    def __init__(self, parent, groups):
        super().__init__(parent)
        self.title("解散音符组")
        self.transient(parent)
        size_window(self, 1200, 690, 740, 420)
        self.groups = groups
        self.result = None
        self.included = set(range(len(groups)))
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        ttk.Label(self, text="默认勾选全部音符组；点击状态栏或按空格可切换。"
                            "只移除方块外壳，保留组内音符、~ 和 @。").grid(
                                row=0, column=0, columnspan=2, sticky="w", padx=12, pady=8)
        columns = ("state", "section", "layer", "group")
        self.table = ttk.Treeview(self, columns=columns, show="headings", selectmode="browse")
        for name, title, width in zip(columns, ("本次处理", "段", "轨", "原音符组"), (90, 55, 55, 650)):
            self.table.heading(name, text=title)
            self.table.column(name, width=width, stretch=name == "group")
        for i, group in enumerate(groups):
            self.table.insert("", "end", iid=str(i),
                              values=("☑ 解散", group.section + 1, group.layer + 1, group.source))
        self.table.grid(row=1, column=0, sticky="nsew", padx=(12, 0))
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.table.yview)
        scroll.grid(row=1, column=1, sticky="ns", padx=(0, 12))
        self.table.configure(yscrollcommand=scroll.set)
        self.table.bind("<Button-1>", self.click_row)
        self.table.bind("<space>", self.toggle_selection)

        self.preview = tk.Text(self, height=4, wrap="char", state="disabled")
        self.preview.grid(row=2, column=0, columnspan=2, sticky="ew", padx=12, pady=6)
        self.table.bind("<<TreeviewSelect>>", self.preview_selection)
        actions = ttk.Frame(self, padding=12)
        actions.grid(row=3, column=0, columnspan=2, sticky="ew")
        ttk.Button(actions, text="全选", command=lambda: self.select_all(True)).pack(side="left")
        ttk.Button(actions, text="全部忽略", command=lambda: self.select_all(False)).pack(
            side="left", padx=6)
        self.count = tk.StringVar(self)
        ttk.Label(actions, textvariable=self.count).pack(side="left", padx=6)
        self.confirm_button = ttk.Button(actions, command=self.confirm)
        self.confirm_button.pack(side="right")
        ttk.Button(actions, text="取消", command=self.destroy).pack(side="right", padx=6)
        self.update_actions()
        self.table.selection_set("0")
        self.table.focus("0")
        self.table.focus_set()
        self.bind("<Escape>", lambda _: self.destroy())

    def click_row(self, event):
        if self.table.identify_region(event.x, event.y) == "cell" and self.table.identify_column(event.x) == "#1":
            row = self.table.identify_row(event.y)
            if row:
                self.table.selection_set(row)
                self.table.focus(row)
                self.table.focus_set()
                self.toggle(int(row))
                return "break"

    def toggle_selection(self, _):
        rows = self.table.selection()
        if rows:
            self.toggle(int(rows[0]))
        return "break"

    def toggle(self, index):
        if index in self.included:
            self.included.remove(index)
        else:
            self.included.add(index)
        self.update_row(index)
        self.update_actions()

    def select_all(self, include):
        self.included = set(range(len(self.groups))) if include else set()
        for i in range(len(self.groups)):
            self.update_row(i)
        self.update_actions()

    def update_row(self, index):
        self.table.set(str(index), "state", "☑ 解散" if index in self.included else "☐ 忽略")

    def update_actions(self):
        count = len(self.included)
        self.count.set(f"已选 {count}/{len(self.groups)} 组")
        self.confirm_button.configure(text=f"确认解散所选 {count} 组",
                                      state="normal" if count else "disabled")

    def preview_selection(self, _):
        rows = self.table.selection()
        if rows:
            group = self.groups[int(rows[0])]
            self.preview.configure(state="normal")
            self.preview.delete("1.0", "end")
            self.preview.insert("1.0", f"原音符组：{group.source}\n解散后：{group.replacement}")
            self.preview.configure(state="disabled")

    def confirm(self):
        if self.included and messagebox.askyesno(
                "再次确认", f"确定解散所选的 {len(self.included)} 个音符组吗？"
                            "忽略的组会保留，应用后可撤销。", parent=self):
            self.result = [self.groups[i] for i in sorted(self.included)]
            self.destroy()
