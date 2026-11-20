"""Edit and audition the playlist excerpt in the current score document."""
import tkinter as tk
from tkinter import ttk

from audition import audition_sections, audition_windows
from window_layout import size_window
from midi_preview import SystemMidiPlayer, build_audition_midi


class AuditionDialog(tk.Toplevel):
    def __init__(self, master, data):
        sections = audition_sections(data)
        super().__init__(master)
        self.data, self.sections = data, sections
        self.player = SystemMidiPlayer()
        self.poll = None
        self.title('歌单片段')
        self.transient(master)
        size_window(self, 870, 360, 610, 250)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.bind('<Escape>', lambda _: self.destroy())
        self.protocol('WM_DELETE_WINDOW', self.destroy)
        frame = ttk.Frame(self, padding=14)
        frame.grid(sticky='nsew')
        frame.columnconfigure(4, weight=1)
        frame.rowconfigure(4, weight=1)
        ttk.Label(frame, text='选择主旋律的起止音符，两端都包含，可跨段。\n'
                             '编号从 1 开始；特殊方块内逐项计数，和弦和休止符各算一项。',
                  justify='left').grid(row=0, column=0, columnspan=5, sticky='w')
        self.fields = {}
        section_names = tuple(str(i + 1) for i in range(len(sections)))
        for row, (name, label) in enumerate((('start', '起点'), ('end', '终点')), 1):
            section, index = data['audition'][name]
            section_var = tk.StringVar(self, str(section + 1))
            index_var = tk.StringVar(self, str(index + 1))
            detail = tk.StringVar(self)
            ttk.Label(frame, text=label + ' · 段').grid(row=row, column=0, sticky='w', pady=6)
            section_box = ttk.Combobox(frame, textvariable=section_var, values=section_names,
                                       state='readonly', width=4)
            section_box.grid(row=row, column=1, padx=6)
            ttk.Label(frame, text='音符').grid(row=row, column=2)
            note_box = ttk.Spinbox(frame, textvariable=index_var, from_=1, width=7)
            note_box.grid(row=row, column=3, padx=6)
            ttk.Label(frame, textvariable=detail, width=42, anchor='w').grid(row=row, column=4, sticky='ew')
            self.fields[name] = (section_var, index_var, note_box, detail)
            section_box.bind('<<ComboboxSelected>>', lambda _, key=name: self.section_changed(key))
            index_var.trace_add('write', self.refresh)
        self.summary = tk.StringVar(self)
        self.status = tk.StringVar(self)
        ttk.Label(frame, textvariable=self.summary, wraplength=480).grid(
            row=3, column=0, columnspan=5, sticky='w', pady=(4, 0))
        ttk.Label(frame, textvariable=self.status, wraplength=480).grid(
            row=4, column=0, columnspan=5, sticky='w', pady=(4, 0))
        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, columnspan=5, sticky='e', pady=(10, 0))
        self.play_button = ttk.Button(buttons, text='试听歌单片段', command=self.play)
        self.play_button.pack(side='left', padx=4)
        self.stop_button = ttk.Button(buttons, text='停止试听', command=self.stop, state='disabled')
        self.stop_button.pack(side='left', padx=4)
        ttk.Button(buttons, text='取消', command=self.destroy).pack(side='left', padx=4)
        self.apply_button = ttk.Button(buttons, text='应用到 JSON', command=self.apply)
        self.apply_button.pack(side='left', padx=4)
        self.refresh()
        self.grab_set()

    def section_changed(self, name):
        section, index, _, _ = self.fields[name]
        count = len(self.sections[int(section.get()) - 1])
        try:
            current = int(index.get())
        except ValueError:
            current = 1
        index.set(str(max(1, min(current, count))))

    def selection(self):
        try:
            return {name: [int(section.get()) - 1, int(index.get()) - 1]
                    for name, (section, index, _, _) in self.fields.items()}
        except ValueError as exc:
            raise ValueError('请输入整数音符编号') from exc

    def refresh(self, *_):
        self.stop()
        for section, index, note_box, detail in self.fields.values():
            tokens = self.sections[int(section.get()) - 1]
            note_box.configure(to=len(tokens))
            try:
                number = int(index.get())
            except ValueError:
                number = 0
            token = tokens[number - 1].text if 1 <= number <= len(tokens) else ''
            if len(token) > 28:
                token = token[:27] + '…'
            detail.set(f'共 {len(tokens)} 项' + (f' · {token}' if token else ''))
        try:
            selection = self.selection()
            windows = audition_windows(self.data, selection, self.sections)
            seconds = sum((last - first) * 60 / bpm / 32 for _, first, last, bpm in windows)
            self.summary.set(f'JSON：{selection["start"]} → {selection["end"]}   ·   约 {seconds:.2f} 秒')
            state = 'normal'
        except ValueError as exc:
            self.summary.set(str(exc))
            state = 'disabled'
        self.play_button.configure(state=state)
        self.apply_button.configure(state=state)

    def apply(self):
        selection = self.selection()
        audition_windows(self.data, selection, self.sections)
        self.data['audition'] = selection
        self.destroy()

    def play(self):
        self.stop()
        try:
            midi, duration = build_audition_midi(self.data, self.selection())
            self.player.play(midi)
        except (ValueError, RuntimeError, OSError, OverflowError) as exc:
            self.status.set(f'无法试听：{exc}')
            return
        self.status.set(f'正在试听主旋律和伴奏，约 {duration:.2f} 秒')
        self.stop_button.configure(state='normal')
        self.poll = self.after(150, self.check_playback)

    def check_playback(self):
        self.poll = None
        try:
            if self.player.is_playing():
                self.poll = self.after(150, self.check_playback)
                return
            result = '试听结束'
        except (RuntimeError, OSError) as exc:
            result = f'试听失败：{exc}'
        self.stop()
        self.status.set(result)

    def stop(self):
        if self.poll is not None:
            self.after_cancel(self.poll)
            self.poll = None
        self.player.stop()
        self.stop_button.configure(state='disabled')
        self.status.set('')

    def destroy(self):
        try:
            self.stop()
        finally:
            super().destroy()
