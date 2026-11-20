"""Load an existing score while leaving musical syntax to the score editor."""
import json
import tkinter as tk
from tkinter import simpledialog, ttk

from audition import audition_sections
from score_editor import inspect_score
from window_layout import size_window
from score_parameters import is_arena_score, positive_number


def read_score_document(text):
    data = json.loads(text)
    sections = data.get("musics") if isinstance(data, dict) else None
    if not isinstance(sections, list) or not sections:
        raise ValueError("谱面 JSON 的 musics 必须至少包含一段")
    arena = is_arena_score(data)
    for i, section in enumerate(sections, 1):
        if not isinstance(section, dict):
            raise ValueError(f"第 {i} 段格式无效")
        scores = section.get("scores")
        if not isinstance(scores, list) or not scores or any(not isinstance(s, str) for s in scores):
            raise ValueError(f"第 {i} 段的 scores 必须是非空字符串数组")
        for key in ("bpm", "baseBeats"):
            if arena and key == "bpm" and key not in section:
                continue
            if not positive_number(section.get(key)):
                raise ValueError(f"第 {i} 段的 {key} 必须为有限正数")
    if not arena:
        data.setdefault("baseBpm", sections[0]["bpm"])
    ensure_audition(data)
    return data


def ensure_audition(data):
    # A malformed score can be imported before it has valid excerpt coordinates.
    if "audition" not in data and not any(inspect_score(s["scores"][0])[1] for s in data["musics"]):
        count = len(audition_sections(data)[0])
        data["audition"] = {"start": [0, 0], "end": [0, min(16, count - 1)]}


class JsonSyntaxDialog(simpledialog.Dialog):
    def __init__(self, parent, source):
        self.source = source
        self.result = None
        super().__init__(parent, title="修复 JSON 格式")

    def body(self, master):
        size_window(self, 1220, 800, 740, 500)
        self.after_idle(lambda: master.pack_configure(fill="both", expand=True))
        self.error = tk.StringVar(self)
        ttk.Label(master, textvariable=self.error).pack(anchor="w")
        frame = ttk.Frame(master)
        frame.pack(fill="both", expand=True)
        self.editor = tk.Text(frame, width=96, height=22, wrap="char")
        scroll = ttk.Scrollbar(frame, command=self.editor.yview)
        scroll.pack(side="right", fill="y")
        self.editor.pack(side="left", fill="both", expand=True)
        self.editor.configure(yscrollcommand=scroll.set)
        self.editor.insert("1.0", self.source)
        self.editor.tag_configure("error", foreground="#850000", background="#ffc2c2")
        self.validate()
        return self.editor

    def validate(self):
        self.editor.tag_remove("error", "1.0", "end")
        try:
            self.result = read_score_document(self.editor.get("1.0", "end-1c"))
        except json.JSONDecodeError as exc:
            self.error.set(f"第 {exc.lineno} 行，第 {exc.colno} 列：{exc.msg}")
            offset = min(exc.pos, max(0, len(exc.doc) - 1))
            at = f"1.0+{offset}c"
            self.editor.tag_add("error", at, f"{at}+1c")
            self.editor.mark_set("insert", at)
            self.editor.see(at)
            return False
        except ValueError as exc:
            self.error.set(str(exc))
            return False
        return True
