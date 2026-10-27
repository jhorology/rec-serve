#!/usr/bin/env bash
# ==============================================================================
# service.sh - rec-serve lifecycle + launchd (LaunchAgent / LaunchDaemon)
# ==============================================================================
# Recursive の rec (Flint) を常駐運用する。
# `rec serve` は 127.0.0.1 にしかバインドできないため、前面に keepalive 付き
# プロキシを置いて任意のインターフェース (Tailscale IP など) に公開する。
#
# 常駐方式:
#   LaunchAgent  (既定)    ~/Library/LaunchAgents   ログイン中に常駐
#   LaunchDaemon (--daemon) /Library/LaunchDaemons  ブート時から常駐 (要 sudo)
#                            UserName で現在のユーザとして実行する
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

LOGS_DIR="${REC_SERVE_LOGS_DIR:-$HOME/Library/Logs/rec-serve}"
LOG_FILE="$LOGS_DIR/service.log"
LOCAL_LOGS_DIR="$SCRIPT_DIR/logs"
PID_FILE="$SCRIPT_DIR/.service.pid"
HOST_FILE="$SCRIPT_DIR/.service.host"
DEFAULT_PORT="${REC_SERVE_PORT:-11501}"
BACKEND_PORT="${REC_SERVE_BACKEND_PORT:-11500}"
BIND_HOST="${REC_SERVE_HOST:-127.0.0.1}"
PLIST_LABEL="com.user.rec-serve"
AGENT_PLIST="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
DAEMON_PLIST="/Library/LaunchDaemons/${PLIST_LABEL}.plist"
LAUNCHER_BIN="$HOME/.local/bin/rec-serve"
LOAD_TIMEOUT="${REC_SERVE_LOAD_TIMEOUT:-300}"
REC_BIN="${REC_BIN:-$(command -v rec || echo "$HOME/.local/bin/rec")}"
KEEPALIVE_MODE=""
BIND_MODE=""
SERVICE_MODE=""

# 登録済みの方式 (daemon 優先) を検出する。
installed_plist() {
  if [[ -f "$DAEMON_PLIST" ]]; then
    echo "$DAEMON_PLIST"
  elif [[ -f "$AGENT_PLIST" ]]; then
    echo "$AGENT_PLIST"
  fi
}

plist_get() {
  local plist
  plist=$(installed_plist)
  [[ -n "$plist" ]] && /usr/libexec/PlistBuddy -c "Print $1" "$plist" 2>/dev/null
}

# 明示指定がなければ常駐設定のバインド先を継承する。
if [[ -z "${REC_SERVE_HOST:-}" ]]; then
  BIND_HOST=$(plist_get ':EnvironmentVariables:REC_SERVE_HOST' || echo "127.0.0.1")
fi

mkdir -p "$LOGS_DIR" "$LOCAL_LOGS_DIR"
# ローカルの logs/service.log からも参照できるようにシンボリックリンクを作成
if [[ ! -e "$LOCAL_LOGS_DIR/service.log" ]]; then
  ln -sf "$LOG_FILE" "$LOCAL_LOGS_DIR/service.log" 2>/dev/null || true
fi

is_public() { [[ "$1" == "0.0.0.0" || "$1" == "::" ]]; }

client_host() {
  case "$1" in
  0.0.0.0) echo "127.0.0.1" ;;
  ::) echo "[::1]" ;;
  *:*) echo "[$1]" ;;
  *) echo "$1" ;;
  esac
}

resolve_tailscale_host() {
  local ip
  if ! command -v tailscale >/dev/null 2>&1; then
    echo "❌ tailscale CLI が見つかりません" >&2
    return 1
  fi
  if ! ip=$(tailscale ip -4) || [[ -z "$ip" ]]; then
    echo "❌ Tailscale IPv4 を取得できません。Tailscale の接続状態を確認してください" >&2
    return 1
  fi
  # 空文字や複数行などをバインド先として渡さない。
  python3 -c 'import ipaddress, sys; ip = ipaddress.IPv4Address(sys.argv[1]); assert ip in ipaddress.IPv4Network("100.64.0.0/10"); print(ip)' "$ip"
}

reachable_addrs() {
  local iface ip
  for iface in $(ifconfig -l); do
    [[ "$iface" == en* ]] || continue
    ip=$(ipconfig getifaddr "$iface" 2>/dev/null || true)
    [[ -n "$ip" ]] && echo "LAN($iface) $ip"
  done
  if command -v tailscale >/dev/null 2>&1; then
    ip=$(tailscale ip -4 2>/dev/null | head -n 1 || true)
    [[ -n "$ip" ]] && echo "Tailscale $ip"
  fi
  return 0
}

rotate_log_if_needed() {
  mkdir -p "$LOGS_DIR"
  if [[ -f "$LOG_FILE" ]]; then
    local size
    size=$(stat -f%z "$LOG_FILE" 2>/dev/null || echo 0)
    if ((size > 10 * 1024 * 1024)); then
      mv "$LOG_FILE" "${LOG_FILE}.1"
    fi
  fi
}

get_server_pid() {
  local pid port_pid
  pid=$(cat "$PID_FILE" 2>/dev/null || true)
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "$pid"
    return 0
  fi
  port_pid=$(lsof -ti tcp:"$DEFAULT_PORT" -sTCP:LISTEN 2>/dev/null | head -n 1 || true)
  if [[ -n "$port_pid" ]]; then
    echo "$port_pid" >"$PID_FILE"
    echo "$port_pid"
    return 0
  fi
  return 1
}

wait_for_health() {
  local timeout="${1:-$LOAD_TIMEOUT}" start
  start=$(date +%s)
  echo -n ">> 起動待機 (モデル読み込み)"
  until curl --noproxy '*' --connect-timeout 2 --max-time 5 -sf "http://$(client_host "$BIND_HOST"):${DEFAULT_PORT}/health" >/dev/null 2>&1; do
    if (($(date +%s) - start >= timeout)); then
      echo " [タイムアウト]  tail -n 40 $LOG_FILE"
      return 1
    fi
    echo -n "."
    sleep 2
  done
  echo " [完了]"
}

# --- launchd (agent / daemon) -------------------------------------------------

mode_of_plist() {
  [[ "$1" == "$DAEMON_PLIST" ]] && echo daemon || echo agent
}

launchd_domain() {
  [[ "$1" == daemon ]] && echo "system" || echo "gui/$(id -u)"
}

launchd_sudo() {
  # LaunchDaemon の操作には root が必要
  [[ "$1" == daemon && "$(id -u)" != 0 ]] && echo sudo
  return 0
}

launchd_loaded() {
  local mode="$1"
  launchctl print "$(launchd_domain "$mode")/${PLIST_LABEL}" >/dev/null 2>&1
}

launchd_bootout() {
  local mode="$1" plist="$2" sudo
  sudo=$(launchd_sudo "$mode")
  $sudo launchctl bootout "$(launchd_domain "$mode")/${PLIST_LABEL}" 2>/dev/null ||
    $sudo launchctl unload "$plist" 2>/dev/null || true
}

launchd_bootstrap() {
  local mode="$1" plist="$2" sudo
  sudo=$(launchd_sudo "$mode")
  $sudo launchctl bootstrap "$(launchd_domain "$mode")" "$plist" 2>/dev/null ||
    $sudo launchctl load "$plist"
}

# ------------------------------------------------------------------------------

cmd_start() {
  local pid
  if pid=$(get_server_pid); then
    local running_host
    running_host=$(cat "$HOST_FILE" 2>/dev/null || echo "127.0.0.1")
    echo "既に稼働中: PID $pid  (${running_host}:${DEFAULT_PORT})"
    if [[ "$running_host" != "$BIND_HOST" ]]; then
      echo "   ⚠ バインド先が異なります。変更するには: REC_SERVE_HOST='$BIND_HOST' ./service.sh restart"
    fi
    return 0
  fi

  if ! command -v "$REC_BIN" >/dev/null 2>&1; then
    echo "❌ $REC_BIN が見つかりません (rec のインストールが必要です)" >&2
    return 1
  fi

  rotate_log_if_needed
  local extra_args=()
  [[ "$KEEPALIVE_MODE" == 0 ]] && extra_args+=(--no-keepalive)
  echo ">> 起動: ${BIND_HOST}:${DEFAULT_PORT} → rec 127.0.0.1:${BACKEND_PORT}  (ログ: $LOG_FILE)"
  nohup mise exec -- uv run rec-serve --host "$BIND_HOST" --port "$DEFAULT_PORT" \
    --backend-port "$BACKEND_PORT" --rec-bin "$REC_BIN" \
    ${extra_args[@]+"${extra_args[@]}"} >>"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
  echo "$BIND_HOST" >"$HOST_FILE"
  wait_for_health
  cmd_status
}

cmd_stop() {
  local pid plist mode
  # launchd 経由なら一時的に unload して KeepAlive 再起動を防ぐ
  plist=$(installed_plist)
  if [[ -n "$plist" ]]; then
    mode=$(mode_of_plist "$plist")
    if launchd_loaded "$mode"; then
      echo ">> launchd ($mode) を一時停止 (bootout)..."
      launchd_bootout "$mode" "$plist"
    fi
  fi

  if ! pid=$(get_server_pid); then
    rm -f "$PID_FILE" "$HOST_FILE"
    echo "停止済み: :${DEFAULT_PORT}"
    return 0
  fi
  echo ">> 停止中: PID $pid ..."
  # プロキシが子の rec serve も終了させる
  kill "$pid" 2>/dev/null || true
  for _ in {1..30}; do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  kill -9 "$pid" 2>/dev/null || true
  # 万一プロキシが残っていたら掃除
  local leftover
  leftover=$(lsof -ti tcp:"$DEFAULT_PORT" -sTCP:LISTEN 2>/dev/null || true)
  if [[ -n "$leftover" ]]; then
    kill $leftover 2>/dev/null || true
  fi
  # プロキシ強制終了で rec が残った場合は rec 自身に停止させる
  if lsof -ti tcp:"$BACKEND_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    "$REC_BIN" stop >/dev/null 2>&1 || true
  fi
  rm -f "$PID_FILE" "$HOST_FILE"
  echo "✓ 停止"
}

cmd_restart() {
  cmd_stop || true
  if [[ -n "$(installed_plist)" ]]; then
    # 環境を plist に反映し直して load
    cmd_install
  else
    cmd_start
  fi
}

cmd_status() {
  local pid host health_json info any_public=0 label ip plist mode
  echo "rec-serve 状態  (Recursive rec / Flint)"
  if pid=$(get_server_pid); then
    host=$(cat "$HOST_FILE" 2>/dev/null || echo "127.0.0.1")
    health_json=$(curl --noproxy '*' --connect-timeout 2 --max-time 5 -sf "http://$(client_host "$host"):${DEFAULT_PORT}/health" 2>/dev/null || true)
    if [[ -n "$health_json" ]]; then
      info=$(
        python3 -c "
import sys, json
d=json.loads(sys.stdin.read())
k=d.get('keepalive',{})
u=d.get('upstream',{})
rem=int(k.get('remaining_sec',0)//60)
state='無効' if k.get('enabled') is False else ('有効 (残り '+str(rem)+'分)' if k.get('active') else 'アイドル解除')
print('rec: '+str(u.get('status', d.get('status')))+(' busy' if u.get('busy') else '')+' / スリープ抑制: '+state, end='')
" <<<"$health_json" 2>/dev/null || echo ""
      )
      echo "  【稼働中】  PID: $pid  bind ${host}:${DEFAULT_PORT}  http://$(client_host "$host"):${DEFAULT_PORT}/v1  [$info]"
      if is_public "$host"; then
        any_public=1
        while read -r label ip; do
          [[ -n "$ip" ]] && printf "              %-14s http://%s:%s/v1\n" "$label" "$ip" "$DEFAULT_PORT"
        done < <(reachable_addrs)
      fi
    else
      echo "  【応答なし / 読み込み中】 PID: $pid  :${DEFAULT_PORT}"
    fi
  else
    echo "  【停止中】  :${DEFAULT_PORT}"
  fi

  plist=$(installed_plist)
  if [[ -n "$plist" ]]; then
    mode=$(mode_of_plist "$plist")
    [[ "$mode" == daemon ]] && label="LaunchDaemon" || label="LaunchAgent"
    if launchd_loaded "$mode"; then
      echo "  $label: 登録済・ロード中  ($plist)"
    else
      echo "  $label: 登録済・アンロード  ($plist)"
    fi
  else
    echo "  launchd: 未登録  (./service.sh install [--daemon] で自動起動)"
  fi

  if ((any_public)); then
    echo "  ⚠ 公開バインド中 (rec の API キーはプロキシが付与するため実質認証なし)。"
  fi
}

cmd_log() {
  local follow=""
  for arg in "$@"; do
    case "$arg" in
    -f | --follow) follow=1 ;;
    esac
  done
  if [[ ! -f "$LOG_FILE" ]]; then
    echo "ログなし: $LOG_FILE"
    return 0
  fi
  tail -n 40 ${follow:+-f} "$LOG_FILE"
}

write_plist() {
  local out="$1" mode="$2" keepalive_plist="" user_plist=""
  if [[ "$KEEPALIVE_MODE" == 0 ]]; then
    keepalive_plist="<string>--no-keepalive</string>"
  fi
  if [[ "$mode" == daemon ]]; then
    # root ではなく現在のユーザで実行 (rec のモデル・API キーは ~/.local/share/rec)
    user_plist="<key>UserName</key>
    <string>$(id -un)</string>
    <key>GroupName</key>
    <string>$(id -gn)</string>
    <key>SessionCreate</key>
    <true/>"
  fi

  # 現在の BIND_HOST / PORT を plist に焼き込む
  cat <<EOF >"$out"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_LABEL}</string>
    ${user_plist}
    <key>WorkingDirectory</key>
    <string>${HOME}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${LAUNCHER_BIN}</string>
        <string>--host</string>
        <string>${BIND_HOST}</string>
        <string>--port</string>
        <string>${DEFAULT_PORT}</string>
        <string>--backend-port</string>
        <string>${BACKEND_PORT}</string>
        <string>--rec-bin</string>
        <string>${REC_BIN}</string>
        ${keepalive_plist}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>ProcessType</key>
    <string>Interactive</string>
    <key>StandardOutPath</key>
    <string>${LOG_FILE}</string>
    <key>StandardErrorPath</key>
    <string>${LOG_FILE}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>${HOME}</string>
        <key>PATH</key>
        <string>${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>REC_SERVE_HOST</key>
        <string>${BIND_HOST}</string>
        <key>REC_SERVE_PORT</key>
        <string>${DEFAULT_PORT}</string>
        <key>REC_SERVE_BACKEND_PORT</key>
        <string>${BACKEND_PORT}</string>
    </dict>
</dict>
</plist>
EOF
}

cmd_install() {
  local mode="${SERVICE_MODE:-}" plist other tmp sudo
  # 未指定なら登録済みの方式を継承 (なければ LaunchAgent)
  if [[ -z "$mode" ]]; then
    plist=$(installed_plist)
    if [[ -n "$plist" ]]; then
      mode=$(mode_of_plist "$plist")
    else
      mode=agent
    fi
  fi
  if [[ "$mode" == daemon ]]; then
    plist="$DAEMON_PLIST" other="$AGENT_PLIST"
  else
    plist="$AGENT_PLIST" other="$DAEMON_PLIST"
  fi
  sudo=$(launchd_sudo "$mode")

  if [[ ! -x "$SCRIPT_DIR/.venv/bin/rec-serve" ]]; then
    echo "❌ .venv が未作成です。先に: mise install && mise exec -- uv sync" >&2
    return 1
  fi

  mkdir -p "$HOME/Library/LaunchAgents" "$LOGS_DIR" "$HOME/.local/bin"
  # 既存インスタンスを停止 (ポート競合・KeepAlive クラッシュループ防止)
  if get_server_pid >/dev/null || [[ -n "$(installed_plist)" ]]; then
    echo ">> 既存インスタンスを停止中..."
    cmd_stop
  fi
  # もう一方の方式が登録されていれば解除 (二重起動防止)
  if [[ -f "$other" ]]; then
    local omode
    omode=$(mode_of_plist "$other")
    echo ">> 別方式 ($omode) の登録を解除: $other"
    launchd_bootout "$omode" "$other"
    $(launchd_sudo "$omode") rm -f "$other"
  fi

  # システム設定 (ログイン項目) で「bash」ではなく「rec-serve」と表示させるため、
  # TCC 保護外 (~/.local/bin) に実行スクリプトを配置する。
  # launchd 下でも mise に依存しないよう .venv を直接使う。
  cat <<EOF >"$LAUNCHER_BIN"
#!/bin/bash
set -euo pipefail
cd "${SCRIPT_DIR}"
exec "${SCRIPT_DIR}/.venv/bin/rec-serve" "\$@"
EOF
  chmod +x "$LAUNCHER_BIN"

  tmp=$(mktemp -t rec-serve-plist)
  write_plist "$tmp" "$mode"
  plutil -lint "$tmp" >/dev/null
  if [[ "$mode" == daemon ]]; then
    echo ">> LaunchDaemon を登録します (sudo が必要です)"
    sudo install -m 644 -o root -g wheel "$tmp" "$plist"
    rm -f "$tmp"
    echo "✓ LaunchDaemon を作成しました: $plist  (UserName=$(id -un), ブート時から常駐)"
  else
    mv "$tmp" "$plist"
    chmod 644 "$plist"
    echo "✓ LaunchAgent を作成しました: $plist  (ログイン時に常駐)"
  fi
  launchd_bootout "$mode" "$plist"
  launchd_bootstrap "$mode" "$plist"
  echo "$BIND_HOST" >"$HOST_FILE"
  wait_for_health || true
  cmd_status
}

cmd_uninstall() {
  local plist mode
  plist=$(installed_plist)
  if [[ -z "$plist" ]]; then
    echo "登録されていません。"
    return 0
  fi
  while [[ -n "$plist" ]]; do
    mode=$(mode_of_plist "$plist")
    launchd_bootout "$mode" "$plist"
    $(launchd_sudo "$mode") rm -f "$plist"
    echo "✓ 自動起動 ($mode) の登録を解除しました: $plist"
    plist=$(installed_plist)
  done
  rm -f "$LAUNCHER_BIN" "$PID_FILE" "$HOST_FILE"
}

cmd_help() {
  cat <<EOF
rec-serve — Recursive rec (Flint) 常駐サービス (外部公開プロキシ付き)

使用法: ./service.sh <command> [options]   (引数なし → status)

  start [--public | --tailscale]   バックグラウンド起動 (launchd なし)
  stop                 停止
  restart [--public | --tailscale]   停止 → 起動 (launchd 登録済なら再 load)
  status               稼働状態 / keepalive / launchd
  log [-f]             ログ表示 (-f でフォロー)

  install [--daemon | --agent] [--public | --tailscale]
                       自動常駐を登録 (KeepAlive)
                       --agent  (既定) ~/Library/LaunchAgents/${PLIST_LABEL}.plist
                       --daemon        /Library/LaunchDaemons/${PLIST_LABEL}.plist (要 sudo)
                       未指定なら登録済みの方式を継承
  uninstall            自動起動を解除

オプション:
  --public             0.0.0.0 にバインド (LAN / Tailscale、認証なし)
  --tailscale          Tailscale IPv4 のみにバインド (LAN / localhost では待受しない)
  --localhost          127.0.0.1 のみに戻す
  --no-keepalive       スリープ抑制を無効化。restart 時も設定を継承
  --keepalive          スリープ抑制を再び有効化

環境変数:
  REC_SERVE_HOST          バインド先 (例: 127.0.0.1 / 0.0.0.0)
  REC_SERVE_PORT          公開ポート (既定: 11501)
  REC_SERVE_BACKEND_PORT  rec serve のポート (既定: 11500 = rec の既定)
  REC_SERVE_KEEPALIVE_TIMEOUT  アイドル秒 (既定: 3600)
  REC_BIN                 rec バイナリ (既定: PATH 上の rec)

エンドポイント (API キー不要。プロキシが rec のキーを付与):
  GET  /health
  GET  /v1/models
  POST /v1/chat/completions
EOF
}

action="${1:-status}"
shift || true

args=()
for arg in "$@"; do
  case "$arg" in
  --public | --tailscale | --localhost)
    if [[ -n "$BIND_MODE" && "$BIND_MODE" != "$arg" ]]; then
      echo "❌ --public / --tailscale / --localhost は同時指定できません" >&2
      exit 1
    fi
    BIND_MODE="$arg"
    ;;
  --daemon) SERVICE_MODE=daemon ;;
  --agent) SERVICE_MODE=agent ;;
  --no-keepalive) KEEPALIVE_MODE=0 ;;
  --keepalive) KEEPALIVE_MODE=1 ;;
  *) args+=("$arg") ;;
  esac
done
set -- ${args[@]+"${args[@]}"}

case "$BIND_MODE" in
--public) BIND_HOST="0.0.0.0" ;;
--localhost) BIND_HOST="127.0.0.1" ;;
--tailscale) BIND_HOST=$(resolve_tailscale_host) ;;
esac

if [[ -z "$KEEPALIVE_MODE" ]]; then
  KEEPALIVE_MODE=1
  if plist_get ':ProgramArguments' | grep -q -- '--no-keepalive'; then
    KEEPALIVE_MODE=0
  fi
fi

case "$action" in
start) cmd_start ;;
stop) cmd_stop ;;
restart) cmd_restart ;;
status) cmd_status ;;
log) cmd_log "$@" ;;
install) cmd_install ;;
uninstall) cmd_uninstall ;;
help | -h | --help) cmd_help ;;
*)
  echo "❌ 不明なコマンド: $action" >&2
  cmd_help >&2
  exit 1
  ;;
esac
