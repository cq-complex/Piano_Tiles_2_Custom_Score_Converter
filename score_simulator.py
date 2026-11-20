"""Scrollable four-lane simulator with optional synchronized MIDI playback."""
from bisect import bisect_left, bisect_right
import tkinter as tk
from tkinter import ttk

from midi_preview import SystemMidiPlayer, build_notation_preview
from simulator_model import COLORS, model_score
from window_layout import size_window


class ScoreSimulator(tk.Toplevel):
    def __init__(self, master, data):
        sections, blocks, warnings = model_score(data)
        super().__init__(master)
        self.data, self.sections, self.blocks = data, sections, blocks
        self.starts = [block.start for block in blocks]
        self.section_starts = [section.start for section in sections]
        self.total = sections[-1].end if sections else 0
        self.position = 0
        self.player = SystemMidiPlayer()
        self.play_id = None
        self.timeline = []
        self.time_starts = []
        self.playing = False
        self.title("钢琴块2 · 谱面模拟器")
        size_window(self, 900, 900, 600, 580)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda _: self.destroy())
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        self.unit = 88
        self.hit_line = 0
        header = ttk.Frame(self, padding=(12, 8))
        header.grid(row=0, column=0, sticky="ew")
        self.play_button = ttk.Button(header, text="从当前位置试听", command=self.play)
        self.play_button.pack(side="left")
        self.stop_button = ttk.Button(header, text="停止", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=6)
        ttk.Button(header, text="回到开头", command=lambda: self.seek(0)).pack(side="left", padx=6)
        ttk.Label(header, text="跳转谱段").pack(side="left", padx=(12, 4))
        self.section_box = ttk.Combobox(header, width=8, state="readonly",
                                        values=[str(i + 1) for i in range(len(sections))])
        self.section_box.pack(side="left")
        self.section_box.bind("<<ComboboxSelected>>", self.jump)
        ttk.Button(header, text="关闭", command=self.destroy).pack(side="right")

        legend = ttk.Frame(self, padding=(12, 2))
        legend.grid(row=1, column=0, sticky="ew")
        for color, label in (("black", "黑块 / 双黑 / 狂戳"), ("hold", "长条"),
                             ("slide", "滑块"), ("side", "伴奏"), ("burst", "爆裂"), ("warning", "异常")):
            ttk.Label(legend, text="■", foreground=COLORS[color]).pack(side="left")
            ttk.Label(legend, text=label).pack(side="left", padx=(3, 12))

        self.info = ttk.Label(self, padding=(12, 4), anchor="w")
        self.info.grid(row=2, column=0, sticky="ew")
        area = ttk.Frame(self, padding=(12, 0, 12, 4))
        area.grid(row=3, column=0, sticky="nsew")
        area.rowconfigure(0, weight=1)
        area.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(area, background="#eef3fb", highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scroll = ttk.Scrollbar(area, orient="vertical", command=self.scroll_by)
        self.scroll.grid(row=0, column=1, sticky="ns")
        self.canvas.bind("<Configure>", lambda _: self.render())
        self.canvas.bind("<MouseWheel>", self.wheel)
        self.canvas.bind("<Button-1>", self.select)
        self.detail = ttk.Label(self, padding=(12, 6), wraplength=540,
                                text="点选方块查看谱面内容。可滚动或跳转谱段。")
        self.detail.grid(row=4, column=0, sticky="ew")
        if warnings:
            ttk.Label(self, text=" ".join(warnings), wraplength=540, foreground="#955121",
                      padding=(12, 4)).grid(row=5, column=0, sticky="ew")
        self.selected = None

    def jump(self, _=None):
        index = self.section_box.current()
        if index >= 0:
            self.seek(self.sections[index].start)

    def seek(self, position):
        self.stop()
        self.position = min(max(0, position), self.total)
        self.selected = None
        self.render()

    def wheel(self, event):
        self.seek(self.position + event.delta / 120)
        return "break"

    def scroll_by(self, command, *args):
        if command == "moveto":
            self.seek(float(args[0]) * (self.total + self.view_span()))
        elif command == "scroll":
            self.seek(self.position + int(args[0]) * (self.view_span() if args[1] == "pages" else 1))

    def view_span(self):
        return self.hit_line / self.unit

    def current_section(self):
        return max(0, min(len(self.sections) - 1, bisect_right(self.section_starts, self.position) - 1))

    def render(self):
        c = self.canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w <= 2 or h <= 2 or not self.sections:
            return
        self.left = max(5, (w - min(600, w - 16)) / 2)
        self.lane = (w - 2 * self.left) / 4
        self.unit = self.lane * 2
        self.hit_line = h - min(130, h * 0.22)
        for lane in range(4):
            x = self.left + self.lane * lane
            c.create_rectangle(x, 0, x + self.lane, h, fill="#ffffff" if lane % 2 == 0 else "#f5f8fd",
                               outline="#c6d0e1")
        low = max(0, self.position - (h - self.hit_line) / self.unit)
        high = self.position + self.view_span()
        index = max(0, bisect_right(self.starts, low) - 1)
        end = bisect_left(self.starts, high)
        for i in range(index, end):
            self.draw_block(self.blocks[i], i)
        for i in range(bisect_left(self.section_starts, low), bisect_right(self.section_starts, high)):
            y = self.y(self.sections[i].start)
            c.create_line(self.left, y, w - self.left, y, fill="#6785a6", dash=(5, 3))
            c.create_text(self.left + 5, y - 4, text=f"第 {i + 1} 段",
                          anchor="sw", fill="#344c70", font=("Segoe UI", 9, "bold"))
        c.create_line(self.left, self.hit_line, w - self.left, self.hit_line, fill="#f29e27", width=3)
        i = self.current_section()
        section = self.sections[i]
        self.section_box.current(i)
        speed = section.bpm * 32 / (60 * section.minimum)
        self.info.configure(text=f"第 {i + 1} 段  ·  BPM {section.bpm:g}  ·  {speed:.2f} 块/s"
                                 f"  ·  段内 {(self.position - section.start) * section.minimum / 32:.2f} 拍")
        span = self.view_span()
        extent = max(1e-9, self.total + span)
        self.scroll.set(self.position / extent, (self.position + span) / extent)

    def y(self, position):
        return self.hit_line - (position - self.position) * self.unit

    def x(self, lane):
        return self.left + self.lane * (lane + 0.5)

    def draw_block(self, block, index):
        c = self.canvas
        start, end = self.y(block.start), self.y(block.start + block.length)
        top, bottom = end + 1, max(end + 5, start - 1)
        kind = block.kind
        tag = f"tile-{index}"
        color = COLORS[("slide" if kind in ("7", "8") else "hold" if kind in ("hold", "6", "9")
                        else "burst" if kind in ("head", "burst") else "black")]
        if block.warning:
            color = COLORS["warning"]
        outline = "#f29e27" if self.selected == index else ""
        if block.path:
            points = [coordinate for lane, at in block.path
                      for coordinate in (self.x(lane), self.y(block.start + at * block.length))]
            c.create_line(*points, fill=color, width=max(12, self.lane * .36),
                          capstyle="round", joinstyle="round", tags=tag)
            for lane, at in (block.path[0], block.path[-1]):
                x, y = self.x(lane), self.y(block.start + at * block.length)
                r = self.lane * .18
                c.create_oval(x-r, y-r, x+r, y+r, fill=color, outline=outline, width=3, tags=tag)
        elif kind == "head":
            left, right = self.x(1) - self.lane * .43, self.x(2) + self.lane * .43
            middle = (top + bottom) / 2
            inset = self.lane * .37
            c.create_polygon(left, middle, left + inset, top, right - inset, top, right, middle,
                             right - inset, bottom, left + inset, bottom,
                             fill=color, outline=outline, width=3, tags=tag)
        elif kind == "3":
            c.create_rectangle(self.x(1) - self.lane * .48, top, self.x(2) + self.lane * .48, bottom,
                               fill=color, outline=outline, width=3, tags=tag)
            c.create_text((self.x(1) + self.x(2)) / 2, top + min(30, (bottom-top) * .32),
                          text=str(block.count), fill="white", font=("Segoe UI", 16, "bold"), tags=tag)
        else:
            half = self.lane * (.28 if kind in ("hold", "6", "9") else .46)
            for lane in block.lanes:
                c.create_rectangle(self.x(lane) - half, top, self.x(lane) + half, bottom,
                                   fill=color, outline=outline, width=3, tags=tag)
            if kind == "9":
                side = (2, 3) if block.entry < 2 else (1, 0)
                for j, (offset, length) in enumerate(block.sides):
                    x = self.x(side[j % 2])
                    y0, y1 = self.y(block.start + offset), self.y(block.start + offset + length)
                    c.create_rectangle(x - self.lane * .34, y1 + 2, x + self.lane * .34, y0 - 2,
                                       fill=color if block.warning else COLORS["side"], outline="", tags=tag)
        if block.warning:
            lane = max(block.lanes)
            x = self.x(lane) + self.lane * .38
            y = min(bottom + 8, max(12, top + 10))
            if top < self.canvas.winfo_height() and bottom >= 0:
                c.create_polygon(x, y - 9, x - 9, y + 7, x + 9, y + 7,
                                 fill=COLORS["warning"], outline="#795715", tags=tag)
                c.create_text(x, y + 1, text="!", fill="#513a10", font=("Segoe UI", 8, "bold"), tags=tag)
        if self.playing and block.start <= self.position < block.start + block.length:
            c.create_rectangle(self.left + 1, self.hit_line - 2, self.left + 6, self.hit_line + 2,
                               fill="#f29e27", outline="")

    def select(self, _):
        item = self.canvas.find_withtag("current")
        for tag in self.canvas.gettags(item[0]) if item else ():
            if tag.startswith("tile-"):
                self.selected = int(tag[5:])
                block = self.blocks[self.selected]
                self.detail.configure(text=f"第 {block.section + 1} 段 · {block.tick / 32:g} 拍"
                                           f" · 第 {block.entry + 1} 列 → 第 {block.exit + 1} 列：{block.token}"
                                           + (f"\n⚠ {block.warning}" if block.warning else ""))
                self.render()
                return

    def play(self):
        if self.playing or self.position >= self.total or not self.sections:
            return
        i = self.current_section()
        section = self.sections[i]
        try:
            midi, self.timeline = build_notation_preview(
                self.data, i, (self.position - section.start) * section.minimum)
            self.player.play(midi)
        except (ValueError, RuntimeError, OSError, OverflowError) as exc:
            self.detail.configure(text=f"无法试听：{exc}")
            return
        self.time_starts = [span[1] for span in self.timeline]
        self.playing = True
        self.play_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.poll()

    def poll(self):
        self.play_id = None
        try:
            if not self.player.is_playing():
                self.stop()
                self.detail.configure(text="试听完毕。")
                return
        except (ValueError, RuntimeError, OSError) as exc:
            self.stop()
            self.detail.configure(text=f"试听失败：{exc}")
            return
        at = self.player.position_seconds
        index, start, end, tick, seconds_per_tick = self.timeline[max(0, bisect_right(self.time_starts, at) - 1)]
        section = self.sections[index]
        self.position = min(section.end, section.start + (tick + (min(at, end)-start) / seconds_per_tick) / section.minimum)
        self.render()
        self.play_id = self.after(33, self.poll)

    def stop(self):
        if self.play_id is not None:
            self.after_cancel(self.play_id)
            self.play_id = None
        try:
            self.player.stop()
        except (RuntimeError, OSError) as exc:
            self.detail.configure(text=f"停止试听失败：{exc}")
        self.playing = False
        self.play_button.configure(state="normal")
        self.stop_button.configure(state="disabled")

    def destroy(self):
        self.stop()
        super().destroy()
