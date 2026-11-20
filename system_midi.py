"""Play MIDI events directly through the Windows MIDI output API."""
import ctypes
import sys
import threading
import time


class WinMmOutput:
    def __init__(self):
        self.handle = ctypes.c_void_p()
        self.library = None

    def _check(self, code):
        if code:
            text = ctypes.create_unicode_buffer(512)
            self.library.midiOutGetErrorTextW(code, text, len(text))
            raise RuntimeError(f"系统 MIDI 输出失败（{code}）：{text.value}")

    def open(self):
        if sys.platform != "win32":
            raise RuntimeError("系统 MIDI 试听目前仅支持 Windows")
        self.library = ctypes.WinDLL("winmm")
        signatures = {
            "midiOutOpen": [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint,
                            ctypes.c_size_t, ctypes.c_size_t, ctypes.c_uint],
            "midiOutShortMsg": [ctypes.c_void_p, ctypes.c_uint32],
            "midiOutReset": [ctypes.c_void_p],
            "midiOutClose": [ctypes.c_void_p],
            "midiOutGetErrorTextW": [ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_uint],
        }
        for name, arguments in signatures.items():
            function = getattr(self.library, name)
            function.argtypes = arguments
            function.restype = ctypes.c_uint
        # MIDI_MAPPER selects the system's default MIDI output synthesizer.
        self._check(self.library.midiOutOpen(ctypes.byref(self.handle), 0xFFFFFFFF, 0, 0, 0))

    def send(self, data):
        packed = sum(byte << (8 * index) for index, byte in enumerate(data))
        self._check(self.library.midiOutShortMsg(self.handle, packed))

    def close(self):
        if self.handle.value:
            try:
                self._check(self.library.midiOutReset(self.handle))
            finally:
                self._check(self.library.midiOutClose(self.handle))
                self.handle = ctypes.c_void_p()


def midi_schedule(midi):
    """Merge all tracks and convert delta times to absolute seconds."""
    events, duration = [], 0.0
    for message in midi:
        duration += message.time
        if not message.is_meta:
            if message.type == "sysex":
                raise ValueError("片段试听不支持 MIDI 系统专用消息")
            events.append((duration, message.bytes()))
    return events, duration


class SystemMidiPlayer:
    """An interruptible, absolute-time scheduler independent of player status."""
    RELEASE_SECONDS = 0.35

    def __init__(self, output_factory=WinMmOutput, clock=time.monotonic):
        self._output_factory = output_factory
        self._clock = clock
        self._cancel = threading.Event()
        self._thread = None
        self._error = None
        self._started_at = None
        self.duration_seconds = 0.0
        self.events_sent = 0
        self.total_events = 0
        self._completed = False

    def play(self, midi):
        self.stop()
        events, duration = midi_schedule(midi)
        if not events or duration <= 0:
            raise ValueError("片段没有可播放的 MIDI 事件或时长")
        output = self._output_factory()
        try:
            output.open()
        except Exception:
            output.close()
            raise
        self._cancel.clear()
        self._error = None
        self._completed = False
        self._started_at = None
        self.duration_seconds = duration
        self.events_sent = 0
        self.total_events = len(events)
        self._thread = threading.Thread(target=self._run, args=(output, events, duration),
                                        name="PianoTilesMidiPreview", daemon=True)
        try:
            self._thread.start()
        except Exception:
            output.close()
            self._thread = None
            raise

    def _wait_until(self, deadline):
        while not self._cancel.is_set():
            remaining = deadline - self._clock()
            if remaining <= 0:
                return True
            if self._cancel.wait(remaining):
                return False
        return False

    def _run(self, output, events, duration):
        self._started_at = self._clock()
        try:
            for when, data in events:
                if not self._wait_until(self._started_at + when):
                    return
                output.send(data)
                self.events_sent += 1
            # Include the complete MIDI duration even if no audible event
            # occurs in its final rest, then allow the final note to decay.
            if not self._wait_until(self._started_at + duration):
                return
            if not self._wait_until(self._clock() + self.RELEASE_SECONDS):
                return
            self._completed = True
        except Exception as exc:
            self._error = RuntimeError(f"片段 MIDI 试听失败：{exc}")
        finally:
            try:
                output.close()
            except Exception as exc:
                if self._error is None:
                    self._error = RuntimeError(f"关闭系统 MIDI 输出失败：{exc}")

    @property
    def position_seconds(self):
        if self._completed:
            return self.duration_seconds
        if self._started_at is None:
            return 0.0
        return max(0.0, min(self.duration_seconds, self._clock() - self._started_at))

    def is_playing(self):
        if self._error is not None:
            raise self._error
        return bool(self._thread and self._thread.is_alive())

    def stop(self):
        self._cancel.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            if self._thread.is_alive():
                raise RuntimeError("系统 MIDI 输出未能及时停止，请稍后重试")
            self._thread = None
        self._error = None
