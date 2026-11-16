"""The window itself, driven programmatically. Needs a display (Xvfb on Linux CI); skipped without one."""

from __future__ import annotations

import gc
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import pytest

from moviekaleidoscope.gui import app as appmod
from moviekaleidoscope.gui import model
from moviekaleidoscope.gui.app import CUT_LABELS, App
from moviekaleidoscope.gui.model import GuiSettings

pytestmark = pytest.mark.integration

# Absolute on every OS ("/" or "C:\"). A bare "/a.mp4" is drive-relative on Windows: a settings file
# resolves it against its own drive, so it comes back as "C:\a.mp4".
ABS = Path(Path.cwd().anchor)
A_MP4, O_MP4 = str(ABS / "a.mp4"), str(ABS / "o.mp4")


def new_tk(capsys) -> tk.Tk:
    # Output capture off while Tcl/Tk starts: on Windows, with pytest's fd capture in place, about
    # half the runs failed to create a second Tk ("Can't find a usable tk.tcl", "invalid command
    # name tcl_findLibrary"); never with -s, never outside pytest.
    with capsys.disabled():
        return tk.Tk()


@pytest.fixture
def root(capsys):
    try:
        r = new_tk(capsys)
    except tk.TclError as e:
        pytest.skip(f"no display: {e}")
    r.withdraw()
    yield r
    try:
        r.destroy()
    except tk.TclError:
        pass
    # Free this window's Tk objects here, on the main thread. Left to the cyclic GC, they can be
    # collected on a later test's job thread, where each Tcl call waits ~1 s for a main loop and
    # fails ("main thread is not in main loop"), and freeing the interpreter there aborts Python
    # ("Tcl_AsyncDelete: async handler deleted by the wrong thread"). Seen on Windows.
    del r
    gc.collect()


@pytest.fixture
def app(root, tmp_path):
    return App(root, remember=False, store_dir=tmp_path / "store")


@pytest.fixture
def dialogs(monkeypatch):
    """Record message boxes instead of showing them; file dialogs answer from `answers`."""
    class Box:
        errors: list = []
        answers: dict = {}
        ask_yes = True

    box = Box()
    box.errors = []
    box.answers = {}
    monkeypatch.setattr(messagebox, "showerror", lambda title, msg, **k: box.errors.append((title, msg)))
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: box.ask_yes)
    for name in ("askopenfilenames", "askopenfilename", "asksaveasfilename", "askdirectory"):
        monkeypatch.setattr(filedialog, name, lambda *a, _n=name, **k: box.answers.get(_n, ""))
    return box


MAX_EVENTS = 2000


def process_events(root: tk.Misc) -> None:
    """What `update()` does, but bounded.

    `update()` returns only once no event is pending. On macOS CI (Aqua Tk 8.6.16) native events
    kept arriving after a few windows had come and gone (more than 2000 per call, with no Tcl
    timer pending but the app's own poll), so it never returned and the job hung for hours.
    `mainloop()` in the real app has no such "until idle" step."""
    n = 0
    while n < MAX_EVENTS and root.tk.dooneevent(tk._tkinter.DONT_WAIT):
        n += 1


def pump(app: App, until, timeout=90.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        process_events(app.root)
        app.poll_now()           # not via the 100 ms timer: Tk timers follow the wall clock, which can jump (WSL2)
        if until():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out; status={app.status.get()!r}")


def idle(app: App) -> bool:
    return not app.runner.running and str(app.run_button.cget("state")) == "normal"


def fill(app: App, dummy, tmp_path, **over) -> Path:
    out = tmp_path / "out" / "result.mp4"
    s = GuiSettings(inputs=[str(dummy.base), str(dummy.small43)], output=str(out), duration="3", cut_mode="fixed",
                    cut_fixed="1", crf="30", preset="ultrafast", seed="5", jobs="2",
                    effects={n: n == "hue" for n in model.registry.names()})
    for k, v in over.items():
        setattr(s, k, v)
    app.write_settings(s)
    return out


# ---- the form ---------------------------------------------------------------------------------------------------
def test_form_round_trips_every_field(app):
    s = GuiSettings(inputs=["/a.mp4", "/b dir"], recursive=True, output="/o.mp4", overwrite=True, duration="1:30",
                    audio_file="/m.wav", audio_start="2", bpm="128", cut_mode="random", cut_fixed="3",
                    cut_random_min="0.5", cut_random_max="2", beats="2,4", width="1280", height="720", fps="29.97",
                    crf="20", preset="slow", seed="42", jobs="3", ffmpeg="/x/ffmpeg",
                    effects={**{n: True for n in model.registry.names()}, "reverse": False}, base={"extensions": ["mp4"]})
    app.write_settings(s)
    assert app.read_settings() == s
    assert app.cut_mode.get() == CUT_LABELS["random"]            # the form shows the Japanese label


def test_defaults_match_the_model(app):
    assert app.read_settings() == GuiSettings()


@pytest.mark.parametrize("mode,fixed,random,beats", [
    ("auto", True, False, True), ("beat", False, False, True), ("fixed", True, False, False), ("random", False, True, False),
])
def test_only_the_fields_a_cut_mode_uses_are_enabled(app, mode, fixed, random, beats):
    app.cut_mode.set(CUT_LABELS[mode])
    app._update_cut_fields()
    state = lambda key: str(app.cut_widgets[key][0].cget("state")) == "normal"   # noqa: E731
    assert (state("fixed"), state("random"), state("beat")) == (fixed, random, beats)


def test_input_list_buttons(app, dialogs):
    dialogs.answers["askopenfilenames"] = ("/x/a.mp4", "/x/b.mp4")
    app.add_files()
    dialogs.answers["askopenfilenames"] = ("/x/b.mp4", "/x/c.mp4")              # b again: no duplicate
    app.add_files()
    dialogs.answers["askdirectory"] = "/x/folder"
    app.add_folder()
    assert list(app.input_list.get(0, "end")) == ["/x/a.mp4", "/x/b.mp4", "/x/c.mp4", "/x/folder"]
    app.input_list.selection_set(1)
    app.input_list.selection_set(3)
    app.remove_selected()
    assert list(app.input_list.get(0, "end")) == ["/x/a.mp4", "/x/c.mp4"]
    app.clear_inputs()
    assert app.input_list.size() == 0
    dialogs.answers["askopenfilenames"] = ()                                    # cancelling a dialog changes nothing
    app.add_files()
    assert app.input_list.size() == 0


def test_browse_buttons_fill_their_fields(app, dialogs):
    dialogs.answers.update(asksaveasfilename="/o/out.mp4", askopenfilename="/m/bgm.wav")
    app.browse_output()
    app.browse_audio()
    app.browse_ffmpeg()
    assert (app.text["output"].get(), app.text["audio_file"].get(), app.text["ffmpeg"].get()) == ("/o/out.mp4", "/m/bgm.wav", "/m/bgm.wav")
    dialogs.answers.update(asksaveasfilename="", askopenfilename="")
    app.browse_output()
    assert app.text["output"].get() == "/o/out.mp4"


# ---- running ----------------------------------------------------------------------------------------------------------------
def test_settings_problems_are_shown_and_nothing_starts(app, dialogs, dummy, tmp_path):
    app.start()                                                                   # nothing entered
    assert dialogs.errors[-1][0] == "設定を確認してください" and "入力" in dialogs.errors[-1][1]
    assert not app.runner.running
    out = fill(app, dummy, tmp_path)
    out.parent.mkdir(parents=True)
    out.write_bytes(b"x")
    app.start()
    title, msg = dialogs.errors[-1]
    assert "既に存在します" in msg and "上書きする" in msg                        # tells the GUI user what to tick
    app.write_settings(GuiSettings(inputs=[str(dummy.base)], output=str(out), duration="3", width="abc"))
    app.start()
    assert "幅" in dialogs.errors[-1][1]
    assert "幅" in app.log.get("1.0", "end")                                       # also kept in the log


def test_generate_from_the_window(app, dialogs, dummy, tmp_path, monkeypatch, ffprobe_json):
    out = fill(app, dummy, tmp_path)
    opened = []
    monkeypatch.setattr(appmod, "open_path", lambda p: opened.append(p))
    app.start()
    # while running: the form is locked, only Cancel is live
    assert app.runner.running
    assert str(app.run_button.cget("state")) == "disabled" and str(app.plan_button.cget("state")) == "disabled"
    assert str(app.cancel_button.cget("state")) == "normal"
    assert str(app.input_list.cget("state")) == "disabled" and str(app.cut_mode_widget().cget("state")) == "disabled"
    values = []
    pump(app, lambda: values.append(float(app.bar["value"])) or idle(app))
    assert dialogs.errors == []
    assert app.status.get() == "できました" and out.is_file()
    assert values == sorted(values) and values[-1] == 100.0                           # the bar only moves forward
    log = app.log.get("1.0", "end")
    assert "できました" in log and "seed=5" in log
    # form unlocked again, result buttons live
    assert str(app.input_list.cget("state")) == "normal" and str(app.cancel_button.cget("state")) == "disabled"
    assert str(app.folder_button.cget("state")) == "normal" and str(app.play_button.cget("state")) == "normal"
    app.open_folder()
    app.play()
    assert opened == [out.parent, out]
    v = next(s for s in ffprobe_json(out)["streams"] if s["codec_type"] == "video")
    assert int(v["nb_frames"]) == 90


def test_skipped_inputs_are_mentioned(app, dummy, tmp_path):
    fill(app, dummy, tmp_path, inputs=[str(dummy.base), str(dummy.truncated)])
    app.start()
    pump(app, lambda: idle(app))
    assert "使えなかった入力が 1 件" in app.log.get("1.0", "end")


def test_failure_is_a_dialog_and_the_window_recovers(app, dialogs, dummy, tmp_path):
    fill(app, dummy, tmp_path, inputs=[str(dummy.truncated)])
    app.start()
    pump(app, lambda: idle(app))
    assert dialogs.errors[-1][0] == "失敗しました" and "有効な入力" in dialogs.errors[-1][1]
    assert app.status.get() == "失敗しました" and str(app.play_button.cget("state")) == "disabled"
    app.start()                                                                          # and it can run again
    pump(app, lambda: idle(app))
    assert len(dialogs.errors) == 2


def test_cancel_from_the_window(app, dialogs, dummy, tmp_path):
    out = fill(app, dummy, tmp_path, duration="30", cut_fixed="15", width="1280", height="720",
               effects={n: n == "kaleidoscope" for n in model.registry.names()})
    app.start()
    t0 = time.monotonic()
    while time.monotonic() - t0 < 1.0:
        process_events(app.root)
        app.poll_now()
        time.sleep(0.02)
    app.cancel()
    assert app.status.get() == "中止しています" and str(app.cancel_button.cget("state")) == "disabled"
    pump(app, lambda: idle(app), timeout=30)
    assert app.status.get() == "中止しました" and not out.exists() and dialogs.errors == []
    assert "中止しました" in app.log.get("1.0", "end")


def test_plan_only_button(app, dummy, tmp_path):
    out = fill(app, dummy, tmp_path)
    app.start(plan_only=True)
    pump(app, lambda: idle(app))
    assert app.status.get() == "構成(plan)を保存しました"
    assert not out.exists() and out.with_name(out.name + ".plan.json").is_file()
    assert str(app.play_button.cget("state")) == "disabled"


def test_rebuild_from_a_plan_in_the_menu(app, dialogs, dummy, tmp_path):
    out = fill(app, dummy, tmp_path)
    app.start(plan_only=True)
    pump(app, lambda: idle(app))
    plan_file = out.with_name(out.name + ".plan.json")
    again = tmp_path / "again.mp4"
    app.text["output"].set(str(again))
    dialogs.answers["askopenfilename"] = str(plan_file)
    app.replay_plan()
    pump(app, lambda: idle(app))
    assert again.is_file() and app.status.get() == "できました" and dialogs.errors == []
    app.text["output"].set("")                                                           # no output: explained, not started
    app.replay_plan()
    assert dialogs.errors[-1][0] == "出力先を確認してください"


# ---- settings files and memory -----------------------------------------------------------------------------------------------
def test_save_and_open_settings(app, dialogs, tmp_path):
    s = GuiSettings(inputs=[A_MP4], output=O_MP4, duration="45", cut_mode="beat", beats="2", seed="7",
                    effects={**{n: True for n in model.registry.names()}, "strobe": False})
    app.write_settings(s)
    f = tmp_path / "mine.yaml"
    dialogs.answers["asksaveasfilename"] = str(f)
    app.save_settings()
    assert f.is_file()
    app.write_settings(GuiSettings())
    dialogs.answers["askopenfilename"] = str(f)
    app.open_settings()
    back = app.read_settings()
    assert (back.inputs, back.output, back.duration, back.cut_mode, back.beats, back.seed) == ([A_MP4], O_MP4, "45", "beat", "2", "7")
    assert back.effects["strobe"] is False and back.effects["hue"] is True
    f.write_text("{ broken: [", encoding="utf-8")
    app.open_settings()
    assert dialogs.errors[-1][0] == "設定を読めません"
    assert app.read_settings().output == O_MP4                                          # the form is left as it was


def test_the_next_launch_restores_the_last_settings(root, tmp_path, capsys):
    store = tmp_path / "store"
    first = App(root, remember=True, store_dir=store)
    first.write_settings(GuiSettings(inputs=[A_MP4], output=O_MP4, duration="12", cut_mode="random", overwrite=True))
    first.close()
    second_root = new_tk(capsys)
    second_root.withdraw()
    try:
        second = App(second_root, remember=True, store_dir=store)
        s = second.read_settings()
        assert (s.inputs, s.output, s.duration, s.cut_mode) == ([A_MP4], O_MP4, "12", "random")
        assert s.overwrite is False                                                           # never silently overwrite
        second.close()
    finally:
        try:
            second_root.destroy()
        except tk.TclError:
            pass


def test_closing_during_a_job_asks_first(app, dialogs, dummy, tmp_path):
    fill(app, dummy, tmp_path, duration="30", cut_fixed="15", width="1280", height="720",
         effects={n: n == "kaleidoscope" for n in model.registry.names()})
    app.start()
    t0 = time.monotonic()
    while time.monotonic() - t0 < 1.0:
        process_events(app.root)
        app.poll_now()
        time.sleep(0.02)
    dialogs.ask_yes = False
    app.close()
    assert app.runner.running and app.root.winfo_exists() == 1                              # "no": keep going
    dialogs.ask_yes = True
    app.close()
    assert not app.runner.running                                                           # "yes": cancelled, then closed
    with pytest.raises(tk.TclError):
        app.root.winfo_exists()                                                             # the window is gone


# ---- details ---------------------------------------------------------------------------------------------------------------------
def test_progress_bar_follows_the_stages_in_order(app):
    seen = []
    for stage, cur, tot in [("入力検証", 0, 2), ("入力検証", 2, 2), ("BPM 解析", 0, 1), ("BPM 解析", 1, 1), ("計画", 0, 1), ("計画", 1, 1),
                            ("セグメント生成", 0, 900), ("セグメント生成", 450, 900), ("セグメント生成", 900, 900), ("連結", 0, 1), ("連結", 1, 1)]:
        app._handle("progress", (stage, cur, tot))
        seen.append(float(app.bar["value"]))
    assert seen == sorted(seen) and seen[0] == 0 and seen[-1] == 100.0
    app._handle("progress", ("セグメント生成", 450, 900))
    assert app.status.get() == "セグメント生成  450/900フレーム"


def test_log_lines_are_coloured_by_level(app):
    app._handle("log", (20, "info line"))
    app._handle("log", (30, "warn line"))
    app._handle("log", (40, "error line"))
    tags = {app.log.get(f"{i}.0", f"{i}.end"): app.log.tag_names(f"{i}.0") for i in (1, 2, 3)}
    assert tags["info line"] == () and "warning" in tags["warn line"] and "error" in tags["error line"]


def test_japanese_font_is_chosen_from_what_is_installed(root, monkeypatch):
    from tkinter import font as tkfont

    some = tkfont.families(root)[0]
    monkeypatch.setattr(appmod, "PREFERRED_FONTS", ("No Such Font", some))
    assert appmod.apply_japanese_font(root) == some
    assert tkfont.nametofont("TkDefaultFont", root=root).actual("family") == some
    monkeypatch.setattr(appmod, "PREFERRED_FONTS", ("No Such Font",))
    assert appmod.apply_japanese_font(root) is None


def test_open_path_uses_the_system_opener(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(appmod.subprocess, "Popen", lambda cmd: calls.append(cmd))
    monkeypatch.setattr(appmod.sys, "platform", "linux")
    monkeypatch.setattr(appmod.os, "name", "posix")
    appmod.open_path(tmp_path)
    monkeypatch.setattr(appmod.sys, "platform", "darwin")
    appmod.open_path(tmp_path)
    assert calls == [["xdg-open", str(tmp_path)], ["open", str(tmp_path)]]
