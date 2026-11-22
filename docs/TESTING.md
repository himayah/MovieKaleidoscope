# テストの状況と、Windows での確認手順

- 作成日: 2026-11-15(コードの版: 0.1.0 / 仕様書 v0.4)
- 目的: ① どこまでテストが終わっていて、何が未確認かを記録する。② **Windows 上で AI(別のセッション)を呼び出し、残りの自動テストと確認を実行してもらう**ための手順書にする。
- このファイルは、AI が確認を終えるたびに §7(実施結果)を更新する前提で書いてある。

## 1. 要約

| 区分 | 状態 |
|---|---|
| 自動テスト(WSL2 上の Linux) | **491 件中 490 件合格、1 件スキップ**(Windows 専用のテスト)。2026-11-15 の修正後に再実行 |
| 自動テスト(Windows ネイティブ) | **518 件すべて合格**(2026-11-16。Windows 11、Python 3.12.0、ffmpeg 8.1.2)。2026-11-15 の初回は 23 件失敗。原因と修正は §7.1 |
| ffmpeg のバージョン | **Windows で 4.4.1 / 5.1.2 / 6.1.1 / 7.1.1 / 8.1.2 / 9.0.1 の全テスト合格**(各 491 件。9.0.1 は winget で入れたもの)、WSL2 で 4.4.2 |
| CI(GitHub Actions) | 2026-11-16 に初めて実行(最初の push では YAML の誤りで 1 件も動かず、修正)。**Windows(3.10 / 3.12)は合格**。**Linux は `reverse` のフレーム不足で 3 件失敗** → WSL2 で ffmpeg 7.0.2(johnvansickle の静的ビルド)を使って再現し、修正(§7.1 の #12)。修正後(6a04758)は **Linux(3.10 / 3.12)と Windows(3.10 / 3.12)が合格**。**macOS は「All tests」の段階で止まったまま終わらなかった** → ログで `test_gui_app.py::test_generate_from_the_window` の `update()` が戻らないと特定。macOS(Aqua Tk 8.6.16)ではウィンドウを何度か作ったあと、ネイティブのイベントが途切れず届き続け、「イベントがなくなるまで処理する」`update()` が終わらない(1 回で 2000 件を超える)。テストのイベント処理を 1 回 2000 件までにして解決(アプリ本体の `mainloop()` にはこの問題はない)。あわせて、1 テスト 5 分・1 ジョブ 30 分の制限を追加。**修正後は 6 ジョブすべて(Windows / Linux / macOS × 3.10 / 3.12)合格** |
| macOS | CI(GitHub Actions の macos-latest、arm64、Homebrew の ffmpeg)で全テスト合格。実機での GUI 操作は未確認 |
| 人が見て判断する項目(見た目、音楽との合い方、再生互換性など) | **すべて完了**。C6 は決定済み(近い標準の FPS に丸める)。C1〜C4 の人の確認は**すべて OK**(2026-11-16、利用者)。C5 は自動で測定済み |
| Windows で見つかった製品の不具合 | 3 件を修正(新しい ffmpeg での色、Chocolatey のシムによる ffmpeg の取り残し、Ctrl+C で誤った「失敗」と再試行)。§7.1 |

自動テストが「通っている」ことと、「Windows で動く」ことは別です。特に、タイムスタンプ(連結)、色空間、子プロセスの後始末は、ffmpeg のバージョンと OS に依存する部分で、実際に不具合を見つけて直してきた領域です(仕様書 §8.2、§3.3、§11)。

## 2. 実施済みのテスト

### 2.1 実行環境(これまでの確認はすべてここ)

| 項目 | 値 |
|---|---|
| OS | Linux 6.18(WSL2)。Windows ホスト上の Ubuntu 22.04 |
| Python | 3.10.12 |
| ffmpeg / ffprobe | 4.4.2(Ubuntu のパッケージ) |
| numpy / opencv(cv2) / PyYAML / tqdm / pytest | 1.26.4 / 5.0.0 / 6.0.3 / 4.70.0 / 8.3.3 |
| 表示 | GUI のウィンドウのテストは Xvfb(仮想ディスプレイ)で実行 |
| CPU | 24 論理コア |

### 2.2 実行したコマンドと結果

```
python -m pytest -m "not integration" -q     → 302 passed (約 22 秒。ffmpeg 不要)
xvfb-run -a python -m pytest -q              → 490 passed (約 305 秒)
(表示なしで) python -m pytest -q tests/integration/test_gui_app.py
                                             → 1 passed, 21 skipped (ウィンドウのテストは表示がなければスキップ)
```

### 2.3 内訳(何を保証しているか)

**ユニット(303 件。`-m "not integration"`)**

| ファイル | 件数 | 内容 |
|---|---|---|
| `tests/unit/test_config.py` | 73 | 設定の読み込み・優先順位・検証、パスの基準、CLI オプションの規則、設定ファイルの往復(`to_file_dict`) |
| `tests/unit/test_motion_effects.py` | 68 | モーション(式と数値の一致)、サンプリングの範囲、ffmpeg 系エフェクトのフィルタ、レジストリ、重み、範囲の上書き、plan の検証、乱数ストリームの独立 |
| `tests/unit/test_frame_effects.py` | 62 | frame 系エフェクトを合成画像で検査(厳密な幾何・値の性質。ミラー、タイル、万華鏡の対称性など) |
| `tests/unit/test_beat.py` | 59 | BPM 検出(精度、位相、倍テンポ、ノイズ・短い音声の拒否)、拍に合わせたカットの境界、拍同期エフェクト、API の判断(解析・フォールバック・上書き) |
| `tests/unit/test_planner.py` | 17 | カット長、末尾の結合、均等な素材選択、ループ、再現性、plan の入出力、子プロセスのキャンセル(ランチャーの先のプロセスまで止まるか。Windows のみ) |
| `tests/unit/test_inputs.py` | 12 | 入力の収集(自然順、拡張子、重複、再帰)、出力形式の決定 |
| `tests/unit/test_cli_hardening.py` | 12 | cp932 の端末への出力、予期しない例外、進捗(`LogProgress`、`TqdmProgress`)、tkinter がないときの案内 |

**統合(188 件。`-m integration`。ffmpeg が必要)**

| ファイル | 件数 | 内容 |
|---|---|---|
| `tests/integration/test_effects.py` | 59 | 全エフェクトを実ファイルで検査。基準(エフェクトなし)と画素を比較(反転、逆再生、速度の曲線、色相、彩度、RGB シフト)。frame バックエンドは「プロセッサを基準に適用した結果」との一致、タイミングの厳密さ、キャンセル時の子プロセスの後始末と作業フォルダの削除(2026-11-15 から Windows でも実行) |
| `tests/integration/test_generate.py` | 30 | 生成の一連(解像度の統一、NTSC、ループ、plan の往復、`--from-plan`、リトライ、キャンセル、一時フォルダの後始末、CLI の終了コード) |
| `tests/integration/test_gui_runner.py` | 30 | GUI の設定モデルとバックグラウンド実行(画面なし) |
| `tests/integration/test_media.py` | 22 | ffmpeg の探索、ffprobe、壊れた入力、日本語パス、回転メタデータ |
| `tests/integration/test_gui_app.py` | 22 | ウィンドウの操作(**表示が必要**) |
| `tests/integration/test_audio.py` | 16 | 実ファイルの BPM 解析、音声の合成、**映像のカット時刻と音声のクリック位置の同期**、音声の長さの規則 |
| `tests/integration/test_color.py` | 9 | 色の保存(素材の種類 4 通り × 経路 2 通り)、全セグメントのタグの一致 |

特に重要な回帰テスト(過去に実際に起きた不具合を捕まえるもの。Windows で失敗したら要注意):

| テスト | 守っているもの |
|---|---|
| `test_generate.py::test_cuts_do_not_disturb_timing` | 連結時にカットごとのコマ飛び・時間のずれが蓄積しないこと(全フレームの時刻の間隔と末尾のずれ 0.5ms 未満) |
| `test_color.py::test_colours_are_preserved` ほか | 出力の色が素材と一致し、全セグメントが BT.709 のタグを持つこと |
| `test_audio.py::test_cuts_land_on_the_clicks` | 映像のカットが音声の拍に 0.04 秒以内で合うこと |
| `test_effects.py::test_pipe_equals_processor_applied_to_the_reference` | frame バックエンドのパイプで、チャンネル順・時刻・フレームの対応がずれないこと |
| `test_effects.py::test_cancel_during_a_frame_render_stops_the_ffmpeg_processes` | キャンセル後に ffmpeg が残らず、作業フォルダが空になること(全 OS) |

### 2.4 ここで実行した追加の確認(Linux、ffmpeg 4.4.2)

テストには含まれていない種類の入力を、CLI に通して挙動を記録した(`-d 3 --cut-length 1`)。**Windows でも同じ結果になるかを §4.2 の W-B5 で確認する。**

| 入力 | 終了コード | 出力 | メモ |
|---|---|---|---|
| 10bit(yuv420p10le) | 0 | 640×360、yuv420p、30fps、90 フレーム | 警告なし。色の正しさは数値では未検証 |
| フルレンジ(yuvj420p) | 0 | 同上 | 警告なし。色の正しさは未検証 |
| 可変フレームレート(VFR) | 0 | 640×360、**3120/179 fps(約 17.43)**、52 フレーム | 出力 FPS が、最初の入力の `avg_frame_rate` という中途半端な値になる(当時の仕様どおり)。C6 の決定により、2026-11-16 からは **15 fps** に丸める(Windows・8.1.2 で 15/1、3 秒で 45 フレームを確認) |
| 音声つきの動画 | 0 | 音声なし(仕様どおり破棄) | |
| 奇数サイズ(641×361、yuv444p) | 0 | 640×360(偶数に切り捨て) | |
| GIF(10fps) | 0 | 320×180、10fps、30 フレーム | |
| mpeg4 / avi | 0 | 320×180、30fps | |

Windows の結果と見比べるための、Linux(WSL2、ffmpeg 4.4.2)での基準値:

| 確認 | Linux での結果 |
|---|---|
| W-B1 性能(1280×720・60 秒・全エフェクト既定・22 クリップ、`preset: medium`) | `--jobs 1`: 29.7 秒、`--jobs 12`: 9.7 秒、ピークメモリ約 1GB。1920×1080 は `--jobs 12` で 18 秒、約 1.4GB(24 論理コア) |
| W-B2 (1) API でのキャンセル(30 秒の動画、1.5 秒後に中止) | 1.6 秒で中止。ffmpeg は残らず、作業フォルダは空(この手順書のスクリプトをそのまま実行して確認。`tasklist` の代わりに `pgrep`) |
| W-B2 (2) Ctrl+C、W-B3 cp932、W-B4 日本語のパス(出力・作業フォルダ)、W-B6 インストール | 未実施(Windows 固有の確認。ホイールのビルドと、別の場所への配置・起動は Linux で確認済み) |

## 3. 未確認の領域

### 3.1 環境に依存するもの(優先度順)

| ID | 未確認の内容 | なぜ重要か | 確認の方法 |
|---|---|---|---|
| E1 | **Windows ネイティブでの全自動テスト** | 第一ターゲット。まだ一度も実行していない | §4.1、§4.2 の W-A |
| E2 | **新しい ffmpeg(6〜8 系)** | タイムスタンプ(連結)、色空間(タグに従う変換)、`scale` のオプション、mp4 のマルチプレクサの挙動が、バージョンで変わりうる。4.4 でしか確認していない | W-A4(ffmpeg のバージョンを記録して全テスト) |
| E3 | 子プロセス(ffmpeg)の後始末(キャンセル・失敗・Ctrl+C) | Windows の `terminate()` と Ctrl+C の伝わり方が POSIX と違う。自動テストはここで Windows をスキップしている | W-B2 |
| E4 | cp932 のリダイレクト出力 | 日本語 Windows で実際に起きた(起きうる)不具合の領域。テストは擬似的な cp932 ストリームで確認しただけ | W-B3 |
| E5 | 日本語・空白を含むパスでの一連の動作(入力・出力・作業フォルダ・ユーザープロファイル) | テストは日本語名の入力ファイルとフォルダのみ | W-B4 |
| E6 | `pip install` したパッケージとしての動作、`mkaleido-gui`(コンソールなしの起動) | ホイールを作って配置・起動を確認したのは Linux のみ | W-B6 |
| E7 | GitHub Actions での実行 | 設定は未実行。ffmpeg のセットアップのステップが最初の失敗候補 | リポジトリを作ってから実行 → **2026-11-16 に実施、全 OS で合格**(§1、§7.1 の #12・#15) |
| E8 | macOS | 完全に未確認 | 実機がある場合のみ → CI(macos-latest)で全テスト合格(2026-11-16)。実機での GUI 操作は未確認 |
| E9 | 性能(Windows) | WSL2 の数値のみ(README の表) | W-B1 |

### 3.2 テストでカバーしていない領域(OS に依らない)

grep で確認した(テストに該当する入力・機能が存在しない)もの:

- 10bit・HDR、フルレンジ(yuvj)、可変フレームレート、**音声トラックを持つ入力**、奇数サイズ、GIF などの入力は、**テストがない**(§2.4 で手動実行した結果のみ)。
- 回転メタデータのある動画は、ffprobe の読み取りだけテストしていて、**レンダリングはテストしていない**。
- 実際の楽曲での BPM 検出(合成した信号のみ)。
- パラメータ範囲の見た目(§4.2 の C3)。
- 大量のクリップ(数百)と長い動画(数分以上)の実生成(性能の測定のみ。結果の検証はしていない)。
- `reverse` が実際に使うメモリ(計算値のみ)。

### 3.3 Windows で、テストの書き方が原因で失敗しうる箇所(予測)

**これは実行して確認したものではなく、コードを読んでの予測。**(2026-11-15 に Windows で実行した結果: GUI の 2 行は予測どおり失敗した。`test_beat.py`、`test_config.py` は予測どおり通った。`assert_clean` は不安定にならなかったが、別の原因(Chocolatey のシム)で作業フォルダが残る製品の不具合が見つかった。pgrep のテストは移植した。§7.1)失敗したら、まず「テストが POSIX 前提で書かれている」ことを疑い、製品の不具合と切り分けること(§6)。

| 場所 | 予測される問題 |
|---|---|
| `tests/integration/test_gui_runner.py`(`"/m/bgm.wav"`、`"/opt/ffmpeg"`、`"/out/o.mp4"` など) | Windows では `Path("/m/bgm.wav")` が `\m\bgm.wav` になり、文字列の比較(`str(cfg.audio.file) == "/m/bgm.wav"`)が失敗する。ドライブ文字のない `/x` は絶対パスと見なされないため、設定の読み込みでの基準パスの解決も変わる |
| `tests/integration/test_gui_app.py`(`"/a.mp4"`、`"/o.mp4"`、`"/x/a.mp4"` など) | 同上。フォームの値と、設定ファイルを往復したあとの値の比較 |
| `tests/unit/test_beat.py`(`Path("/m/bgm.wav")`)、`tests/unit/test_config.py`(`"/abs/dir"`、`"/tmp/w"`) | 往復の比較は `Path` 同士なので通る可能性が高いが、未確認 |
| `assert_clean(tmp_path)`(作業フォルダが空であること) | Windows ではウイルス対策ソフトなどがファイルを掴んでいて、削除が遅れると不安定になりうる |
| `tests/integration/test_effects.py::test_cancel_during...` | `pgrep` がないためスキップされる(失敗ではない) |

## 4. Windows で AI に実行してもらう手順

### 4.1 準備

前提:

- Windows 10/11、**対話型のデスクトップセッション**(GUI のウィンドウのテストが `tk.Tk()` を作れること。サービスや CI のような画面なしの環境では、そのテストはスキップされる)。
- Python 3.10 以上(3.10 と 3.12 の両方で試せれば理想)。
- ffmpeg と ffprobe(**バージョンを必ず記録する**。`winget install Gyan.FFmpeg` で入る。ターミナルを開き直す)。

```powershell
cd <このプロジェクトのフォルダ>            # README.md がある場所
python --version
ffmpeg -version | Select-Object -First 1
ffprobe -version | Select-Object -First 1
python -m pip install -e ".[dev]"
mkaleido --version
```

プロジェクトはまだ Git リポジトリではない。**コミットや公開はしない。**

### 4.2 確認項目

結果は §7 の表に書く。**ID はそのまま使う。**

#### A. 自動テスト(AI が実行して結果を報告する)

| ID | 内容 | コマンド | 期待 |
|---|---|---|---|
| W-A1 | 環境の記録 | §4.1 のコマンド。加えて `python -c "import numpy, cv2, yaml, tqdm, pytest; print(numpy.__version__, cv2.__version__, yaml.__version__, tqdm.__version__, pytest.__version__)"` | バージョンを記録するだけ |
| W-A2 | ユニットテスト | `python -m pytest -m "not integration" -q` | 303 件合格(約 30 秒)。**ffmpeg は不要** |
| W-A3 | 全テスト | `python -m pytest -q --durations=25` | 491 件すべて合格(約 2.5 分。2026-11-15 の実績)。Linux では、Windows 専用のテスト 1 件がスキップされる |
| W-A4 | ffmpeg のバージョン違い(任意) | 別のバージョンの ffmpeg の場所を環境変数 `FFMPEG_BIN` と `FFPROBE_BIN` に設定して W-A3 を再実行。可能なら、4.4〜5 系と最新系の両方 | 同じ結果。**違いが出たら、どのテストか、どの ffmpeg かを記録(最重要)** |
| W-A5 | Python のバージョン違い(任意) | 3.10 と 3.12 で W-A3 | 同じ結果 |

失敗を分析する順序:

1. `python -m pytest -q <失敗したテストの ID> -x` で再現し、メッセージを読む。
2. §3.3 の予測に当てはまるか確認する。
3. §6 の規則で、テストの問題か、製品の問題か、環境の問題かに分類する。

#### B. コマンドで確認する項目(AI が実行できる)

**W-B1 性能の測定**(README の表と同じ条件。Windows の数値を残すため):

```powershell
$d = "$env:TEMP\mk_dummy"
python tests\tools\make_dummy.py $d
"video: {crf: 18, preset: medium}" | Set-Content "$d\perf.yaml" -Encoding utf8
foreach ($j in 1, 4) {
  Remove-Item "$d\perf.mp4*" -ErrorAction SilentlyContinue
  $t = Measure-Command { mkaleido -c "$d\perf.yaml" -i "$d\base_720p30.mp4" -i "$d\small43_480p24.mp4" -i "$d\vertical_1080x1920.mp4" -o "$d\perf.mp4" -d 60 --cut-random 1:4 --seed 4 --jobs $j -y -q }
  "jobs=$j : $($t.TotalSeconds) 秒"
}
```

記録する値: CPU の論理コア数(`$env:NUMBER_OF_PROCESSORS`)、各 `jobs` の秒数。目標は、**一般的な PC で 5 分以内**(WSL2 では `jobs=1` で 29.7 秒)。

**W-B2 キャンセル・中断の後始末**(自動テストが Windows でスキップしている部分):

```powershell
# (1) API でキャンセルし、ffmpeg が残らず、作業フォルダが消えるか(PowerShell のヒアストリングを python に渡す)
@'
import threading, time, subprocess, tempfile, pathlib
from moviekaleidoscope.config import config_from_dict
from moviekaleidoscope.api import generate
from moviekaleidoscope.errors import Cancelled
d = pathlib.Path(tempfile.gettempdir()) / "mk_dummy"
work = pathlib.Path(tempfile.mkdtemp(prefix="mkwork_"))
cfg = config_from_dict({"inputs": [str(d / "base_720p30.mp4")], "output": str(work / "o.mp4"), "duration": 30, "seed": 1,
    "cut": {"mode": "fixed", "fixed": 15}, "effects": {"enabled": ["kaleidoscope"]}, "video": {"width": 1280, "height": 720},
    "runtime": {"work_dir": str(work / "w")}})
ev = threading.Event(); threading.Timer(1.5, ev.set).start()
t0 = time.monotonic()
try: generate(cfg, cancel=ev); print("NOT cancelled?!")
except Cancelled: print("cancelled after %.1fs" % (time.monotonic() - t0))
time.sleep(1)
out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq ffmpeg.exe"], capture_output=True, text=True).stdout
print("ffmpeg.exe still running:", "ffmpeg.exe" in out)
print("work dir leftovers:", list((work / "w").iterdir()) if (work / "w").exists() else [])
'@ | python -
```

期待: 数秒以内に中止し、`ffmpeg.exe still running: False`、`work dir leftovers: []`。

```powershell
# (2) Ctrl+C(手動の操作が要る。AI が操作できなければ、人に頼む)
mkaleido -i "$env:TEMP\mk_dummy\base_720p30.mp4" -o "$env:TEMP\mk_dummy\ctrlc.mp4" -d 60 --cut-length 20 --effects kaleidoscope --width 1280 --height 720 -y
# 数秒後に Ctrl+C。そのあと:
echo $LASTEXITCODE        # 期待: 130。"中断しました" と表示され、トレースバックや RenderError は出ない
Get-Process ffmpeg -ErrorAction SilentlyContinue   # 期待: 何も出ない
```

注意: Windows では、Ctrl+C がコンソール内の子プロセス(ffmpeg)にも届き、ffmpeg が先に異常終了する。そのとき、**ワーカーの失敗(RenderError)ではなく、中断(130)として扱われること**を確認する。(2026-11-15 以降は ffmpeg を別のプロセスグループで起動するので、Ctrl+C は ffmpeg に届かない。SPEC §11)

AI が実行するときは `tests/tools/ctrl_c_windows.py` を使う。新しいコンソールでコマンドを動かし、指定秒後にそのコンソールの全プロセスへ本物の Ctrl+C(`CTRL_C_EVENT`)を送る。呼び出し元のコンソールには届かない。

```powershell
python tests\tools\ctrl_c_windows.py 3 (Get-Command mkaleido).Source -i "$env:TEMP\mk_dummy\base_720p30.mp4" -i "$env:TEMP\mk_dummy\small43_480p24.mp4" -o "$env:TEMP\mk_dummy\ctrlc.mp4" -d 60 --cut-length 20 --effects kaleidoscope,hue --width 1280 --height 720 -y
# 期待: exit_code 130、ffmpeg_after_1s 0、出力に WARNING(書き出しに失敗/やり直します)が出ない
```

**W-B3 cp932 でのリダイレクト出力**:

```cmd
chcp 932
mkaleido --list-effects > %TEMP%\effects.txt
echo %ERRORLEVEL%
mkaleido --dump-config > %TEMP%\dump.yaml
mkaleido -c %TEMP%\dump.yaml --dump-config
```

期待: すべて終了コード 0(**クラッシュしないこと**が条件)。`%ERRORLEVEL%` は 1 行に `&` でつなぐと行全体の解釈時に展開されて正しく取れないので、コマンドごとに行を分ける(バッチファイルにする)。環境変数 `PYTHONIOENCODING` や `PYTHONUTF8` が設定されていると cp932 の経路を通らないので、`set PYTHONIOENCODING=` などで消してから実行する(AI のセッションでは `PYTHONIOENCODING=utf-8` が設定されていることがある)。先に、システムのロケールを記録する: `python -c "import locale,sys; print(locale.getpreferredencoding(), sys.stdout.encoding)"`。

- 日本語ロケールの Windows(cp932): `effects.txt` が cp932 として読めて、日本語が壊れていない。`?` を含まない(ソースのメッセージは cp932 で符号化できる文字に限っている)。
- 英語ロケールなど(cp1252 など): 日本語は符号化できず `?` に置き換わる。これは、クラッシュさせないための意図した動作(`errors="replace"`)。読みたいときは `set PYTHONUTF8=1` を付けて実行する。

PowerShell のリダイレクト(UTF-16 になる場合がある)も別に試して、クラッシュしないことを確認する。

**W-B4 日本語・空白を含むパス**:

```powershell
$base = "$env:TEMP\mk テスト 日本語"
New-Item -ItemType Directory "$base\素材 フォルダ" -Force | Out-Null
Copy-Item "$env:TEMP\mk_dummy\base_720p30.mp4" "$base\素材 フォルダ\動画 A.mp4"
Copy-Item "$env:TEMP\mk_dummy\click_120bpm.wav" "$base\素材 フォルダ\音楽 B.wav"
mkaleido -i "$base\素材 フォルダ" -o "$base\出力 フォルダ\結果 1.mp4" -d 6 --audio "$base\素材 フォルダ\音楽 B.wav" --work-dir "$base\作業 フォルダ" --seed 1 -y
echo $LASTEXITCODE
```

期待: 終了コード 0。出力と `結果 1.mp4.plan.json` が日本語のパスに作られ、作業フォルダの中身が消える。`ffprobe` で音声つきの 6 秒の動画。可能なら、ユーザー名に日本語や空白を含む環境、OneDrive 配下、ネットワークドライブでも試す。

**W-B5 これまでテストしていない種類の入力**(Linux での結果は §2.4):

```powershell
$e = "$env:TEMP\mk_edge"; New-Item -ItemType Directory $e -Force | Out-Null
$src = "testsrc2=size=640x360:rate=30:duration=4"
ffmpeg -v error -y -f lavfi -i $src -c:v libx264 -pix_fmt yuv420p10le -profile:v high10 "$e\ten_bit.mp4"
ffmpeg -v error -y -f lavfi -i $src -c:v libx264 -pix_fmt yuvj420p "$e\full_range.mp4"
ffmpeg -v error -y -f lavfi -i "testsrc2=size=640x360:rate=30:duration=6" -vf "select='lt(mod(n,7),4)'" -fps_mode vfr -c:v libx264 -pix_fmt yuv420p "$e\vfr.mp4"
ffmpeg -v error -y -f lavfi -i $src -f lavfi -i "sine=frequency=440:duration=4" -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest "$e\with_audio.mp4"
ffmpeg -v error -y -f lavfi -i "testsrc2=size=641x361:rate=30:duration=4" -c:v libx264 -pix_fmt yuv444p "$e\odd_size.mp4"
ffmpeg -v error -y -f lavfi -i "testsrc2=size=320x180:rate=10:duration=3" "$e\anim.gif"
Get-ChildItem "$e\*.*" | Where-Object { $_.Extension -in ".mp4",".gif" -and $_.Name -notlike "out_*" } | ForEach-Object {
  mkaleido -i $_.FullName -o "$e\out_$($_.BaseName).mp4" -d 3 --cut-length 1 --seed 2 -y -q
  "$($_.Name): exit=$LASTEXITCODE  " + (ffprobe -v error -select_streams v:0 -show_entries stream=width,height,pix_fmt,r_frame_rate,nb_frames -of csv=p=0 "$e\out_$($_.BaseName).mp4")
}
```

(`-fps_mode vfr` は 8.1.2 で使用。4.4 では `-vsync vfr`。回転メタデータつきの素材は、8.1.2 では `ffmpeg -display_rotation:v:0 90 -i 元.mp4 -c copy rotated.mp4` で作る。4.4 にはこのオプションがなく、`-metadata:s:v:0 rotate=90` を使う。8.1.2 は `rotate` のタグを無視して何も書かない。どの版から変わったかは未確認)

期待: すべて終了コード 0(または、使えない入力として警告つきでスキップされ、終了コード 3)。**クラッシュやトレースバックが出ないこと**が条件。出力の形式は §2.4 と同じになる見込み(VFR の FPS は ffmpeg の版で変わりうる)。色の正しさは、AI が画像を確認できるなら、フレームを 1 枚書き出して(`ffmpeg -i out.mp4 -frames:v 1 f.png`)元と見比べる。

**W-B6 インストールと起動**:

```powershell
python -m venv "$env:TEMP\mk_venv"
& "$env:TEMP\mk_venv\Scripts\pip" install .
& "$env:TEMP\mk_venv\Scripts\mkaleido" --version
Test-Path "$env:TEMP\mk_venv\Scripts\mkaleido-gui.exe"
& "$env:TEMP\mk_venv\Scripts\mkaleido" --list-effects | Select-Object -First 3
```

期待: バージョンが表示され、`mkaleido-gui.exe` が作られる。`mkaleido-gui.exe` を起動してウィンドウが開く(コンソールが出ない)かは、人が確認(§4.2 の C1)。

**W-B7 ffmpeg の入手経路の違い**: `winget` の Gyan.FFmpeg(essentials / full)、手でダウンロードした zip、環境変数 `FFMPEG_BIN` / `FFPROBE_BIN`、`--ffmpeg` で、それぞれ `mkaleido -i ... --ffmpeg <パス>` が動くこと。ffmpeg が見つからないときのエラー(終了コード 4)に、導入方法が出ること。

#### C. 人が見て判断する項目

AI だけでは判断できない。AI は、実行して素材(出力ファイル、スクリーンショット、数値)を用意するところまで行い、判断は人に回す。

| ID | 内容 |
|---|---|
| C1 | **GUI の見た目と操作**(Windows): 表示倍率 125% / 150% での崩れ、日本語フォント、ダイアログ、`mkaleido-gui` でコンソールが出ないこと、実行中のロックと中止 |
| C2 | **実際の楽曲**で、検出された BPM と、カットが拍に合っている感じ(倍テンポ・半テンポの取り違えがないか。自動検出が外れる曲の傾向) |
| C3 | **エフェクトの見た目**: 各エフェクトのパラメータ範囲が「サイケデリックで良い見た目」になっているか(仕様書 §6 で「目視で調整する」としたが、**未了**)。重みの既定値も |
| C4 | **再生互換性**: 出力の mp4(H.264、yuv420p、BT.709、faststart、AAC)が、Windows のメディアプレーヤー、VLC、ブラウザ、スマートフォン、動画サイトにアップロードしたときに、問題なく再生・処理されるか |
| C5 | 長い動画(数分)と大量のクリップ(数百)を、実際の素材で作ったときの所要時間・メモリ・見た目 |
| C6 | **可変フレームレートの最初の入力**で、出力 FPS が約 17.43 のような中途半端な値になる(§2.4)ことをどう扱うか。**決定(2026-11-16): 比が最も近い標準の FPS に丸める**(SPEC §3.3) |

### 4.3 AI への依頼文(コピーして使う)

> このプロジェクト(MovieKaleidoscope。動画を切り貼りしてサイケデリックな動画を作る Python 製のツール)の自動テストを、Windows で実行して確認してほしい。
>
> 1. まず `docs/TESTING.md` を最後まで読む。次に `README.md` を読む。必要に応じて `docs/SPEC.md` の §3.3(色空間)、§8.2(連結・タイムスタンプ)、§11(クロスプラットフォーム)、§13(テスト)、§17(GUI)を読む。
> 2. `docs/TESTING.md` の §4.1 で環境を整え、§4.2 の W-A1 から順に実行する。W-A4 と W-A5 は、環境が許せば実施する。W-B1〜W-B7 も、実行できるものはすべて実行する。人が見て判断する項目(C1〜C6)は、判断に必要な素材(出力ファイル、数値、スクリーンショット)を用意して、一覧にする。
> 3. 失敗したテストは、§6 の規則で「テストの問題」「製品の問題」「環境の問題」に分類する。**テストの期待値を、通すためだけに緩めない。** 製品の問題は、原因を調べて最小限の修正をし、その不具合を捕まえるテストを追加する。
> 4. 作業の終わりに、`docs/TESTING.md` の §7(実施結果)を、実際に実行した結果で更新する。実行していない項目は「未実施」と書く。推測を事実として書かない。
> 5. コミットや外部への公開はしない。

## 5. 補足: テスト実行時の環境の癖

- **Linux(WSL2)では、壁時計が数十秒単位で後ろへ跳ぶことがある。** pytest の所要時間が負の値になったり、`time.time()` で測った時間が実際より大きくなったりする。時間の測定には `time.monotonic()` を使う。Tk の `after` タイマーも遅れるので、GUI のテストは `app.poll_now()` で取り込んでいる。**Windows では通常は起きないが、時間に関する失敗は、まずこれを疑う。**
- GUI のウィンドウのテストは、表示がなければスキップされる(失敗にはならない)。Linux の CI は `xvfb-run` が必要。
- GUI のテストでイベントを処理するときは、`root.update()` ではなく `test_gui_app.py` の `process_events()`(1 回 2000 件まで)を使う。macOS では `update()` が戻らないことがある(§7.1 の #15)。
- ダミー素材は、テストの実行時に ffmpeg と numpy で生成する(`tests/tools/make_dummy.py`)。リポジトリには置かない。手で生成するには `python tests/tools/make_dummy.py <出力フォルダ>`。
- ユニットテストだけなら ffmpeg なしで動く(`-m "not integration"`)。

## 6. 失敗の分類と、してよいこと・してはいけないこと

| 分類 | 見分け方 | 対応 |
|---|---|---|
| **テストの問題**(Windows でのテストデータの書き方) | POSIX のパス(`/m/bgm.wav`)、改行コード、`pgrep` など、テストが OS を前提にしている | `tmp_path` から組み立てたパスを使うなど、**テストを移植可能に直す**。直し方を §7 に記録する。期待値の意味は変えない |
| **製品の問題** | 同じ入力と設定で、Linux と結果が違う(例: 時間のずれ、色、フレーム数、終了コード、残ったプロセス) | 原因を調べて最小限に直す。その不具合を捕まえるテストを追加。仕様書(§8.2、§11 など)に理由を追記する |
| **環境の問題** | ffmpeg のバージョン固有、ウイルス対策ソフトによるファイルの掴み、権限、ディスク | 環境の条件を記録する。ffmpeg のバージョン固有なら、どのバージョンから変わったかを調べて記録し、必要なら製品側で吸収する |

してはいけないこと:

- 通すためだけに、許容誤差や期待値を緩める(許容を変えるときは、理由を実測値で示す)。
- 失敗したテストを、理由なく `skip` / `xfail` にする。
- 確認していないことを「確認済み」と記録する。

## 7. Windows での実施結果

**この表を、AI が実行のたびに更新する。** 未実施の項目は空欄のままにせず「未実施」と書く。

実施日: 2026-11-15  / 実施者: Claude Opus 5.5(Claude Code、Windows ネイティブのセッション)

共通の環境: Windows 11 Pro 10.0.26300(日本語、ロケール cp932)、24 論理コア。Python 3.12.0(公式インストーラ、専用の venv)。ffmpeg / ffprobe 8.1.2-essentials_build-www.gyan.dev(**Chocolatey でインストール。PATH 上の `ffmpeg.exe` はシムで、本物は `C:\ProgramData\chocolatey\lib\ffmpeg\tools\ffmpeg\bin\`**)。numpy 2.5.4 / opencv 5.0.0 / PyYAML 6.0.3 / tqdm 4.70.1 / pytest 9.2.0。比較用に、同じ PC の WSL2(Ubuntu 22.04、Python 3.10.12、ffmpeg 4.4.2)でも同じテストを実行した。

| 項目 | Windows のバージョン | Python | ffmpeg | 結果 | 件数・値 | 備考 |
|---|---|---|---|---|---|---|
| W-A1 環境の記録 | 11 Pro 10.0.26300 | 3.12.0 | 8.1.2 | 済 | 上の「共通の環境」 | `pgrep` は PATH になし |
| W-A2 ユニット(302 件) | 同上 | 3.12.0 | 不要 | **合格** | 302 passed(30.7 秒)。修正後は 303 件 | |
| W-A3 全テスト(490 件) | 同上 | 3.12.0 | 8.1.2 | 初回 **23 件失敗** → 修正後 **合格** | 初回: 23 failed, 466 passed, 1 skipped、終了時に Tcl の異常終了。修正後: **491 passed**(145 秒)、Tk のエラー出力 0 件 | 失敗の内訳と対応は §7.1。WSL2(4.4.2)でも修正後に再実行: 490 passed, 1 skipped(Windows 専用のテスト) |
| W-A4 ffmpeg の版違い | 同上 | 3.12.0 | 4.4.1 / 5.1.2 / 6.1.1 / 7.1.1 / 8.1.2 / 9.0.1 | **合格** | 6 つの版すべてで 491 passed(4.4.1: 141 秒、5.1.2: 116 秒、6.1.1: 108 秒、7.1.1: 104 秒、8.1.2: 146 秒、9.0.1: 111 秒。9.0.1 は W-B7 の winget 版) | gyan.dev の essentials ビルド(GitHub の GyanD/codexffmpeg)を `%TEMP%\mk_ffmpeg_versions` に展開し、`FFMPEG_BIN` / `FFPROBE_BIN` で切り替えた(PATH は変更していない)。#1・#2 の挙動は 6.1.1 では起きず、7.1.1 から起きる。5.1.2 は 2026-11-16 に追加で実施 |
| W-A5 Python の版違い | 同上 | 3.12.0 のみ | | **一部実施** | Windows は 3.12.0 のみ(3.10 は未導入)。3.10.12 は WSL2 で全テスト合格 | |
| W-B1 性能 | 同上 | 3.12.0 | 8.1.2 | **済** | jobs=1: 24.1 秒 / jobs=4: 11.6 秒 / jobs=12: 10.1 秒。1920×1080・jobs=12: 21.4 秒 | 22 クリップ、`preset: medium`。目標(5 分以内)を満たす。メモリは未測定。README の表に追記 |
| W-B2 キャンセル・中断 | 同上 | 3.12.0 | 8.1.2 | 初回 **不具合あり** → 修正後 **合格** | (1) API: 1.5〜1.7 秒で中止、ffmpeg 残らず、作業フォルダ空(3 回)。(2) 本物の Ctrl+C(`tests/tools/ctrl_c_windows.py`): 終了コード 130、0.4〜0.7 秒で停止、ffmpeg 残らず、作業フォルダ空(frame 系・ffmpeg 系・`mkaleido.exe` 経由で計 6 回) | 初回は (1) で作業フォルダにセグメントが残り(4/4 回)、(2) で誤った「失敗」の警告と再試行、ffmpeg の取り残しが起きた(終了コードは 130)。§7.1 の #4、#5 |
| W-B3 cp932 の出力 | 同上 | 3.12.0 | 8.1.2 | **合格** | `cmd` で `chcp 932`、`PYTHONIOENCODING` を消して実行: `--list-effects`、`--dump-config`、`-c` での再読込、エラー時、通常の生成のすべてで終了コードは期待どおり(0、存在しない入力は 2)、出力は cp932 として読めて `?` もトレースバックもない。`--dump-config` の往復はバイト単位で一致 | ロケール: `cp932 cp932 utf8_mode=0`。PowerShell のリダイレクトでも終了コード 0 |
| W-B4 日本語・空白のパス | 同上 | 3.12.0 | 8.1.2 | **合格** | 終了コード 0。`結果 1.mp4`(h264 1280×720 + AAC、6.000 秒)と plan が日本語のパスに作られ、作業フォルダは空 | 入力に `〜`(U+301C。cp932 で表せない)を含むファイル名も追加。ユーザー名が日本語、OneDrive、ネットワークドライブは未確認 |
| W-B5 特殊な入力 | 同上 | 3.12.0 | 8.1.2 | **合格** | 全 8 種で終了コード 0、警告なし。形式は §2.4(Linux)と同じ(VFR も 3120/179 fps)。回転メタデータつき(320×240、90°)は 240×320 で出力 | 色: エフェクトなしで描画して素材と比較した平均誤差は 1.1〜2.1(フルレンジ、10bit、回転、音声つき、奇数サイズ。ffmpeg 系・frame 系とも)。`avi`(mpeg4)も追加 |
| W-B6 インストールと起動 | 同上 | 3.12.0 | 8.1.2 | **合格** | 新しい venv に `pip install .`、`mkaleido --version` = 0.1.0、`mkaleido-gui.exe` あり、別のフォルダから生成も成功 | `mkaleido-gui.exe` は PE のサブシステムが GUI(2)で、起動するとウィンドウ「MovieKaleidoscope 0.1.0」が開き、閉じると終了した。コンソールが出ないことの目視、見た目は C1 |
| W-B7 ffmpeg の入手経路 | 同上 | 3.12.0 | 8.1.2 / 9.0.1 | **合格** | Chocolatey(PATH 上のシム)、`FFMPEG_BIN` / `FFPROBE_BIN`(本物の exe)、`--ffmpeg`(`ffprobe` は同じフォルダのものが選ばれる)で、いずれも終了コード 0。見つからないときは終了コード 4 で、導入方法(winget / brew / apt)が表示される。**winget**(`Gyan.FFmpeg.Essentials`、9.0.1、2026-11-16): PATH だけで見つけて生成が終了コード 0(h264 / yuv420p / BT.709 のタグ)、本物の Ctrl+C で 0.2〜0.6 秒で終了コード 130、ffmpeg 残らず(15 → 0)、作業フォルダ空、全テスト 491 passed。**手動の zip**: W-A4 の 5 つの版がこれにあたる(`FFMPEG_BIN` で指定) | winget のこのパッケージは、シムやリンクではなく、展開した `bin` フォルダ(本物の exe)を**ユーザーの PATH の末尾**に追加する。このため #4 の問題は起きない。ただし、Chocolatey の ffmpeg もある PC では、システムの PATH にある Chocolatey のシムが先に見つかる(新しいターミナルで `where.exe ffmpeg` の 1 行目)。確認はそのターミナルだけ PATH の先頭に winget の `bin` を置いて行った。full 版(`Gyan.FFmpeg`)は未確認 |
| C1 GUI | 同上 | 3.12.0 | 8.1.2 | **不具合あり → 修正 → 人の確認 OK** | 利用者の確認で、生成中に空の黒いウィンドウ(Terminal)が大量に開いた。コンソールのない親(pythonw)から 4 秒の動画を作ると、修正前 15 個、修正後 0 個 | §7.1 の #11。見た目と操作は、修正後に利用者が確認して OK(2026-11-16) |
| C2 実際の楽曲(自動で数値を確認) | 同上 | 3.12.0 | 8.1.2 | **合格**(人の確認 OK) | 4 曲。DnB 2 曲は 174.00 BPM(60 秒ごとの検出も 173.97〜174.19 で安定、表拍への当たりは裏拍より高い)。**Introspektro は 142.5 と検出。実際の拍は 95(または 190)と思われる(1.5 倍の取り違え)**。60 秒ごとでは 4 区間中 3 区間が 95.0。**53 分の DJ ミックスは、冒頭 10 分に拍がなく検出に失敗(仕様どおり固定長にフォールバック)**、区間によってテンポが 171〜176 で変わる。実際の生成(Kanine、148 クリップ): 全カットが拍から半フレーム以内(最大 17.1 ms)、122 / 147 が画面上のカットとして検出された | 「音楽に合っている感じ」は利用者が確認して OK(2026-11-16)。1.5 倍の取り違えは README で `--bpm` を案内。素材は `C:\work\radio\らじれこ\MUSIC` |
| C3 エフェクトの見た目(実際の映像) | 同上 | 3.12.0 | 8.1.2 | **合格**(修正後、人の確認 OK) | 15 種を 1 つずつ実際の映像で生成し、コマ見本で確認。**`negate` は反転の強さが 0.5 前後で、画面全体がほぼ一様な灰色になる**(元と反転の平均は常に約 128。9 コマ中 5 コマ)→ 修正(#13)。修正後は同じ seed で灰色のコマ 0。`kaleidoscope`、`tile`、`mirror`、`solarize`、`rgbshift`、`hue` ははっきり効く。`posterize`、`zoom`、`feedback` は弱め | 修正後の見た目を利用者が確認して OK(2026-11-16) |
| C4 再生互換性(形式の確認のみ) | 同上 | 3.12.0 | 8.1.2 | **合格**(人の確認 OK) | H.264 High、yuv420p、BT.709 のタグ、AAC LC、`moov` がファイルの先頭(faststart)、キーフレームの間隔は最大 1.4 秒、全フレームのデコードでエラーなし。**ただし、4K の素材が最初に来ると出力も 4K(レベル 5.1)になり、202 秒で 1.7 GB(約 69 Mbps)** → 既定を最大 1080p に変更(#14)。同じ条件で 1920×1080、672 MB(約 28 Mbps) | 実際の再生は利用者が確認して OK(2026-11-16) |
| C5 長い動画・大量のクリップ | 同上 | 3.12.0 | 8.1.2 | **一部に課題** | ダミー素材: 5 分・431 クリップで 45 秒、ffmpeg を含むメモリのピーク 2.1 GB。出力は 9000 フレーム・映像と音声とも 300.000 秒。**実際の素材(4K 60p を含む)で 202 秒の 4K 出力: 323 秒、メモリのピーク 17.2 GB(搭載 32 GB)** → 修正(#14)後、同じコマンドで 1080p 出力・並列数 7(空きメモリから自動)、**109 秒、ピーク 4.9 GB** | 修正前は、4K の素材では 16 GB の PC で足りない見込みだった |
| C6 VFR の出力 FPS | 同上 | 3.12.0 | 8.1.2 | **決定・実装済み** | 近い標準の FPS に丸める。VFR 素材(3120/179)で 15/1・45 フレーム(3 秒)、終了コード 0。全テスト 503 passed(Windows)、WSL2(4.4.2)で 502 passed・1 skipped | ユニットテスト 12 件を追加(`test_inputs.py`) |

### 7.1 見つかった問題と、その分類・対応

| # | テスト・項目 | 症状 | 分類 | 対応 | 追加したテスト |
|---|---|---|---|---|---|
| 1 | `test_color.py::test_colours_are_preserved[ffmpeg-untagged_hd]` ほか、ffmpeg 系の経路の色 | タグのない HD 素材の色がずれる(平均誤差 9.97、許容 3)。ffmpeg 7.1.1 と 8.1.2 で起きる(4.4.1、4.4.2、6.1.1 では起きない) | **製品**(ffmpeg の版) | 新しい ffmpeg はフィルタグラフ全体で色の属性をネゴシエーションし、エンコーダの BT.709 のタグに合わせて、タグのない入力を BT.601 と見なして変換していた。先頭の `scale` に `in_color_matrix` / `out_color_matrix` を明示した(`render/ffmpeg.py`)。SPEC §3.3 | 既存のテストが捕まえる(8.1.2 で失敗、修正後に合格) |
| 2 | `test_color.py::test_every_segment_carries_the_same_colour_tags` | 出力に `color_primaries` / `color_transfer` のタグがない(`KeyError`)。ffmpeg 7.1.1 と 8.1.2 で起きる(4.4.1、6.1.1 では起きない) | **製品**(ffmpeg の版) | 8.1.2 は、フレームの属性が未指定だとエンコーダの `-color_primaries` / `-color_trc` を無視する。フィルタの最後に `setparams=...` を置き、フレームにもタグを付けた(セグメントと frame 系のエンコーダの両方)。SPEC §3.3 | 既存のテストが捕まえる |
| 3 | (#1 の修正で)`test_effects.py::test_rgbshift_frame_and_ffmpeg_backends_agree_on_direction` が **4.4.2 で**失敗 | `rgbashift`(RGB でしか動かない)の前後の自動変換が、フレームのタグから行列を選ぶため、片方向だけ BT.709 になり色がずれた | **製品**(ffmpeg の版) | `rgbashift` の前後の RGB 変換を明示した(`effects/rgbshift.py`)。SPEC §3.3 | 既存のテストが捕まえる(4.4.2)。ユニットテストの期待値(フィルタ文字列)を更新 |
| 4 | W-B2 (1) キャンセル後に作業フォルダが残る(4/4 回) | `seg_*.mp4` が「別のプロセスが使用中」で削除できず、`rmtree(ignore_errors=True)` が黙って無視していた | **製品**(Windows と ffmpeg の入れ方) | PATH 上の `ffmpeg.exe` が Chocolatey のシムで、本物の ffmpeg はその子プロセス。`terminate()` はシムしか止めず、本物の ffmpeg が動き続けてファイルを開いていた(最初は「終了直後の一時的なロック」と見誤った。`preset: ultrafast` では再現しなかった。処理中のフレームが少なく、取り残された ffmpeg がすぐ終わるためと思われる(未検証))。Windows では `taskkill /T /F` でプロセスツリーごと終了するようにし(`util/process.py` の `terminate`、frame 系も共通化)、作業フォルダが消せないときは警告を出すようにした。SPEC §8.4、§11 | `test_planner.py::test_cancel_stops_the_real_program_behind_a_launcher`(ランチャーの子がファイルを開いたまま残らないこと。Windows のみ)。`test_effects.py::test_cancel_during...` を全 OS で動くようにし(pgrep の代わりに起動したプロセスを記録)、`preset: veryfast` と作業フォルダが空であることの確認を追加。どちらも修正前のコードで失敗することを確認 |
| 5 | W-B2 (2) Ctrl+C(複数の入力、ffmpeg 系のエフェクト) | 3 回中 2 回、「書き出しに失敗しました … 別の入力で 1 回だけやり直します」の警告が出て、再試行の ffmpeg が起動された。停止に約 5 秒、作業フォルダが残った。終了コードは 130 | **製品**(Windows) | Ctrl+C が ffmpeg にも届き、メインスレッドが KeyboardInterrupt を受け取る(最大 0.2 秒後)より先に、ワーカーが ffmpeg の異常終了を失敗と見なしていた。Windows では ffmpeg を `CREATE_NEW_PROCESS_GROUP` で起動し、Ctrl+C を受けるのを Python だけにした(子プロセスはこちらで止める)。SPEC §11 | 自動テストはなし(別のコンソールが必要)。`tests/tools/ctrl_c_windows.py` で手順化し、修正後に 6 回確認 |
| 6 | `test_effects.py::test_pipe_equals_processor_applied_to_the_reference` の 20 件、`test_tile_scroll_follows_the_clock` | frame 系の経路と基準の画素がずれる | **テスト**(OpenCV のビルドの違い) | テストが出力を `cv2.VideoCapture` で読んでいた。Windows 版の opencv-python 5.0.0 は、タグに関係なく常に BT.601 で RGB にする(Linux 版はタグに従い BT.709)。素材の色域外の色を frame 系は RGB でクリップし、ffmpeg 系は保持するため、BT.601 で読むとその差が大きく見えた。テストでは ffmpeg で BT.709 を明示して読むようにした(出力は常に BT.709)。許容値は変えていない | — |
| 7 | `test_media.py::test_probe_applies_rotation` | 回転したはずのダミー素材が回転していない | **テスト**(ffmpeg の版) | 8.1.2 は `-metadata:s:v:0 rotate=90` を無視する(4.4.2 は表示行列に変換していた)。`make_dummy.py` で `-display_rotation` を使い、ない版(4.4)では従来の方法にした。製品の読み取り(表示行列)は正しかった | — |
| 8 | `test_gui_app.py` の 2 件、`test_gui_runner.py` の 2 件 | `"/a.mp4"` が `C:\a.mp4` になって戻る | **テスト**(§3.3 の予測どおり) | Windows ではドライブ文字のない `/a.mp4` はドライブ相対のパスで、設定ファイルの基準で解決されるのは正しい動作。どの OS でも絶対パスになる `Path(Path.cwd().anchor)` から組み立てた | — |
| 9 | `test_gui_app.py::test_closing_during_a_job_asks_first`、大量の `Variable.__del__ … main thread is not in main loop`、全テストの最後に `Tcl_AsyncDelete: async handler deleted by the wrong thread` で Python が異常終了 | 前のテストのウィンドウの Tk オブジェクトが、後のテストのジョブのスレッドでガベージコレクトされ、Tcl の呼び出しが 1 回ごとに約 1 秒待ってから失敗していた(ジョブの中止が 15 秒で終わらない)。インタプリタの解放が別スレッドで起きると異常終了する | **テスト**(Windows で顕在化) | `root` フィクスチャの後始末で、メインスレッドで `gc.collect()` する。製品(GUI は 1 つのウィンドウだけを使う)では起きない想定だが、未確認 | — |
| 10 | `test_gui_app.py` の 2 番目のテスト(W-A4 の実行中に気づいた) | 約半分の実行で、2 回目の `tk.Tk()` が「Can't find a usable tk.tcl」「invalid command name "tcl_findLibrary"」で失敗し、「no display」としてスキップされた(失敗にはならないので見逃しやすい)。#9 の修正の前から起きていた(`gc.collect()` を無効にすると 8 回中 7 回) | **テスト**(Windows の Tcl/Tk と pytest) | pytest の出力キャプチャ(fd の差し替え)が有効なときだけ起きる(`-s` では 8 回中 0 回、pytest の外では 0 回)。Tk を作る瞬間だけ `capsys.disabled()` でキャプチャを止めた。修正後は 12 回中 0 回 | — |
| 11 | C1(2026-11-16、利用者の確認) | GUI から生成すると、ffprobe・ffmpeg・taskkill のたびに空の黒いウィンドウ(Windows Terminal)が開く | **製品**(Windows) | GUI(pythonw)にはコンソールがないため、コンソールプログラムの子プロセスごとに新しいコンソールが作られていた。すべての子プロセスを `CREATE_NO_WINDOW` で起動する(`util/process.py`)。Chocolatey のシム経由でも 0 個になることを確認 | `test_no_console_window.py`(コンソールのない親から起動した子に、コンソールのウィンドウがないこと。修正前のコードで失敗することを確認) |
| 12 | CI(Linux)の `test_effects.py` の `reverse` の 3 件 | `reverse` の断片のフレームが足りない(60 → 51)。ffmpeg 7.0.2(静的ビルド)で起きる。4.4.2、7.1.1、8.1.2、9.0.1 では起きない | **製品**(ffmpeg の版) | `reverse` の後ではフィルタグラフが出力のフレームレートを伝えず、出力側の CFR の処理が 25 fps と見なして 6 フレームに 1 枚を捨てていた(`-v verbose` で `dropping frame`)。フィルタだけ(`framecrc`)なら 60 枚。セグメントと frame 系のデコードに `-r <出力 FPS>` を明示した(`render/ffmpeg.py`)。SPEC §8 | 既存のテストが捕まえる(7.0.2 で失敗、修正後に全テスト合格) |
| 13 | C3 `negate` | 反転の強さが中間のとき、画面がほぼ一様な灰色になる | **製品**(見た目の設計) | 元と反転を混ぜると、混ぜ具合 m でコントラストが \|1 − 2m\| 倍になり、0.5 で 0 になる。使う値を 0〜0.2 と 0.8〜1 に写し、中間は瞬時に切り替える(コントラストを 60% 以上残す)。利用者の判断(2026-11-16)。SPEC §6 | `test_frame_effects.py::test_negate` を書き換え(以前は 0.5 で灰色になることを確認していた) |
| 14 | C4・C5 4K の素材 | 出力が 4K になり、ファイルが大きく(1.7 GB / 202 秒)、生成中のメモリが 17 GB | **製品**(既定値) | 利用者の判断(2026-11-16)で、既定の解像度を最大 1080p に(`--width/--height` で 4K も可)、既定の並列数を空きメモリの 70% に収まる数に(`render/pipeline.py`、`util/memory.py`)。SPEC §3.3、§8 | `test_inputs.py`(1080p の上限、指定時は 4K)、`test_auto_jobs.py`(見積もりが実測と合う、16 GB の PC で減る、最小 1) |
| 15 | CI(macOS)の「All tests」が何時間も終わらない | `test_gui_app.py::test_generate_from_the_window` の `root.update()` が戻らない(macOS 単独で実行すると合格。同じファイルの前のテストに続けて実行すると止まる) | **テスト**(macOS の Tk) | macOS(Aqua Tk 8.6.16)では、ウィンドウを何度か作ったあと、ネイティブのイベントが途切れず届き続けた(1 回の呼び出しで 2000 件超。Tcl のタイマーはアプリの 100 ms の取り込みだけ)。`update()` は「待ちのイベントがなくなるまで」処理するので終わらない。テストのイベント処理を 1 回 2000 件までにした(`process_events()`)。Tk を 1 つにまとめる案は効果がなかった。アプリ本体の `mainloop()` には「なくなるまで」の処理がないので固まらない(macOS の実機で、ジョブ中に CPU を余計に使うかは未確認)。CI に 1 テスト 5 分・1 ジョブ 30 分の制限を追加 | — |

### 7.2 未解決・持ち越し

- **ffmpeg の入れ方**: winget の full 版(`Gyan.FFmpeg`)と、Scoop など他のシムを使う入れ方は未確認(`taskkill /T` はどのランチャーでも子を止める)。Chocolatey と winget の両方で入れた PC では Chocolatey のシムが使われる(W-B7)。
- **Linux の Ctrl+C**: Linux でも Ctrl+C はフォアグラウンドのプロセスグループ全体に届くので、#5 と同じ競合が理論上はありうる(メインスレッドがすぐに割り込まれるので、起きにくい)。未確認・未対策。
- **連結(concat)中のキャンセル**: `.part` の削除が失敗した場合の挙動は未確認(#4 の修正で ffmpeg は確実に止まるので、起きにくい見込み)。
- **C1〜C5**: 2026-11-16 に自動で確認できる範囲を実施(§7 の表)。見た目と実際の再生も、利用者の確認で OK(2026-11-16)。`negate` の灰色、4K でのメモリとファイルサイズ、CI の Linux での `reverse` は修正済み(#12〜#14)。Introspektro のような拍の取り違え(1.5 倍)は、検出は変えず README で `--bpm` の指定を案内する(利用者の判断)。材料:
  - C1: `%TEMP%\mk_venv\Scripts\mkaleido-gui.exe`(W-B6 で作った venv)を起動して見る。GUI のサブシステムで、ウィンドウが開くことは確認済み。
  - C3 / C4: `%TEMP%\mk_dummy\perf.mp4`(720p・60 秒・22 クリップ、全エフェクト)、`%TEMP%\mk_dummy\perf1080.mp4`(1080p)、`%TEMP%\mk テスト 日本語\出力 フォルダ\結果 1.mp4`(音声つき)。どれもダミー素材(テストパターン)なので、実際の素材での確認が別に必要。
  - W-B5 の回転: `%TEMP%\mk_edge\rotated_out.png` が 240×320 の縦長で、テストパターンが 90° 回っている。回転の向きがプレーヤーの表示と一致するかは、元の `%TEMP%\mk_edge\rotated.mp4` をプレーヤーで見て比べる。
- **製品の GUI での #9**: 終了時に `close()` がジョブの終了を最大 15 秒待つ間にメインループが止まるので、その間に Tk オブジェクトがジョブのスレッドで解放されると同じことが起きうる。現状の GUI では起きない想定(未確認)。
- **NVENC の同時セッション数上限(2026-11-22)**: このPC・この GPU では上限が 10〜13 ジョブの間だったが、他の機種・ドライバでの上限は未確認。`pipeline._NvencGate` は render 1 回ごとに上限 1 から再試行するので、同時実行数が極端に少ない GPU(あるいは複数ジョブを同時に起動できない設定)でも最終的には動く想定だが、実機は未確認。

## 8. 更新履歴

| 日付 | 内容 |
|---|---|
| 2026-11-15 | 作成。WSL2(Linux)での結果(490 件合格)と、Windows で確認すべき項目を記録 |
| 2026-11-15 | Windows ネイティブ(Python 3.12.0、ffmpeg 8.1.2)で W-A1〜W-B7 を実施(§7)。製品の不具合 5 件(色 3 件、Windows のプロセス 2 件)とテストの問題 4 件を修正し、Windows で 491 件合格、WSL2(4.4.2)で 490 件合格・1 件スキップ。`tests/tools/ctrl_c_windows.py` を追加 |
| 2026-11-15 | W-A4 を実施: Windows で ffmpeg 4.4.1 / 6.1.1 / 7.1.1 / 8.1.2 のすべてで 491 件合格。#1・#2 は 7.1 からの変化と特定。GUI のテストが Windows で約半分の確率でスキップされる問題(#10)を修正 |
| 2026-11-16 | ffmpeg 5.1.2(手動の zip)と、winget で入れた 9.0.1 で、全テスト 491 件合格。W-B7 の winget の確認(生成、Ctrl+C、ffmpeg が見つからないとき)を実施し、合格。製品の変更なし |
| 2026-11-16 | C6 を決定(既定の FPS を近い標準の値に丸める)し、実装・テスト追加。CI の設定ファイルの YAML の誤り(引用符のない `: `)を直し、初めて CI を実行 |
| 2026-11-16 | C1 の黒いウィンドウを修正(#11)。C2〜C5 を、実際の素材を使って自動で確認できる範囲で実施 |
| 2026-11-16 | CI の Linux の `reverse`(ffmpeg 7.0.2、#12)、`negate` の灰色(#13)、4K の既定(#14)を修正。Windows で 518 件合格 |
| 2026-11-16 | 人の確認(C1〜C4)がすべて OK。これで、このプロジェクトの確認項目はすべて完了 |
| 2026-11-16 | CI の macOS が止まる問題(#15)を、GUI のテストのイベント処理の上限で解決。CI の 6 ジョブ(Windows / Linux / macOS × Python 3.10 / 3.12)すべて合格 |
| 2026-11-22 | GPU(NVENC)エンコード・電源プランの自動切替・frame バックエンドのパイプライン化を追加(4337a72。§1〜§7 の「すべて完了」は、この変更より前の時点のもの)。README の性能数値を Windows ネイティブで再実測し、ほぼ変化なしと確認。追加で NVENC 版を実測したところ、既定のジョブ数(論理コア数の 3/4、このPCでは 18)で `h264_nvenc` が `Could not open encoder before EOF` を返し、**レンダリング全体が失敗する**不具合を発見(GPU の同時 NVENC セッション数の上限は機種・ドライバ依存で事前に分からない。この PC では 10 ジョブは成功、14 ジョブは失敗)。該当クリップをその場で再試行しつつ同時実行数を 1 ずつ下げる自動縮退(`pipeline._NvencGate`)を実装して修正し、同じ既定値(18)でも 12 まで自動で下がって成功することを確認した。あわせて、`config.example.yaml` に抜けていた `video.backend` / `runtime.power_plan` と、`SPEC.md` が参照していたが存在しなかった「GPU redesign §A/§D」節を追加した |
