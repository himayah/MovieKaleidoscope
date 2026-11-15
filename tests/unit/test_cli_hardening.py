"""How the CLI behaves when things go wrong or the console is limited."""

from __future__ import annotations

import io
import logging
import sys

import pytest

from moviekaleidoscope import cli
from moviekaleidoscope.util.progress import LogProgress, TqdmProgress


def run(*argv):
    return cli.main(list(argv))


# ---- consoles that cannot show every character ----------------------------------------------------------------
def strict_cp932():
    """Redirected output on Japanese Windows: cp932, and a failing write raises."""
    raw = io.BytesIO()
    return raw, io.TextIOWrapper(raw, encoding="cp932", errors="strict", write_through=True)


def test_premise_cp932_really_rejects_some_characters():
    _, stream = strict_cp932()
    stream.write("〜")                              # fine in cp932 (Shift-JIS wave dash)
    with pytest.raises(UnicodeEncodeError):
        stream.write("\u2014")                      # EM DASH is not


@pytest.mark.parametrize("command", [["--list-effects"], ["--dump-config"]])
def test_normal_output_is_plain_cp932(monkeypatch, command):
    raw, stream = strict_cp932()
    monkeypatch.setattr(sys, "stdout", stream)
    assert run(*command) == 0
    assert "?" not in raw.getvalue().decode("cp932") and len(raw.getvalue()) > 100


def test_unencodable_characters_degrade_instead_of_crashing(monkeypatch):
    """Dynamic text (file names, ffmpeg messages) can hold anything: it must not kill a good run."""
    raw, err = strict_cp932()
    monkeypatch.setattr(sys, "stderr", err)
    cli._prepare_streams()
    err.write("a\u2014b")                           # would raise before reconfigure
    err.flush()
    assert raw.getvalue() == b"a?b"


def test_every_message_in_the_source_fits_cp932():
    """Messages are Japanese and Windows is the first target: keep to characters cp932 can show."""
    import ast
    import pathlib

    root = pathlib.Path(cli.__file__).parent
    bad = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                      if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                      and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                try:
                    node.value.encode("cp932")
                except UnicodeEncodeError as e:
                    bad.append(f"{path.name}:{node.lineno}: {node.value[e.start]!r} in {node.value[:40]!r}")
    assert bad == []


# ---- unexpected failures ---------------------------------------------------------------------------------
def test_unexpected_exception_is_reported_plainly(monkeypatch, caplog, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("simulated bug")

    monkeypatch.setattr(cli, "generate", boom)
    args = ["-i", str(tmp_path), "-o", str(tmp_path / "o.mp4"), "-d", "2", "-q"]
    with caplog.at_level(logging.ERROR):
        assert run(*args) == 1
    assert "予期しないエラー: RuntimeError: simulated bug" in caplog.text
    assert not any(r.exc_info for r in caplog.records)           # no traceback unless -v
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        assert run(*args, "-v") == 1
    assert any(r.exc_info for r in caplog.records)


def test_os_errors_are_reported_without_a_traceback(monkeypatch, caplog, tmp_path):
    def disk_full(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cli, "generate", disk_full)
    with caplog.at_level(logging.ERROR):
        assert run("-i", str(tmp_path), "-o", str(tmp_path / "o.mp4"), "-d", "2", "-q") == 1
    assert "ファイル操作に失敗しました" in caplog.text and "No space left" in caplog.text


def test_output_that_is_a_folder_is_a_usage_error(tmp_path):
    folder = tmp_path / "out.mp4"
    folder.mkdir()
    assert run("-i", str(tmp_path), "-o", str(folder), "-d", "2", "-q") == 2


# ---- progress ---------------------------------------------------------------------------------------------------
def test_log_progress_reports_start_quarters_and_end(caplog):
    log = logging.getLogger("t.progress")
    p = LogProgress(log)
    with caplog.at_level(logging.INFO, logger="t.progress"):
        p("セグメント生成", 0, 1000)
        for n in (100, 260, 270, 510, 990, 1000):
            p("セグメント生成", n, 1000)
        p("連結", 0, 1)
        p("連結", 1, 1)
    lines = [r.getMessage() for r in caplog.records]
    assert lines == ["セグメント生成: 開始(1000フレーム)", "セグメント生成: 26%(260/1000フレーム)",
                     "セグメント生成: 51%(510/1000フレーム)", "セグメント生成: 99%(990/1000フレーム)",
                     "セグメント生成: 完了", "連結"]


def test_log_progress_with_big_jumps_logs_each_step_once(caplog):
    p = LogProgress(logging.getLogger("t.progress2"))
    with caplog.at_level(logging.INFO, logger="t.progress2"):
        p("セグメント生成", 0, 100)
        p("セグメント生成", 90, 100)          # one big step: one line, not three
        p("セグメント生成", 100, 100)
    assert len(caplog.records) == 3


def test_tqdm_progress_draws_and_cleans_up():
    out = io.StringIO()
    p = TqdmProgress(file=out)
    p("入力検証", 0, 3)
    p("入力検証", 2, 3)
    p("セグメント生成", 0, 90)           # a new stage replaces the bar
    p("セグメント生成", 90, 90)
    p.close()
    p.close()                              # closing twice is fine
    assert "入力検証" in out.getvalue() and "フレーム" in out.getvalue()


# ---- the GUI launcher without tkinter -------------------------------------------------------------------------------
def test_gui_launcher_explains_a_missing_tkinter(monkeypatch, capsys):
    from moviekaleidoscope import gui

    monkeypatch.delitem(sys.modules, "moviekaleidoscope.gui.app", raising=False)
    monkeypatch.setitem(sys.modules, "tkinter", None)            # `import tkinter` now raises ImportError
    assert gui.main([]) == 1
    err = capsys.readouterr().err
    assert "tkinter" in err and "python3-tk" in err and "mkaleido" in err
