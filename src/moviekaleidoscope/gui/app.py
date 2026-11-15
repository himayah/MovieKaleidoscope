"""The window (tkinter). A thin layer: the form holds a GuiSettings, `model` turns it into a Config
(with the same validation as the command line) and `runner` runs the job in a background thread.

The window never blocks: the job reports through a queue that `_poll` drains every 100 ms.
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from .. import __version__
from ..config import CUT_MODES, X264_PRESETS, validate_for_render, validate_for_run
from ..effects import registry
from ..errors import ConfigError, MkError
from ..util.progress import STAGE_UNITS
from . import model
from .model import GuiSettings
from .runner import JobRunner, generate_job, replay_job

CUT_LABELS = {
    "auto": "自動(音楽があれば拍に合わせる)",
    "beat": "拍に合わせる",
    "fixed": "固定の長さ",
    "random": "ランダムな長さ",
}
# Where each stage sits on the overall progress bar (start %, share %).
STAGE_RANGES = {"入力検証": (0, 5), "BPM 解析": (5, 5), "計画": (10, 2), "セグメント生成": (12, 80), "連結": (92, 8)}
VIDEO_TYPES = [("動画", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.mpg *.mpeg *.wmv *.flv"), ("すべてのファイル", "*.*")]
AUDIO_TYPES = [("音声", "*.wav *.mp3 *.flac *.ogg *.m4a *.aac *.aiff"), ("すべてのファイル", "*.*")]


# Japanese-capable UI fonts, best first. Tk's own default (Segoe UI, Helvetica, ...) usually falls
# back to something for Japanese, but not everywhere (and a monospace default rarely does).
PREFERRED_FONTS = ("Yu Gothic UI", "Meiryo UI", "Meiryo", "Hiragino Sans", "Noto Sans CJK JP", "Noto Sans JP",
                   "IPAexGothic", "IPAPGothic", "TakaoPGothic", "MS UI Gothic", "MS Gothic")
NAMED_FONTS = ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont", "TkSmallCaptionFont",
               "TkIconFont", "TkTooltipFont")


def apply_japanese_font(root: tk.Tk) -> str | None:
    """Switch Tk's named fonts to the first installed Japanese-capable family; returns it (or None)."""
    from tkinter import font as tkfont

    available = set(tkfont.families(root))
    family = next((f for f in PREFERRED_FONTS if f in available), None)
    if family:
        for name in NAMED_FONTS:
            tkfont.nametofont(name, root=root).configure(family=family)
    return family


def open_path(path: Path) -> None:
    """Open a file or folder with the system's default application."""
    if os.name == "nt":
        os.startfile(str(path))                       # noqa: S606 - the user asked to open their own output
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class App:
    def __init__(self, root: tk.Tk, settings: GuiSettings | None = None, *, runner: JobRunner | None = None,
                 store_dir: Path | None = None, remember: bool = True) -> None:
        self.root = root
        self.runner = runner or JobRunner()
        self.store_dir = store_dir
        self.remember = remember
        self._base: dict[str, Any] = {}
        self._result_path: Path | None = None
        self._form: list[tk.Widget] = []              # widgets disabled while a job runs
        self._poll_id: str | None = None
        self._closing = False

        apply_japanese_font(root)
        root.title(f"MovieKaleidoscope {__version__}")
        root.minsize(780, 700)
        self._make_variables()
        self._build_menu()
        self._build_tabs()
        self._build_run_area()
        self.write_settings(settings if settings is not None else (model.recall(store_dir) if remember else GuiSettings()))
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._poll_id = root.after(100, self._poll)

    # ------------------------------------------------------------------------------------------------------------
    # form <-> settings
    # ------------------------------------------------------------------------------------------------------------
    TEXT_FIELDS = ("output", "duration", "audio_file", "audio_start", "bpm", "cut_fixed", "cut_random_min",
                   "cut_random_max", "beats", "width", "height", "fps", "crf", "seed", "jobs", "ffmpeg")

    def _make_variables(self) -> None:
        self.text = {name: tk.StringVar(master=self.root) for name in self.TEXT_FIELDS}
        self.recursive = tk.BooleanVar(master=self.root)
        self.overwrite = tk.BooleanVar(master=self.root)
        self.cut_mode = tk.StringVar(master=self.root)            # shows the label; mapped back by `mode_of`
        self.preset = tk.StringVar(master=self.root)
        self.effect_on = {n: tk.BooleanVar(master=self.root, value=True) for n in registry.names()}

    def cut_mode_widget(self) -> ttk.Combobox:
        return self._cut_combo

    @staticmethod
    def mode_of(label: str) -> str:
        return next((m for m, text in CUT_LABELS.items() if text == label), "auto")

    def read_settings(self) -> GuiSettings:
        t = {k: v.get() for k, v in self.text.items()}
        return GuiSettings(
            inputs=list(self.input_list.get(0, "end")), recursive=self.recursive.get(), overwrite=self.overwrite.get(),
            audio_start=t["audio_start"], cut_mode=self.mode_of(self.cut_mode.get()), preset=self.preset.get(),
            effects={n: v.get() for n, v in self.effect_on.items()}, base=self._base,
            **{k: t[k] for k in self.TEXT_FIELDS if k != "audio_start"},
        )

    def write_settings(self, s: GuiSettings) -> None:
        for name in self.TEXT_FIELDS:
            self.text[name].set(getattr(s, name))
        self.recursive.set(s.recursive)
        self.overwrite.set(s.overwrite)
        self.cut_mode.set(CUT_LABELS.get(s.cut_mode, CUT_LABELS["auto"]))
        self.preset.set(s.preset)
        for n, var in self.effect_on.items():
            var.set(s.effects.get(n, True))
        self.input_list.delete(0, "end")
        for p in s.inputs:
            self.input_list.insert("end", p)
        self._base = s.base
        self._update_cut_fields()

    # ------------------------------------------------------------------------------------------------------------
    # layout
    # ------------------------------------------------------------------------------------------------------------
    def _add(self, widget: tk.Widget) -> tk.Widget:
        self._form.append(widget)
        return widget

    def _entry(self, parent, var, width=14, **kw) -> ttk.Entry:
        return self._add(ttk.Entry(parent, textvariable=var, width=width, **kw))

    def _button(self, parent, text, command, **kw) -> ttk.Button:
        return self._add(ttk.Button(parent, text=text, command=command, **kw))

    def _build_menu(self) -> None:
        bar = tk.Menu(self.root)
        file_menu = tk.Menu(bar, tearoff=False)
        file_menu.add_command(label="設定を開く…", command=self.open_settings)
        file_menu.add_command(label="設定を保存…", command=self.save_settings)
        file_menu.add_separator()
        file_menu.add_command(label="plan から動画を作り直す…", command=self.replay_plan)
        file_menu.add_separator()
        file_menu.add_command(label="終了", command=self.close)
        bar.add_cascade(label="ファイル", menu=file_menu)
        self.root.configure(menu=bar)
        self._menu = file_menu

    def _build_tabs(self) -> None:
        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill="both", expand=False, padx=8, pady=(8, 4))
        for title, build in (("入力と出力", self._tab_io), ("音楽とカット", self._tab_music), ("見た目", self._tab_look),
                             ("詳細", self._tab_advanced)):
            frame = ttk.Frame(self.tabs, padding=10)
            self.tabs.add(frame, text=title)
            build(frame)

    def _tab_io(self, f: ttk.Frame) -> None:
        box = ttk.LabelFrame(f, text="入力動画(ファイルやフォルダ。何個でも)", padding=8)
        box.pack(fill="both", expand=True)
        box.columnconfigure(0, weight=1)
        self.input_list = tk.Listbox(box, height=8, selectmode="extended", activestyle="none")
        sb = ttk.Scrollbar(box, orient="vertical", command=self.input_list.yview)
        self.input_list.configure(yscrollcommand=sb.set)
        self.input_list.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self._form.append(self.input_list)
        side = ttk.Frame(box)
        side.grid(row=0, column=2, sticky="n", padx=(8, 0))
        for text, cmd in (("ファイルを追加…", self.add_files), ("フォルダを追加…", self.add_folder),
                          ("選んだものを削除", self.remove_selected), ("すべて削除", self.clear_inputs)):
            self._button(side, text, cmd).pack(fill="x", pady=2)
        self._add(ttk.Checkbutton(box, text="フォルダの中のフォルダも探す", variable=self.recursive)).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

        out = ttk.LabelFrame(f, text="出力", padding=8)
        out.pack(fill="x", pady=(10, 0))
        out.columnconfigure(1, weight=1)
        ttk.Label(out, text="出力ファイル(.mp4)").grid(row=0, column=0, sticky="w")
        self._entry(out, self.text["output"], width=50).grid(row=0, column=1, sticky="ew", padx=6)
        self._button(out, "参照…", self.browse_output).grid(row=0, column=2)
        self._add(ttk.Checkbutton(out, text="同じ名前のファイルがあれば上書きする", variable=self.overwrite)).grid(
            row=1, column=1, sticky="w", pady=(4, 0))
        ttk.Label(out, text="長さ").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self._entry(out, self.text["duration"], width=10).grid(row=2, column=1, sticky="w", padx=6, pady=(8, 0))
        ttk.Label(out, text="秒、または 分:秒(例 90、1:30)。音楽を使うなら空欄で、音楽の長さになります",
                  foreground="gray40").grid(row=3, column=1, sticky="w", padx=6)

    def _tab_music(self, f: ttk.Frame) -> None:
        music = ttk.LabelFrame(f, text="音楽(なくても作れます)", padding=8)
        music.pack(fill="x")
        music.columnconfigure(1, weight=1)
        ttk.Label(music, text="音声ファイル").grid(row=0, column=0, sticky="w")
        self._entry(music, self.text["audio_file"], width=50).grid(row=0, column=1, sticky="ew", padx=6)
        bar = ttk.Frame(music)
        bar.grid(row=0, column=2)
        self._button(bar, "参照…", self.browse_audio).pack(side="left")
        self._button(bar, "クリア", lambda: self.text["audio_file"].set("")).pack(side="left", padx=(4, 0))
        ttk.Label(music, text="開始位置(秒)").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self._entry(music, self.text["audio_start"], width=8).grid(row=1, column=1, sticky="w", padx=6, pady=(8, 0))
        ttk.Label(music, text="BPM").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self._entry(music, self.text["bpm"], width=8).grid(row=2, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(music, text="空欄なら音楽から自動で検出します。テンポの取り違えがあれば、ここで指定してください",
                  foreground="gray40").grid(row=3, column=1, sticky="w", padx=6)

        cut = ttk.LabelFrame(f, text="カット(断片の長さ)", padding=8)
        cut.pack(fill="x", pady=(10, 0))
        ttk.Label(cut, text="方式").grid(row=0, column=0, sticky="w")
        combo = ttk.Combobox(cut, textvariable=self.cut_mode, state="readonly", width=34,
                             values=[CUT_LABELS[m] for m in CUT_MODES])
        combo.grid(row=0, column=1, columnspan=4, sticky="w", padx=6)
        combo.bind("<<ComboboxSelected>>", lambda e: self._update_cut_fields())
        self._add(combo)
        self._cut_combo = combo
        self.cut_widgets: dict[str, list[tk.Widget]] = {"fixed": [], "random": [], "beat": []}
        ttk.Label(cut, text="固定の長さ(秒)").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.cut_widgets["fixed"].append(self._entry(cut, self.text["cut_fixed"], width=8))
        self.cut_widgets["fixed"][-1].grid(row=1, column=1, sticky="w", padx=6, pady=(8, 0))
        ttk.Label(cut, text="ランダム(秒)  最小").grid(row=2, column=0, sticky="w", pady=(6, 0))
        lo = self._entry(cut, self.text["cut_random_min"], width=8)
        lo.grid(row=2, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(cut, text="最大").grid(row=2, column=2, sticky="e", pady=(6, 0))
        hi = self._entry(cut, self.text["cut_random_max"], width=8)
        hi.grid(row=2, column=3, sticky="w", padx=6, pady=(6, 0))
        self.cut_widgets["random"] += [lo, hi]
        ttk.Label(cut, text="拍の数").grid(row=3, column=0, sticky="w", pady=(6, 0))
        beats = self._entry(cut, self.text["beats"], width=12)
        beats.grid(row=3, column=1, sticky="w", padx=6, pady=(6, 0))
        self.cut_widgets["beat"].append(beats)
        ttk.Label(cut, text="例 4(4 拍ごと)、2,4(2 拍か 4 拍をランダムに)", foreground="gray40").grid(
            row=3, column=2, columnspan=3, sticky="w", padx=6, pady=(6, 0))

    def _tab_look(self, f: ttk.Frame) -> None:
        fx = ttk.LabelFrame(f, text="使うエフェクト(断片ごとに 1 つをランダムに選びます)", padding=8)
        fx.pack(fill="both", expand=True)
        for i, name in enumerate(registry.names()):
            e = registry.get(name)
            cb = self._add(ttk.Checkbutton(fx, text=f"{name}   {e.description.split('。')[0]}", variable=self.effect_on[name]))
            cb.grid(row=i % 8, column=i // 8, sticky="w", padx=(0, 16), pady=1)
        bar = ttk.Frame(fx)
        bar.grid(row=8, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self._button(bar, "すべて選ぶ", lambda: self._set_effects(True)).pack(side="left")
        self._button(bar, "すべて外す", lambda: self._set_effects(False)).pack(side="left", padx=4)

        fmt = ttk.LabelFrame(f, text="出力の形式(空欄なら最初の入力に合わせます)", padding=8)
        fmt.pack(fill="x", pady=(10, 0))
        for col, (label, key, width) in enumerate((("幅", "width", 7), ("高さ", "height", 7), ("FPS", "fps", 9), ("画質 CRF", "crf", 5))):
            ttk.Label(fmt, text=label).grid(row=0, column=col * 2, sticky="e", padx=(0 if col == 0 else 12, 4))
            self._entry(fmt, self.text[key], width=width).grid(row=0, column=col * 2 + 1, sticky="w")
        ttk.Label(fmt, text="速さ").grid(row=0, column=8, sticky="e", padx=(12, 4))
        self._add(ttk.Combobox(fmt, textvariable=self.preset, values=list(X264_PRESETS), state="readonly", width=10)).grid(
            row=0, column=9, sticky="w")
        ttk.Label(fmt, text="CRF は小さいほど高画質(18 が目安)。速さは遅いほど小さなファイルになります", foreground="gray40").grid(
            row=1, column=0, columnspan=10, sticky="w", pady=(6, 0))

    def _tab_advanced(self, f: ttk.Frame) -> None:
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="乱数のシード").grid(row=0, column=0, sticky="w")
        row = ttk.Frame(f)
        row.grid(row=0, column=1, sticky="w", padx=6)
        self._entry(row, self.text["seed"], width=12).pack(side="left")
        self._button(row, "決めておく", lambda: self.text["seed"].set(str(random.randrange(2**32)))).pack(side="left", padx=4)
        self._button(row, "空にする", lambda: self.text["seed"].set("")).pack(side="left")
        ttk.Label(f, text="空欄なら毎回違う構成になります。同じシードと設定なら同じ構成になります", foreground="gray40").grid(
            row=1, column=1, sticky="w", padx=6)
        ttk.Label(f, text="並列数").grid(row=2, column=0, sticky="w", pady=(12, 0))
        self._entry(f, self.text["jobs"], width=6).grid(row=2, column=1, sticky="w", padx=6, pady=(12, 0))
        ttk.Label(f, text="空欄なら自動(CPU の論理コア数の半分)", foreground="gray40").grid(row=3, column=1, sticky="w", padx=6)
        ttk.Label(f, text="ffmpeg の場所").grid(row=4, column=0, sticky="w", pady=(12, 0))
        self._entry(f, self.text["ffmpeg"], width=50).grid(row=4, column=1, sticky="ew", padx=6, pady=(12, 0))
        self._button(f, "参照…", self.browse_ffmpeg).grid(row=4, column=2, pady=(12, 0))
        ttk.Label(f, text="空欄なら PATH から探します", foreground="gray40").grid(row=5, column=1, sticky="w", padx=6)

    def _build_run_area(self) -> None:
        run = ttk.Frame(self.root, padding=(8, 4))
        run.pack(fill="both", expand=True)
        bar = ttk.Frame(run)
        bar.pack(fill="x")
        self.run_button = ttk.Button(bar, text="動画を作る", command=self.start)
        self.run_button.pack(side="left")
        self.plan_button = ttk.Button(bar, text="構成(plan)だけ作る", command=lambda: self.start(plan_only=True))
        self.plan_button.pack(side="left", padx=6)
        self.cancel_button = ttk.Button(bar, text="中止", command=self.cancel, state="disabled")
        self.cancel_button.pack(side="left")
        self.status = tk.StringVar(master=self.root, value="待機中")
        ttk.Label(run, textvariable=self.status).pack(fill="x", pady=(8, 2))
        self.bar = ttk.Progressbar(run, maximum=100, mode="determinate")
        self.bar.pack(fill="x")
        logf = ttk.Frame(run)
        logf.pack(fill="both", expand=True, pady=(8, 0))
        self.log = tk.Text(logf, height=9, wrap="word", state="disabled", font="TkTextFont")
        sb = ttk.Scrollbar(logf, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.log.tag_configure("warning", foreground="#b36b00")
        self.log.tag_configure("error", foreground="#c0392b")
        res = ttk.Frame(run)
        res.pack(fill="x", pady=(6, 0))
        self.folder_button = ttk.Button(res, text="出力のフォルダを開く", command=self.open_folder, state="disabled")
        self.folder_button.pack(side="left")
        self.play_button = ttk.Button(res, text="動画を再生", command=self.play, state="disabled")
        self.play_button.pack(side="left", padx=6)

    # ------------------------------------------------------------------------------------------------------------
    # small actions
    # ------------------------------------------------------------------------------------------------------------
    def _update_cut_fields(self) -> None:
        """Enable only the cut fields the chosen method uses (`auto` may become beat, so it keeps those)."""
        mode = self.mode_of(self.cut_mode.get())
        use = {"fixed": mode in ("auto", "fixed"), "random": mode == "random", "beat": mode in ("auto", "beat")}
        busy = self.runner.running
        for key, widgets in self.cut_widgets.items():
            for w in widgets:
                w.configure(state="normal" if use[key] and not busy else "disabled")

    def _set_effects(self, on: bool) -> None:
        for v in self.effect_on.values():
            v.set(on)

    def add_files(self) -> None:
        for p in filedialog.askopenfilenames(parent=self.root, title="入力動画を選ぶ", filetypes=VIDEO_TYPES) or ():
            self._add_input(p)

    def add_folder(self) -> None:
        p = filedialog.askdirectory(parent=self.root, title="動画のフォルダを選ぶ")
        if p:
            self._add_input(p)

    def _add_input(self, path: str) -> None:
        if path not in self.input_list.get(0, "end"):
            self.input_list.insert("end", path)

    def remove_selected(self) -> None:
        for i in reversed(self.input_list.curselection()):
            self.input_list.delete(i)

    def clear_inputs(self) -> None:
        self.input_list.delete(0, "end")

    def browse_output(self) -> None:
        p = filedialog.asksaveasfilename(parent=self.root, title="出力ファイル", defaultextension=".mp4",
                                         filetypes=[("MP4 動画", "*.mp4")], confirmoverwrite=False)
        if p:
            self.text["output"].set(p)

    def browse_audio(self) -> None:
        p = filedialog.askopenfilename(parent=self.root, title="音声ファイルを選ぶ", filetypes=AUDIO_TYPES)
        if p:
            self.text["audio_file"].set(p)

    def browse_ffmpeg(self) -> None:
        p = filedialog.askopenfilename(parent=self.root, title="ffmpeg の実行ファイルを選ぶ")
        if p:
            self.text["ffmpeg"].set(p)

    def open_settings(self) -> None:
        p = filedialog.askopenfilename(parent=self.root, title="設定ファイルを開く", filetypes=[("YAML", "*.yaml *.yml")])
        if not p:
            return
        try:
            self.write_settings(model.load_settings_file(Path(p)))
        except (MkError, OSError) as e:
            messagebox.showerror("設定を読めません", str(e), parent=self.root)

    def save_settings(self) -> None:
        p = filedialog.asksaveasfilename(parent=self.root, title="設定を保存", defaultextension=".yaml",
                                         filetypes=[("YAML", "*.yaml *.yml")])
        if not p:
            return
        try:
            model.save_settings_file(self.read_settings(), Path(p))
        except (MkError, OSError) as e:
            messagebox.showerror("設定を保存できません", str(e), parent=self.root)

    # ------------------------------------------------------------------------------------------------------------
    # running
    # ------------------------------------------------------------------------------------------------------------
    def _error(self, title: str, e: Exception) -> None:
        msg = str(e)
        if "既に存在します" in msg:
            msg += "\n\n(上書きするなら「出力」の「上書きする」にチェックしてください)"
        self._log(f"{title}: {e}", "error")
        messagebox.showerror(title, msg, parent=self.root)

    def start(self, plan_only: bool = False) -> None:
        if self.runner.running:
            return
        try:
            cfg = model.to_config(self.read_settings(), check_runnable=False)
            validate_for_run(cfg, plan_only=plan_only)
        except MkError as e:
            self._error("設定を確認してください", e)
            return
        if self.remember:
            model.remember(self.read_settings(), self.store_dir)
        self._begin(generate_job(cfg, plan_only=plan_only), "構成を作っています" if plan_only else "準備しています")

    def replay_plan(self) -> None:
        if self.runner.running:
            return
        p = filedialog.askopenfilename(parent=self.root, title="plan を開く", filetypes=[("plan", "*.json"), ("すべて", "*.*")])
        if not p:
            return
        try:
            cfg = model.to_config(self.read_settings(), check_runnable=False)
            validate_for_render(cfg)
        except MkError as e:
            self._error("出力先を確認してください", e)
            return
        self._begin(replay_job(Path(p), cfg), "plan から作り直しています")

    def _begin(self, job, status: str) -> None:
        self._result_path = None
        self.status.set(status)
        self.bar["value"] = 0
        self._log("-" * 40)
        self._set_busy(True)
        self.runner.start(job)

    def cancel(self) -> None:
        if self.runner.running:
            self.runner.cancel()
            self.status.set("中止しています")
            self.cancel_button.configure(state="disabled")

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for w in self._form:
            try:
                if isinstance(w, ttk.Combobox):
                    w.configure(state="disabled" if busy else "readonly")
                else:
                    w.configure(state=state)
            except tk.TclError:
                pass
        self.run_button.configure(state=state)
        self.plan_button.configure(state=state)
        self.cancel_button.configure(state="normal" if busy else "disabled")
        for item in range(self._menu.index("end") + 1):
            if self._menu.type(item) == "command":
                self._menu.entryconfigure(item, state="disabled" if busy and self._menu.entrycget(item, "label") != "終了" else "normal")
        if not busy:
            self._update_cut_fields()
        else:
            self.folder_button.configure(state="disabled")
            self.play_button.configure(state="disabled")

    def _log(self, text: str, tag: str | None = None) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag or ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def poll_now(self) -> None:
        """Handle every pending job event immediately (what the timer does every 100 ms)."""
        for kind, payload in self.runner.poll():
            self._handle(kind, payload)

    def _poll(self) -> None:
        self._poll_id = None
        try:
            self.poll_now()
        finally:                      # one bad event must not stop the window from ever updating again
            if not self._closing:
                self._poll_id = self.root.after(100, self._poll)

    def _handle(self, kind: str, payload: Any) -> None:
        if kind == "progress":
            stage, cur, tot = payload
            start, share = STAGE_RANGES.get(stage, (0, 0))
            self.bar["value"] = start + share * (cur / tot if tot else 1)
            unit = STAGE_UNITS.get(stage, "")
            self.status.set(f"{stage}  {cur}/{tot}{unit}" if tot > 1 else stage)
        elif kind == "log":
            level, text = payload
            self._log(text, "error" if level >= 40 else "warning" if level >= 30 else None)
        elif kind == "done":
            self._finished(payload)
        elif kind == "failed":
            code, message, trace = payload
            self.status.set("失敗しました")
            self._set_busy(False)
            if trace:
                self._log(trace, "error")
            self._error("失敗しました", Exception(message))
        elif kind == "cancelled":
            self.status.set("中止しました")
            self._log("中止しました", "warning")
            self._set_busy(False)

    def _finished(self, result: Any) -> None:
        self.bar["value"] = 100
        self._set_busy(False)
        if isinstance(result, tuple):                       # replay: (plan, output)
            output, plan_path, plan = result[1], None, result[0]
        else:
            output, plan_path, plan = result.output, result.plan_path, result.plan
        if output is None:
            self.status.set("構成(plan)を保存しました")
            self._log(f"plan を保存しました: {plan_path}")
            return
        self._result_path = Path(output)
        self.status.set("できました")
        self._log(f"できました: {output}(クリップ {len(plan.clips)} 個、seed {plan.seed})")
        if getattr(result, "inputs", None) and result.inputs.skipped:
            self._log(f"使えなかった入力が {len(result.inputs.skipped)} 件ありました", "warning")
        self.folder_button.configure(state="normal")
        self.play_button.configure(state="normal")

    def open_folder(self) -> None:
        if self._result_path:
            open_path(self._result_path.parent)

    def play(self) -> None:
        if self._result_path:
            open_path(self._result_path)

    def close(self) -> None:
        if self.runner.running:
            if not messagebox.askyesno("終了", "動画を作っている途中です。中止して終了しますか?", parent=self.root):
                return
            self.runner.cancel()
            self.runner.join(15)
        self._closing = True
        if self.remember:
            model.remember(self.read_settings(), self.store_dir)
        if self._poll_id is not None:
            self.root.after_cancel(self._poll_id)
        self.root.destroy()


def main(argv: list[str] | None = None) -> int:
    if os.name == "nt":
        try:                                              # sharp text on high-DPI screens
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    try:
        root = tk.Tk()
    except tk.TclError as e:
        print(f"画面を開けません: {e}", file=sys.stderr)
        return 1
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
