"""Native Tk viewer and editor for numbered notation and a compact staff view."""
from bisect import bisect_left, bisect_right
import copy
from dataclasses import replace
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from midi_preview import SystemMidiPlayer, build_notation_preview, pitch_number
from notation_keys import (KeySignature, LETTERS, MAJOR_FIFTHS, key_fit_label, major_fits,
                           numbered_pitch, staff_spelling)
from notation_model import edit_cell, signature_histogram, visual_attacks, visual_cells
from score_parameters import effective_score_bpms
from window_layout import size_window
from score_editor import SPECIAL_SOUNDS
from score_pitch import pitch_name
from score_timeline import audition_positions, main_coordinate, move_section_cut, section_boundaries

ROWS_PER_PAGE = 5
COLUMN_WIDTH = 56
LEFT_MARGIN = 106


def rhythm_shape(ticks):
    value = next((v for v in (256, 128, 64, 32, 16, 8, 4, 2, 1) if v <= ticks), 1)
    dots = 1 if ticks == value * 1.5 else 2 if ticks == value * 1.75 else 0
    return value, max(0, 6 - value.bit_length()), dots

ATOM_MARKS = {"mute": "0", "empty": "0", "chuanshao": "铃"}
INK = "#1b2735"
MUTED = "#53657b"
BLUE = "#126099"


class NotationWindow(tk.Toplevel):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor, self.data = editor, editor.data
        self.title("可视化谱面")
        size_window(self, 1820, 1080, 920, 670)
        self.transient(editor)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _: self.close())
        self.rowconfigure(3, weight=1)
        self.columnconfigure(0, weight=1)
        self.page = 0
        self.selection = None
        self.columns = 20
        self.resize_id = None
        self.poll_id = None
        self.player = SystemMidiPlayer()
        self.play_context = None
        self.play_tick = None
        self.last_follow = None

        controls = ttk.Frame(self, padding=(12, 10))
        controls.grid(row=0, column=0, sticky="ew")
        self.layer = self.selector(controls, "音轨", (), self.load_track, 10)
        self.mode = self.selector(controls, "视图", ("简谱", "五线谱"), self.render_page, 8)
        self.mode.current(0)
        ttk.Button(controls, text="全部音轨", command=self.show_all_tracks).pack(side="left", padx=4)
        navigation = ttk.Frame(self, padding=(12, 0, 12, 6))
        navigation.grid(row=1, column=0, sticky="ew")
        self.prev_page = ttk.Button(navigation, text="上一页", command=lambda: self.turn_page(-1))
        self.prev_page.pack(side="left", padx=(12, 4))
        self.page_label = ttk.Label(navigation, width=22, anchor="center")
        self.page_label.pack(side="left")
        self.next_page = ttk.Button(navigation, text="下一页", command=lambda: self.turn_page(1))
        self.next_page.pack(side="left", padx=4)

        self.play_button = ttk.Button(navigation, text="从选中处播放", command=self.play)
        self.play_button.pack(side="left", padx=(12, 3))
        self.stop_button = ttk.Button(navigation, text="停止", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=3)
        self.mix = tk.BooleanVar(self, True)
        ttk.Checkbutton(navigation, text="单轨视图也试听其他轨", variable=self.mix).pack(side="left", padx=8)
        self.play_status = ttk.Label(navigation, text="未选中音符时从当前页开始")
        self.play_status.pack(side="left", padx=4)
        self.play_status.configure(wraplength=190)

        key_frame = ttk.Frame(self, padding=(12, 0, 12, 8))
        key_frame.grid(row=2, column=0, sticky="ew")
        key_frame.columnconfigure(0, weight=1)
        self.key_label = ttk.Label(key_frame, wraplength=570)
        self.key_label.grid(row=0, column=0, sticky="w")
        self.key_jump = ttk.Combobox(key_frame, state="readonly", width=29)
        self.key_jump.grid(row=0, column=1, padx=(8, 0), sticky="e")
        self.key_jump.bind("<<ComboboxSelected>>", self.jump_key)

        drawing = ttk.Frame(self, padding=(12, 0))
        drawing.grid(row=3, column=0, sticky="nsew")
        drawing.columnconfigure(0, weight=1)
        drawing.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(drawing, background="#ffffff", highlightthickness=1,
                                highlightbackground="#c6d3df")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        xscroll = ttk.Scrollbar(drawing, orient="horizontal", command=self.canvas.xview)
        yscroll = ttk.Scrollbar(drawing, command=self.canvas.yview)
        xscroll.grid(row=1, column=0, sticky="ew")
        yscroll.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(xscrollcommand=xscroll.set, yscrollcommand=yscroll.set)
        self.canvas.bind("<Configure>", self.schedule_resize)
        self.canvas.bind("<MouseWheel>", lambda event: self.canvas.yview_scroll(-event.delta // 120, "units"))

        edit = ttk.Frame(self, padding=12)
        edit.grid(row=4, column=0, sticky="ew")
        self.detail = ttk.Label(edit)
        self.detail.grid(row=0, column=0, columnspan=10, sticky="w", pady=(0, 6))
        ttk.Label(edit, text="音名").grid(row=1, column=0)
        self.sound = ttk.Entry(edit, width=10)
        self.sound.grid(row=1, column=1, padx=5)
        self.minus = ttk.Button(edit, text="↓ 半音", command=lambda: self.nudge(-1))
        self.plus = ttk.Button(edit, text="↑ 半音", command=lambda: self.nudge(1))
        self.minus.grid(row=1, column=2, padx=3)
        self.plus.grid(row=1, column=3, padx=3)
        ttk.Label(edit, text="时值 H–P").grid(row=2, column=0, padx=(0, 3))
        self.length = ttk.Entry(edit, width=12)
        self.length.grid(row=2, column=1, padx=5)
        self.borrow = ttk.Combobox(edit, state="readonly", width=16,
                                   values=("与下一音补偿", "与上一音补偿"))
        self.borrow.current(0)
        self.borrow.grid(row=2, column=2, columnspan=2, padx=5, sticky="w")
        actions = ttk.Frame(edit)
        actions.grid(row=2, column=4, sticky="w", padx=(6, 0))
        self.apply_button = ttk.Button(actions, text="应用微调", command=self.apply)
        self.apply_button.pack(side="left")
        ttk.Button(actions, text="撤销", command=self.undo).pack(side="left", padx=6)
        ttk.Button(actions, text="关闭", command=self.close).pack(side="left")
        positions = ttk.Frame(edit)
        positions.grid(row=3, column=0, columnspan=8, sticky="w", pady=(5, 0))
        self.split_button = ttk.Button(positions, text="设定分段点…", command=self.set_split)
        self.split_button.pack(side="left")
        self.audition_start = ttk.Button(positions, text="设为歌单起点", command=lambda: self.set_audition("start"))
        self.audition_start.pack(side="left", padx=5)
        self.audition_end = ttk.Button(positions, text="设为歌单终点", command=lambda: self.set_audition("end"))
        self.audition_end.pack(side="left")
        self.status = ttk.Label(edit, text="视图为简化记谱，精确时值见每项下方；修改时值会由相邻项补偿。")
        self.status.grid(row=4, column=0, columnspan=10, sticky="w", pady=(6, 0))
        self.configure_layers()
        self.load_track()
        self.grab_set()

    @staticmethod
    def selector(parent, label, items, callback, width):
        ttk.Label(parent, text=label).pack(side="left", padx=(8, 4))
        box = ttk.Combobox(parent, values=items, state="readonly", width=width)
        box.pack(side="left", padx=(0, 5))
        box.bind("<<ComboboxSelected>>", lambda _: callback())
        return box

    @property
    def page_size(self):
        return ROWS_PER_PAGE * self.columns

    def close(self):
        self.destroy()
        if self.editor.winfo_exists():
            self.editor.grab_set()

    def destroy(self):
        if self.resize_id is not None:
            self.after_cancel(self.resize_id)
            self.resize_id = None
        self.stop()
        super().destroy()

    def schedule_resize(self, event):
        columns = max(8, min(26, (event.width - LEFT_MARGIN - 18) // COLUMN_WIDTH))
        if self.resize_id is not None:
            self.after_cancel(self.resize_id)
            self.resize_id = None
        if columns != self.columns:
            self.resize_id = self.after(120, self.resize, columns)

    def resize(self, columns):
        self.resize_id = None
        current = self.page * self.page_size
        self.columns = columns
        self.page = current // self.page_size
        if self.selection:
            layer, index, atom = self.selection
            tick = self.selected_tick(layer, index, atom)
            self.page = self.column_at(tick) // self.page_size
        self.render_page()
        if self.play_tick is not None:
            self.follow(self.play_tick)

    def configure_layers(self):
        count = max(len(part["scores"]) for part in self.data["musics"])
        self.layer.configure(values=["全部音轨", *[f"{i + 1}：{'主旋律' if i == 0 else '伴奏'}"
                                                    for i in range(count)]])
        self.layer.current(0)

    def show_all_tracks(self):
        self.layer.current(0)
        self.load_track()

    def section_at(self, tick):
        return max(0, min(len(self.bounds) - 2, bisect_right(self.bounds, tick) - 1))

    def load_track(self, keep=False, at_tick=None):
        self.stop()
        self.bounds = section_boundaries(self.data)
        self.marker_map = {tick: [f"分段点 {i}"] for i, tick in enumerate(self.bounds[1:-1], 1)}
        for name, tick in audition_positions(self.data, self.bounds).items():
            self.marker_map.setdefault(tick, []).append("歌单起点" if name == "start" else "歌单终点")
        count = len(self.layer["values"]) - 1
        self.track_cells = [[] for _ in range(count)]
        self.attacks = [[] for _ in range(count)]
        self.local_cells = []
        self.references = [[] for _ in range(count)]
        self.key_by_track = [[] for _ in range(count)]
        self.histograms, self.inferred = [], []
        self.visible_tracks = list(range(count)) if self.layer.current() == 0 else [self.layer.current() - 1]
        for section, (part, bpm) in enumerate(zip(self.data["musics"], effective_score_bpms(self.data))):
            origin = self.bounds[section]
            tracks = [visual_cells(line) for line in part["scores"]]
            self.local_cells.append(tracks)
            histogram = signature_histogram(part)
            self.histograms.append(histogram)
            fits = major_fits(histogram)
            fallback = KeySignature(MAJOR_FIFTHS[fits[0][0]]) if fits else KeySignature(0)
            self.inferred.append(fallback)
            for layer in range(count):
                annotated = (self.editor.notation_keys[section][layer]
                             if self.editor.notation_keys and layer < len(self.editor.notation_keys[section]) else [])
                at_start = bool(annotated and annotated[0][0] == 0)
                events = [] if at_start else [(0, fallback, False)]
                events.extend((at, key, True) for at, key in annotated)
                timeline = self.key_by_track[layer]
                for at, key, marked in events:
                    if not timeline or timeline[-1][1:] != (key, marked):
                        timeline.append((origin + at, key, marked))
                if layer >= len(tracks):
                    continue
                cells = tracks[layer]
                offset = len(self.track_cells[layer])
                self.track_cells[layer].extend(replace(cell, tick=cell.tick + origin) for cell in cells)
                self.references[layer].extend((section, i) for i in range(len(cells)))
                self.attacks[layer].extend(replace(attack, tick=attack.tick + origin, index=attack.index + offset)
                                           for attack in visual_attacks(cells, bpm))
        self.attack_ticks = [[attack.tick for attack in track] for track in self.attacks]
        self.attack_index = [{(attack.index, attack.atom): attack.tick for attack in track}
                             for track in self.attacks]
        self.key_ticks_by_track = [[at for at, _, _ in events] for events in self.key_by_track]
        self.key_entries = sorted({(at, layer, key) for layer in self.visible_tracks
                                   for at, key, marked in self.key_by_track[layer] if marked},
                                  key=lambda item: item[:2])
        self.ticks = sorted({attack.tick for layer in self.visible_tracks for attack in self.attacks[layer]}
                            | set(self.marker_map) | {at for at, _, _ in self.key_entries})
        self.key_jump.configure(values=[
            f"{'转调 · ' if at else ''}{at / 32:g} 拍 · 轨 {layer + 1} · {key.label}"
            for at, layer, key in self.key_entries])
        if self.key_entries:
            self.key_jump.current(0)
        if not keep:
            self.page = 0
            self.selection = None
        elif self.selection:
            self.page = self.column_at(self.selected_tick(*self.selection)) // self.page_size
        if at_tick is not None:
            self.page = self.column_at(at_tick) // self.page_size
        self.render_page()
        if at_tick is not None:
            self.show_tick(self.column_at(at_tick))

    def turn_page(self, delta):
        self.stop()
        self.page += delta
        self.selection = None
        self.render_page()

    def column_at(self, tick):
        return max(0, bisect_right(self.ticks, tick + 1e-7) - 1)

    def selected_tick(self, layer, index, atom):
        first = next((start for start in range(atom, -1, -1)
                      if (index, start) in self.attack_index[layer]), 0)
        return self.attack_index[layer].get((index, first), self.track_cells[layer][index].tick)

    def jump_key(self, _):
        choice = self.key_jump.current()
        if choice >= 0:
            self.stop()
            self.page = self.column_at(self.key_entries[choice][0]) // self.page_size
            self.selection = None
            self.render_page()

    def key_at(self, layer, tick):
        index = bisect_right(self.key_ticks_by_track[layer], tick) - 1
        return self.key_by_track[layer][index][1:] if index >= 0 else (self.inferred[0], False)

    def key_caption(self, key):
        if self.mode.current() == 0:
            reference = KeySignature(key.fifths)
            tonic = f"（主音 {key.degree + 1}）" if key.degree else ""
            return f"1={reference.tonic_name} · {key.label}{tonic}"
        if key.fifths:
            return f"{key.label} · {abs(key.fifths)} 个{'升' if key.fifths > 0 else '降'}号"
        return f"{key.label} · 无升降号"

    def render_page(self):
        self.canvas.delete("all")
        self.last_follow = None
        self.systems = []
        total = len(self.ticks)
        pages = max(1, (total + self.page_size - 1) // self.page_size)
        self.page = max(0, min(self.page, pages - 1))
        start = self.page * self.page_size
        ending = min(total, start + self.page_size)
        self.page_label.configure(text=f"{self.page + 1} / {pages} 页 · {total} 个起点")
        self.prev_page.configure(state="normal" if self.page else "disabled")
        self.next_page.configure(state="normal" if self.page + 1 < pages else "disabled")
        start_tick = self.ticks[start] if start < total else 0
        track = self.visible_tracks[0]
        key, marked = self.key_at(track, start_tick)
        section = self.section_at(start_tick)
        status = (f"第 {section + 1} 段 · " +
                  ("原谱调号：" + self.key_caption(key) if marked else
                   key_fit_label(self.histograms[section]) + " · " + self.key_caption(key)))
        if len(self.visible_tracks) > 1:
            status += " · 各轨调号见谱行"
        if any(at > 0 for at, _, _ in self.key_entries):
            status += " · 谱中标出转调"
        self.key_label.configure(text=status)
        if self.key_entries:
            self.key_jump.grid()
        else:
            self.key_jump.grid_remove()
        y, widest = 8, 720
        for first in range(start, ending, self.columns):
            last = min(first + self.columns, ending)
            ticks = self.ticks[first:last]
            bounds = {}
            y0 = y
            y += 36
            for layer in self.visible_tracks:
                y1 = y + self.draw_row(layer, first, last, y)
                bounds[layer] = (y, y1)
                y = y1
            marked = 0
            for column, tick in enumerate(ticks):
                if tick in self.marker_map:
                    x = self.column_x(column)
                    tags = self.marker_map[tick]
                    ink = "#245da8" if any(tag.startswith("分段点") for tag in tags) else "#167c64"
                    self.canvas.create_line(x - 20, y0 + 31, x - 20, y - 3,
                                            fill=ink, width=2, dash=(5, 3), tags="score-marker")
                    self.canvas.create_text(x - 20, y0 + 2 + 15 * (marked % 2), text=" · ".join(tags),
                                            anchor="nw", fill=ink, font=("Segoe UI", 9, "bold"),
                                            tags="score-marker")
                    marked += 1
            self.systems.append((first, last, y0, y, bounds))
            widest = max(widest, self.column_x(len(ticks) - 1) + COLUMN_WIDTH)
            y += 14
        if not total:
            self.canvas.create_text(32, 36, anchor="w", text="没有可显示的音符。", fill=MUTED)
        self.canvas.configure(scrollregion=(0, 0, widest, max(100, y)))
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)
        self.show_selection()
        self.play_button.configure(state="normal" if total and self.play_context is None else "disabled")

    @staticmethod
    def column_x(column):
        return LEFT_MARGIN + column * COLUMN_WIDTH

    def draw_row(self, layer, first, last, y):
        staff = self.mode.current() == 1
        ticks = self.ticks[first:last]
        stop = self.ticks[last] if last < len(self.ticks) else float("inf")
        track = self.attacks[layer]
        at = self.attack_ticks[layer]
        row = track[bisect_left(at, ticks[0]):bisect_left(at, stop)]
        pitches = [(attack.tick, pitch_number(name, piano_range=False)) for attack in row
                   for name in attack.group.split(".") if name and name not in SPECIAL_SOUNDS]
        bass = bool(pitches and sorted(p for _, p in pitches)[len(pitches) // 2] < 60)
        bottom_pos = 18 if bass else 30
        positions = [staff_spelling(p, self.key_at(layer, tick)[0])[0] - bottom_pos
                     for tick, p in pitches] if staff else []
        above, below = max(8, max(positions, default=8)), min(0, min(positions, default=0))
        upper, lower = max(48, above * 5 + 12), max(22, -below * 5 + 14)
        if not staff:
            cluster = max((len(attack.group.split(".")) for attack in row), default=1)
            upper, lower = 32 + max(0, cluster - 1) * 20, 24
        baseline = y + upper + 16
        key, _ = self.key_at(layer, ticks[0])
        name = "主旋律" if layer == 0 else f"伴奏 {layer}"
        self.canvas.create_text(12, y + 2, text=f"{name} · {self.key_caption(key)}",
                                anchor="nw", fill=BLUE, font=("Segoe UI", 10, "bold"))
        width = self.column_x(len(ticks) - 1) + COLUMN_WIDTH // 2
        if staff:
            for line in range(5):
                step = baseline - 10 * line
                self.canvas.create_line(12, step, width, step, fill="#8191a3")
            self.canvas.create_text(27, baseline - 21, text="𝄢" if bass else "𝄞",
                                    font=("Segoe UI Symbol", 24), fill=INK)
            self.draw_signature(43, baseline, key, bass)
        key_marks = {at: key for at, key, marked in self.key_by_track[layer] if marked}
        columns = {tick: i for i, tick in enumerate(ticks)}
        accidentals = {}
        previous = None
        for attack in row:
            cell = self.track_cells[layer][attack.index]
            center = self.column_x(columns[attack.tick])
            next_key, _ = self.key_at(layer, attack.tick)
            if next_key != previous or cell.barline and not attack.connector:
                accidentals.clear()
            if cell.barline and not attack.connector:
                self.canvas.create_line(center - 24, baseline - 40, center - 24, baseline + 4, fill=MUTED)
            self.draw_cell(layer, attack, cell, center, baseline, baseline + lower,
                           bottom_pos, next_key, staff, accidentals)
            previous = next_key
        for tick in ticks:
            if tick > 0 and tick in key_marks:
                center = self.column_x(columns[tick])
                self.canvas.create_line(center - 22, baseline - 42, center - 22, baseline + 4,
                                        fill=BLUE, width=2)
                self.canvas.create_text(center, y + 19, text=f"转调：{key_marks[tick].label}", anchor="w", fill=BLUE)
        return upper + lower + 40

    def draw_signature(self, x, baseline, key, bass):
        order = "FCGDAEB" if key.fifths > 0 else "BEADGCF"
        slots = (8, 5, 9, 6, 3, 7, 4) if key.fifths > 0 else (4, 7, 3, 6, 2, 5, 1)
        for i, letter in enumerate(order):
            alter = key.alterations[letter]
            if alter:
                self.canvas.create_text(x + i * 8, baseline - (slots[i] - (2 if bass else 0)) * 5,
                                        text=("♯" if alter > 0 else "♭") * abs(alter),
                                        font=("Segoe UI Symbol", 11), fill=BLUE)

    def draw_cell(self, layer, attack, cell, center, baseline, duration_y, bottom, key, staff, accidentals):
        index = attack.index
        base_tag = f"cell-{layer}-{index}"
        color = BLUE if self.selection and self.selection[:2] == (layer, index) else INK
        if not attack.connector:
            self.canvas.create_text(center, duration_y, text=cell.lengths or "待修复",
                                    fill=MUTED, font=("Segoe UI", 9), tags=(base_tag,))
        else:
            self.canvas.create_text(center, duration_y, text=attack.connector, fill=BLUE)
        self.canvas.tag_bind(base_tag, "<Button-1>", lambda _, i=index, track=layer: self.pick(i, 0, track))
        if not attack.group:
            if staff and cell.token.kind == "rest":
                glyph = {128: "𝄻", 64: "𝄼", 32: "𝄽", 16: "𝄾", 8: "𝄿", 4: "𝅀"}.get(
                    rhythm_shape(cell.token.ticks)[0], "休")
                self.canvas.create_text(center, baseline - 20, text=glyph,
                                        fill=color, font=("Segoe UI Symbol", 15), tags=(base_tag,))
            else:
                self.canvas.create_text(center, baseline - 8,
                                        text="0" if cell.token.kind == "rest" else "!",
                                        fill=color, font=("Segoe UI", 12), tags=(base_tag,))
            return
        group, atom = attack.group, attack.atom
        for level, name in enumerate(group.split(".")):
            tag = f"atom-{layer}-{index}-{atom}"
            selected = self.selection == (layer, index, atom)
            ink = BLUE if selected else INK
            near_x = center + ((level % 2) * 9 if staff else 0)
            if name in SPECIAL_SOUNDS:
                self.canvas.create_text(near_x, baseline - 18 - level * 14,
                                        text=ATOM_MARKS[name], fill=ink, tags=(tag,))
            elif staff:
                pitch = pitch_number(name, piano_range=False)
                position, accidental = staff_spelling(pitch, key)
                pos = position - bottom
                pitch_y = baseline - 5 * pos
                for ledger in (range(-2, pos - 1, -2) if pos < 0 else range(10, pos + 1, 2)):
                    ly = baseline - 5 * ledger
                    self.canvas.create_line(near_x - 10, ly, near_x + 10, ly, fill=MUTED)
                value, flags, dots = rhythm_shape(cell.token.ticks / attack.group_count)
                fill = "#ffffff" if value >= 64 else ink
                native = key.alterations[LETTERS[position % 7]]
                labels = {0: "♮", 1: "♯", 2: "♯♯", -1: "♭", -2: "♭♭"}
                written = {sym: val for val, sym in labels.items()}.get(accidental, native)
                accidental = labels.get(written, "") if accidentals.get(position, native) != written else ""
                accidentals[position] = written
                self.canvas.create_oval(near_x - 6, pitch_y - 4, near_x + 6, pitch_y + 4,
                                        outline=ink, fill=fill, width=2, tags=(tag,))
                if accidental:
                    self.canvas.create_text(near_x - 16, pitch_y, text=accidental, fill=ink, tags=(tag,))
                for dot in range(dots):
                    dx = near_x + 9 + 4 * dot
                    self.canvas.create_oval(dx, pitch_y - 2, dx + 2, pitch_y,
                                            fill=ink, outline=ink, tags=(tag,))
                if value < 128:
                    stem_y = pitch_y - 23 if pos < 4 else pitch_y + 23
                    side = near_x + (5 if pos < 4 else -5)
                    self.canvas.create_line(side, pitch_y, side, stem_y, fill=ink, tags=(tag,))
                    for f in range(flags):
                        dy = f * (4 if pos < 4 else -4)
                        self.canvas.create_line(side, stem_y + dy, side + 7,
                                                stem_y + dy + (6 if pos < 4 else -6),
                                                fill=ink, tags=(tag,))
            else:
                degree, octave = numbered_pitch(pitch_number(name, piano_range=False), KeySignature(key.fifths))
                ny = baseline - 12 - level * 20
                self.canvas.create_text(near_x, ny, text=degree, fill=ink,
                                        font=("Segoe UI", 13, "bold"), tags=(tag,))
                if octave:
                    dy = -12 if octave > 0 else 12
                    for dot in range(abs(octave)):
                        px = near_x + (dot - (abs(octave) - 1) / 2) * 4
                        self.canvas.create_oval(px - 1, ny + dy - 1, px + 1, ny + dy + 1,
                                                fill=ink, outline=ink, tags=(tag,))
                for line in range(rhythm_shape(cell.token.ticks / attack.group_count)[1]):
                    py = ny + 9 + line * 2
                    self.canvas.create_line(near_x - 5, py, near_x + 5, py, fill=ink, tags=(tag,))
            self.canvas.tag_bind(tag, "<Button-1>", lambda _, i=index, a=atom, track=layer: self.pick(i, a, track))
            atom += 1

    def pick(self, index, atom, layer=None):
        self.stop()
        self.selection = self.visible_tracks[0] if layer is None else layer, index, atom
        xview, yview = self.canvas.xview()[0], self.canvas.yview()[0]
        self.render_page()
        self.canvas.xview_moveto(xview)
        self.canvas.yview_moveto(yview)

    def show_selection(self):
        cell, atom = (None, None) if self.selection is None else (
            self.track_cells[self.selection[0]][self.selection[1]], self.selection[2])
        valid = cell is not None and cell.token.kind != "invalid"
        name = cell.atoms[atom][0] if valid and cell.atoms and atom < len(cell.atoms) else ""
        for entry, value, enabled in ((self.sound, name, bool(name)),
                                      (self.length, cell.lengths if valid else "", valid)):
            entry.configure(state="normal")
            entry.delete(0, "end")
            entry.insert(0, value)
            if not enabled:
                entry.configure(state="disabled")
        for button in (self.minus, self.plus):
            button.configure(state="normal" if name and name not in SPECIAL_SOUNDS else "disabled")
        self.apply_button.configure(state="normal" if valid else "disabled")
        if cell:
            layer, index, atom = self.selection
            tick = self.selected_tick(layer, index, atom)
            section, local = self.references[layer][index]
            self.detail.configure(text=f"第 {section + 1} 段 · 轨 {layer + 1} · 第 {local + 1} 项 · "
                                       f"{tick / 32:g} 拍 · {cell.token.text[:64]}")
        else:
            self.detail.configure(text="点击简谱数字或五线谱音头进行编辑。")

        self.split_button.configure(state="normal" if valid and len(self.bounds) > 2 else "disabled")
        self.audition_start.configure(state="normal" if valid else "disabled")
        self.audition_end.configure(state="normal" if valid else "disabled")

    def show_tick(self, column):
        for first, last, y, _, _ in self.systems:
            if first <= column < last:
                region = list(map(float, self.canvas.cget("scrollregion").split()))
                self.canvas.yview_moveto(y / max(1, region[3]))
                break

    def set_split(self):
        if self.selection is None or len(self.bounds) <= 2:
            return
        tick = self.selected_tick(*self.selection)
        layer, index, _ = self.selection
        if tick != self.track_cells[layer][index].tick:
            messagebox.showwarning("无法设置分段点", "分段点不能切开琶音或连音。", parent=self)
            return
        cuts = range(1, len(self.bounds) - 1)
        boundary = min(cuts, key=lambda i: abs(tick - self.bounds[i]))
        if len(cuts) > 1:
            boundary = simpledialog.askinteger(
                "设定分段点", f"将哪一个分段点移到所选音符起点（{tick / 32:g} 拍）？",
                initialvalue=boundary, minvalue=1, maxvalue=len(cuts), parent=self)
            if boundary is None:
                return
        try:
            result, keys = move_section_cut(self.data, boundary, round(tick), self.editor.notation_keys,
                                            self.editor.measure_ticks)
        except ValueError as exc:
            messagebox.showwarning("无法设置分段点", str(exc), parent=self)
            return
        if result is not self.data:
            self.editor.replace_sections(result, keys)
            self.load_track(at_tick=tick)
        self.status.configure(text=f"分段点 {boundary}：{tick / 32:g} 拍。")

    def set_audition(self, name):
        if self.selection is None:
            return
        layer, index, _ = self.selection
        cell = self.track_cells[layer][index]
        tick = cell.tick + (cell.token.ticks if name == "end" else 0)
        try:
            coordinate = main_coordinate(self.data, self.bounds, tick, end=name == "end")
        except ValueError as exc:
            messagebox.showwarning("无法设置歌单片段", str(exc), parent=self)
            return
        pair = self.data.get("audition")
        known = audition_positions(self.data, self.bounds)
        other = "end" if name == "start" else "start"
        updated = {**(pair if isinstance(pair, dict) else {}), name: coordinate}
        updated[other] = list(updated[other]) if other in known else coordinate.copy()
        moved_other = updated["start"] > updated["end"]
        if moved_other:
            updated[other] = coordinate.copy()
        if updated != self.data.get("audition"):
            self.editor.history.append(copy.deepcopy(self.data))
            self.data["audition"] = updated
            self.editor.refresh()
            self.load_track(keep=True)
        label = "起点" if name == "start" else "终点"
        self.status.configure(text=f"已设置歌单{label}。" +
                              ("另一端已一并移动，以保持起止顺序。" if moved_other else ""))

    def nudge(self, delta):
        try:
            name = pitch_name(pitch_number(self.sound.get().strip(), piano_range=False) + delta,
                              piano_range=False)
        except ValueError as exc:
            messagebox.showerror("无法调整音高", str(exc), parent=self)
            return
        self.sound.delete(0, "end")
        self.sound.insert(0, name)
        self.apply()

    def apply(self):
        if self.selection is None:
            return
        self.stop()
        layer, index, atom = self.selection
        snapshot = copy.deepcopy(self.data)
        try:
            section, local = self.references[layer][index]
            applied = edit_cell(self.data, section, layer, self.local_cells[section][layer],
                                local, atom, self.sound.get().strip(), self.length.get().strip().upper(),
                                borrow=1 if self.borrow.current() == 0 else -1)
        except ValueError as exc:
            messagebox.showerror("无法应用微调", str(exc), parent=self)
            return
        if applied:
            self.editor.history.append(snapshot)
            self.editor.refresh()
            self.load_track(keep=True)
            self.status.configure(text="已应用微调，可撤销。")

    def play(self):
        self.stop()
        if not self.ticks:
            return
        all_tracks = self.layer.current() == 0 or self.mix.get()
        track = self.visible_tracks[0]
        start = self.ticks[min(self.page * self.page_size, len(self.ticks) - 1)]
        selected = None
        if self.selection:
            track, index, atom = self.selection
            start = self.selected_tick(track, index, atom)
            selected = track
        try:
            section = self.section_at(start)
            midi, timeline = build_notation_preview(self.data, section, start - self.bounds[section],
                                                    selected_layer=selected if all_tracks else track,
                                                    all_tracks=all_tracks)
            self.player.play(midi)
        except (ValueError, RuntimeError, OSError) as exc:
            self.play_status.configure(text=f"无法试听：{exc}")
            return
        self.play_context = timeline, [span[1] for span in timeline], track
        self.last_follow = None
        self.play_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.poll()

    def poll(self):
        self.poll_id = None
        if self.play_context is None:
            return
        try:
            if not self.player.is_playing():
                self.stop()
                self.play_status.configure(text="播放完毕")
                return
        except (ValueError, RuntimeError, OSError) as exc:
            self.stop()
            self.play_status.configure(text=f"试听失败：{exc}")
            return
        timeline, starts, _ = self.play_context
        at = self.player.position_seconds
        section, start, end, local, factor = timeline[max(0, bisect_right(starts, at) - 1)]
        tick = local + (min(at, end) - start) / factor
        absolute_tick = self.bounds[section] + tick
        self.follow(absolute_tick)
        self.play_status.configure(text=f"第 {section + 1} 段 · {absolute_tick / 32:.2f} 拍")
        self.poll_id = self.after(45, self.poll)

    def follow(self, tick):
        self.play_tick = tick
        if not self.ticks:
            return
        column = self.column_at(tick)
        page = column // self.page_size
        if page != self.page:
            self.page = page
            self.render_page()
        self.canvas.delete("playhead")
        for first, last, y, end, bounds in self.systems:
            if first <= column < last:
                x = self.column_x(column - first)
                if column + 1 < last:
                    delta = self.ticks[column + 1] - self.ticks[column]
                    x += COLUMN_WIDTH * max(0, min(1, (tick - self.ticks[column]) / delta))
                self.canvas.create_line(x, y, x, end - 2, fill="#d44d1b", width=2, tags="playhead")
                active = []
                for layer, (top, bottom) in bounds.items():
                    i = bisect_right(self.attack_ticks[layer], tick + 1e-7) - 1
                    if i >= 0:
                        attack = self.attacks[layer][i]
                        cell = self.track_cells[layer][attack.index]
                        if cell.tick + cell.token.ticks > tick:
                            col = bisect_left(self.ticks, attack.tick)
                            if first <= col < last:
                                active.append((col, top, bottom))
                for col, top, bottom in active:
                    cx = self.column_x(col - first)
                    item = self.canvas.create_rectangle(cx - 23, top + 19, cx + 23, bottom - 4,
                                                        outline="", fill="#fff0bb", tags="playhead")
                    self.canvas.tag_lower(item)
                if self.last_follow != (page, first):
                    self.last_follow = (page, first)
                    region = list(map(float, self.canvas.cget("scrollregion").split()))
                    view_top = self.canvas.canvasy(0)
                    height = self.canvas.winfo_height()
                    top, bottom = y, end
                    if bottom - top > height and self.play_context is not None:
                        top, bottom = bounds.get(self.play_context[2], bounds[self.visible_tracks[0]])
                    if top < view_top or bottom > view_top + height:
                        self.canvas.yview_moveto(top / max(1, region[3]))
                if x < self.canvas.canvasx(0) or x > self.canvas.canvasx(0) + self.canvas.winfo_width() - 15:
                    region = list(map(float, self.canvas.cget("scrollregion").split()))
                    self.canvas.xview_moveto(max(0, x - 60) / max(1, region[2]))
                break

    def stop(self):
        if self.poll_id is not None:
            self.after_cancel(self.poll_id)
            self.poll_id = None
        was_playing = self.play_context is not None
        self.play_context = None
        self.play_tick = None
        self.last_follow = None
        self.player.stop()
        self.canvas.delete("playhead")
        self.stop_button.configure(state="disabled")
        self.play_button.configure(state="normal" if getattr(self, "ticks", ()) else "disabled")
        if was_playing:
            self.play_status.configure(text="已停止播放")

    def undo(self):
        self.stop()
        at = self.ticks[self.page * self.page_size] if self.ticks else 0
        self.editor.undo()
        self.load_track(at_tick=at)
        self.status.configure(text="已刷新谱面。")
