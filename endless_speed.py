"""Endless-stage speed projection for three-section normal scores."""
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
import tkinter as tk
from tkinter import ttk

from score_parameters import positive_number, rolling_speed_hint
from window_layout import size_window


MAX_ENDLESS_STAGE = 10_000


@dataclass(frozen=True)
class EndlessSpeed:
    score_section: int
    bpm: int
    multiplier: Fraction
    blocks_per_second: Fraction


def endless_speed(sections, stage):
    """Evaluate each transition exactly, feeding the truncated BPM back in."""
    if len(sections) != 3:
        raise ValueError("无尽速度公式仅适用于普通三段谱面。")
    if type(stage) is not int or not 1 <= stage <= MAX_ENDLESS_STAGE:
        raise ValueError(f"请输入 1–{MAX_ENDLESS_STAGE} 的无尽段编号。")
    initial = sections[2].get("bpm")
    bases = [part.get("baseBeats") for part in sections]
    if not positive_number(initial) or not all(positive_number(base) for base in bases):
        raise ValueError("第三段 BPM 和各段基础拍长必须为有限正数。")
    lengths = [Fraction(str(base)) for base in bases]
    bpm = Fraction(str(initial))
    length = lengths[2]
    minimum = Fraction(104, 100)
    for i in range(stage):
        speed = bpm / length
        intercept = Fraction(140 if i < 6 else 143, 100)
        multiplier = max(minimum, intercept - speed / 1000)
        index = i % 3
        length = lengths[index]
        bpm = int(speed * multiplier * length)
    return EndlessSpeed(index + 1, bpm, multiplier, bpm / (60 * length))


class EndlessSpeedDialog(tk.Toplevel):
    def __init__(self, master, data):
        super().__init__(master)
        self.sections = data["musics"]
        self.title("乐谱无尽段速度计算")
        self.transient(master)
        size_window(self, 690, 360, 550, 300)
        self.columnconfigure(0, weight=1)
        frame = ttk.Frame(self, padding=18)
        frame.grid(sticky="ew")
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="前三段为主谱，第 4 段起为无尽，按第 1、2、3 段轮换。").grid(
            row=0, column=0, columnspan=3, sticky="w")
        bases = " / ".join(str(part["baseBeats"]) for part in self.sections)
        ttk.Label(frame, text=f'第三段 BPM：{self.sections[2]["bpm"]}；'
                             f'各段基础拍长：{bases} 拍').grid(
                                 row=1, column=0, columnspan=3, sticky="w", pady=(4, 10))
        ttk.Label(frame, text="无尽第几段（从 1 开始）").grid(row=2, column=0, sticky="w")
        self.stage = ttk.Spinbox(frame, from_=1, to=MAX_ENDLESS_STAGE, width=11, command=self.calculate)
        self.stage.grid(row=2, column=1, sticky="w", padx=8)
        self.stage.set("1")
        ttk.Button(frame, text="计算", command=self.calculate).grid(row=2, column=2, sticky="e")
        self.result = ttk.Label(frame, wraplength=505, justify="left")
        self.result.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(12, 4))
        self.hint = ttk.Label(frame)
        self.hint.grid(row=4, column=0, columnspan=3, sticky="w")
        ttk.Button(frame, text="关闭", command=self.destroy).grid(row=5, column=2, pady=(10, 0))
        self.bind("<Return>", lambda _: self.calculate())
        self.bind("<Escape>", lambda _: self.destroy())
        self.calculate()
        self.stage.focus_set()
        self.stage.selection_range(0, "end")
        self.grab_set()

    def calculate(self):
        try:
            stage = int(self.stage.get())
        except ValueError:
            stage = 0
        try:
            result = endless_speed(self.sections, stage)
        except ValueError as exc:
            self.result.configure(text=str(exc))
            self.hint.configure(text="")
            return
        speed = result.blocks_per_second
        speed_value = Decimal(speed.numerator) / Decimal(speed.denominator)
        speed_text = format(speed_value, ".3f" if speed < 10**8 else ".6E")
        bpm_text = str(result.bpm) if result.bpm < 10**18 else format(Decimal(result.bpm), ".8E")
        base = self.sections[result.score_section - 1]["baseBeats"]
        self.result.configure(text=(
            f"无尽第 {stage} 段（整首第 {stage + 3} 段，循环谱面第 {result.score_section} 段）\n"
            f"BPM：{bpm_text}　基础拍长：{base} 拍\n"
            f"滚动速度：{speed_text} 块/s　　本次倍率：{float(result.multiplier):.4f}"))
        msg, color = rolling_speed_hint(speed)
        self.hint.configure(text=msg, foreground=color)
