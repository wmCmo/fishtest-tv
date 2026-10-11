# fishtest-tv

[Fishtest](https://tests.stockfishchess.org/) のワーカーを動かしている Windows PC 用の、モニター・全画面 TV・スマホのリモコン。
ワーカー本体（`worker.py`）には手を入れず、ログと Fishtest の公開 API を読むだけ。

*A local dashboard, an across-the-room "TV" view with AI football-style commentary, and a phone remote for a Fishtest worker on Windows.*

## できること

- **モニター**（`http://127.0.0.1:8787/`）：ワーカーの状態、いまのバッチの進み具合と戦績、API の応答時間、対局ビューア、テスト用フォルダの容量
- **貢献カード（トロフィー）**：この PC が対局したテストのうち、Stockfish 本体に取り込まれたもの・合格したもの・判定待ち・不合格の数と CPU 時間
- **TV**（`/tv`）：部屋の向こうから眺める全画面の盤面。いつも「パッチ側」から見た向きで表示し、全局流し終えたら次の対局を待つ
  - [Ollama](https://ollama.com/) があれば、対局をサッカー実況風に読み上げる（Edge の英語音声）
  - テスト対象のコード差分から、テストの中身を説明するスカウティングレポートを作る
- **スマホのリモコン**（`/remote`）：ワーカーの開始・穏やかな停止、TV のオン・オフ、進み具合の確認。PIN 付き

## 必要なもの

- Windows、PowerShell 5.1 か 7
- MSYS2（`C:\msys64`）で動く Fishtest ワーカー
- Python 3.10 以上（標準ライブラリだけ使う）
- 任意：Ollama（既定のモデルは `qwen3:8b`）、Microsoft Edge（TV 用）、Tailscale（外出先からリモコンを使う場合）

## 使い方

ワーカーの場所が `C:\fishtest\worker` でなければ、環境変数 `FISHTEST_WORKER` で指定する。PowerShell のプロファイルに関数を登録しておくと便利:

```powershell
function fishtest { & "C:\path\to\fishtest-tv\start-fishtest.ps1" @args }
```

| コマンド | 動き |
|---|---|
| `fishtest` | ワーカー・モニター・TV をまとめて起動（動いているものはそのまま）。Ollama のモデルも更新する |
| `fishtest worker` | ワーカーとモニターだけ |
| `fishtest tv` / `fishtest tvoff` | TV を開く / 閉じる |
| `fishtest stop` | ワーカーを穏やかに止める（`fish.exit` を置き、終わった対局を送ってから止まる） |
| `fishtest remote` | スマホで開く URL と PIN を表示する |

モニターだけを手で動かす場合:

```powershell
python monitor.py --host 0.0.0.0 --worker C:\fishtest\worker
```

## 設定（環境変数）

| 変数 | 既定 | 意味 |
|---|---|---|
| `FISHTEST_WORKER` | `C:\fishtest\worker` | ワーカー本体のフォルダ |
| `FISHTEST_PYTHON` | PATH 上の `python` | モニターを動かす Python |
| `MSYS2_ROOT` | `C:\msys64` | MSYS2 の場所 |
| `NARRATOR_MODEL` / `BRIEF_MODEL` | `qwen3:8b` | 実況・スカウティングレポートに使う Ollama のモデル |

## セキュリティ

`start-fishtest.ps1` はスマホから開けるよう、モニターを全インターフェース（`0.0.0.0`）で起動する。

- この PC（127.0.0.1）以外からは、どのページも PIN でのログインが要る。PIN は初回起動時に `tv-cache\remote-pin.txt` に作られる。消して再起動すると作り直され、全端末がログアウトする
- PIN を 5 回間違えると 10 分締め出す
- 操作ボタンの POST は独自ヘッダーが必須で、ほかのサイトから押させることはできない。Host ヘッダーは IP・localhost・`*.ts.net` だけ受け付ける
- 通信は暗号化されない（http）。ルーターでポートを開けて公開しないこと。外出先から使うなら Tailscale を使い、Windows ファイアウォールでポート 8787 を Tailscale のインターフェースにだけ許可する

## スマホのホーム画面に入れる（PWA）

リモコンはホーム画面アプリとして入れられる（アドレスバーのない独立したウィンドウで開く）。
ブラウザがアプリとして入れてくれるのは HTTPS のページだけなので、Tailscale の HTTPS を使う:

```powershell
tailscale serve --bg 8787
```

表示された `https://<PC の名前>.<tailnet>.ts.net/remote` をスマホで開き、ブラウザのメニューから「ホーム画面に追加」を選ぶ。
`tailscale serve` 経由の接続も PIN が要る（PC 自身からの接続とは見なさない）。やめるときは `tailscale serve reset`。

`http://` の URL でもホーム画面には追加できるが、その場合はブラウザのタブで開くショートカットになる。

アイコンは `python tools/make_icons.py` で作り直せる（Pillow が要る）。

## データ

実行時のデータは `tv-cache\`（Git の管理外）に置く。

- `ledger.json`：この PC が担当したテストとタスクの記録。ワーカーのログは数週間で上書きされるので、**貢献の履歴はこのファイルにしか残らない**。消さないこと
- `runs\`・`tasks\`・`<run>.json`：Fishtest の API とスカウティングレポートのキャッシュ（消しても取り直す）
- `stockfish.git`：取り込み判定用の Stockfish の履歴（1 日 1 回更新）

## ライセンス

MIT（[LICENSE](LICENSE)）。ただし `vendor/chess.js` は [chess.js](https://github.com/jhlywa/chess.js)（BSD 2-Clause、著作権表示はファイル冒頭）。
