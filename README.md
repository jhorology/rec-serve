# rec-serve

Recursive の `rec` (Flint 用 LLM エンジン) を **常駐運用** し、**外部から接続できるようにする** 薄いラッパです。

| 項目 | 内容 |
| :--- | :--- |
| 本体 | `rec serve` (MLX / Flint。`127.0.0.1` 固定・Bearer 認証必須) |
| 公開 API | OpenAI 互換 (`:11501` → rec `127.0.0.1:11500` へ転送) |
| 常駐 | macOS **LaunchAgent** (既定) または **LaunchDaemon** (`--daemon`)。`KeepAlive` + `RunAtLoad` |
| 接続範囲 | localhost / Tailscale IPv4 のみ / 全インターフェースから選択 |
| API キー | クライアント側は不要 (任意の文字列で可)。プロキシが `rec auth token` のキーを付与 |
| スリープ抑制 | 既定は API アクセス時に 1 時間延長。`--no-keepalive` で無効化可能 |

`rec serve` にはバインド先を指定するオプションがなく、`127.0.0.1` でしか待ち受けません。
そこで前面に FastAPI プロキシを置き、指定したインターフェースで待ち受けて rec に転送します。

## セットアップ

```bash
cd ~/.local/share/rec-serve
mise install
mise exec -- uv sync
./run.sh --check
```

事前に `rec doctor` / `rec pull` でモデルを取得しておいてください。

### 常時起動サーバーとして使う (Tailscale 限定)

```bash
./service.sh install --tailscale          # LaunchAgent (ログイン中に常駐)
./service.sh install --daemon --tailscale # LaunchDaemon (ブート時から常駐、sudo が必要)
./service.sh status
```

`install` は既存サービスを停止してから登録・起動します。以後は自動起動し、
`./service.sh restart` でもバインド先・方式 (agent / daemon)・スリープ抑制の設定を継承します。
片方の方式で install すると、もう片方の登録は自動で解除されます。

### LaunchAgent と LaunchDaemon

| 方式 | plist | 起動タイミング | 備考 |
| :--- | :--- | :--- | :--- |
| `--agent` (既定) | `~/Library/LaunchAgents/com.user.rec-serve.plist` | ログイン時 | sudo 不要 |
| `--daemon` | `/Library/LaunchDaemons/com.user.rec-serve.plist` | ブート時 (ログイン不要) | `install` / `stop` / `restart` / `uninstall` に sudo が必要 |

LaunchDaemon は `UserName` で **現在のユーザ** として実行します
(モデル・API キーが `~/.local/share/rec` にあるため)。
FileVault 有効時は起動後に一度ログインするまでホームディレクトリが復号されないため、
実質的にはログイン後に起動します。

## 日常運用

```bash
./service.sh start              # バックグラウンド起動 (launchd なし)
./service.sh restart            # 保存した設定で再起動
./service.sh status
./service.sh log -f
./service.sh stop               # launchd 登録済みなら一時 bootout して停止

./service.sh install [--daemon] # 自動常駐を登録 + 起動
./service.sh uninstall          # 自動起動解除
```

### 公開範囲を選ぶ

| オプション | 待ち受け先 | 用途 |
| :--- | :--- | :--- |
| `--localhost` | `127.0.0.1` | 同じ Mac からのみ接続 |
| `--tailscale` | `tailscale ip -4` で取得した IPv4 | 外部アクセスを Tailscale に限定 |
| `--public` | `0.0.0.0` | LAN を含む全 IPv4 インターフェースに公開 |

`start` / `restart` / `install` で指定できます。優先順位は、上記オプション →
`REC_SERVE_HOST` → 登録済み plist の設定 → `127.0.0.1` です。

`--tailscale` は Tailscale IPv4 のみにバインドします。同じ Mac からも
Tailscale IP の URL を使ってください (rec 本体の `127.0.0.1:11500` は API キー付きで直接利用可)。
Tailscale IP が変わった場合は `./service.sh install --tailscale` で更新します。

> **注意**: プロキシが rec の API キーを付与するため、公開先から到達できる端末は
> キーなしで利用できます。アクセス制御は Tailscale の ACL などで行ってください。

## API

| 設定 | 値 |
| :--- | :--- |
| Base URL | `http://<サーバーの Tailscale IP>:11501/v1` |
| API キー | `rec-serve` など任意の空でない文字列 (キー必須のクライアント向け) |
| モデル | `flint` |

```bash
export REC_SERVE_URL="http://$(tailscale ip -4):11501"   # サーバー自身で確認する例
curl --noproxy '*' -fsS "$REC_SERVE_URL/health" | python3 -m json.tool
curl --noproxy '*' -fsS "$REC_SERVE_URL/v1/models" | python3 -m json.tool
curl --noproxy '*' -fsS "$REC_SERVE_URL/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"flint","messages":[{"role":"user","content":"Hello"}]}'
```

| エンドポイント | 内容 |
| :--- | :--- |
| `GET /health` | プロキシ + rec (`upstream`: `status` / `busy` / `queued`) + keepalive |
| その他すべて | rec にそのまま転送 (`/v1/models`, `/v1/chat/completions` など。stream 対応) |

## 構成

```
クライアント ──► rec-serve (公開 :11501) ──proxy + API キー付与 + keepalive──► rec serve (127.0.0.1:11500)
                     ▲                                                             └ _supervise → rec-mlx
            launchd (LaunchAgent / LaunchDaemon, KeepAlive)
```

rec はプロキシの子プロセスとして起動します。rec が終了するとプロキシも終了し、
launchd の `KeepAlive` で両方とも再起動されます。
rec のポートを既定の 11500 にしているので、同じ Mac 上の `rec status` や
`rec launch pi|opencode` は常駐中の rec をそのまま使えます。
ただし `rec stop` を実行すると rec が停止し、launchd により再起動されます
(止めたい場合は `./service.sh stop`)。

## 環境変数

| 変数 | 既定 | 意味 |
| :--- | :--- | :--- |
| `REC_SERVE_HOST` | `127.0.0.1` | 公開バインド |
| `REC_SERVE_PORT` | `11501` | 公開ポート |
| `REC_SERVE_BACKEND_PORT` | `11500` | rec serve のポート (`127.0.0.1`) |
| `REC_SERVE_CONTEXT` | (release 既定) | `rec serve --context` |
| `REC_SERVE_BACKEND_TIMEOUT` | `300` | rec の読み込み待ち秒数 (プロキシ) |
| `REC_SERVE_KEEPALIVE_TIMEOUT` | `3600` | スリープ抑制のアイドル秒 |
| `REC_BIN` | PATH 上の `rec` | rec バイナリ |
| `REC_SERVE_LOAD_TIMEOUT` | `300` | `service.sh` の起動待機秒数 |
| `REC_SERVE_LOGS_DIR` | `~/Library/Logs/rec-serve` | ログ出力先 |

## トラブルシュート

```bash
./service.sh status
./service.sh log -f
lsof -nP -iTCP:11501 -sTCP:LISTEN   # 公開側
lsof -nP -iTCP:11500 -sTCP:LISTEN   # rec 本体
```

| 状況 | 確認・対処 |
| :--- | :--- |
| `127.0.0.1:11500 は既に使用中` でクラッシュループ | 手動の `rec serve` / `rec launch` が動いている。`rec stop` で停止 |
| 起動待機がタイムアウト | 初回はモデル読み込みに時間がかかる。`./service.sh log -f` で `flint is ready` を確認 |
| `--tailscale` で IP を取得できない | `tailscale` が PATH にあるか、Tailscale に接続済みか確認 |
| 別の端末から接続できない | 接続元の Tailscale 接続・ACL と、サーバーの `status` を確認 |
