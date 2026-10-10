# Fishtest のワーカー・モニター・TV をまとめて起動する。すでに動いているものは起動しない
# （TV を2つ開くと実況が2人になるので、特に TV は重複させない）。
#   fishtest      ワーカー + モニター + TV
#   fishtest tv   TV だけ（モニターが止まっていれば一緒に起動）
#   fishtest stop ワーカーを穏やかに止める（Worker Manager アプリの停止と同じく fish.exit を置く）
#   fishtest worker ワーカーだけ（TV は開かない。スマホのリモコンから使う）
#   fishtest tvoff  TV を閉じる
#   fishtest remote スマホのリモコンの URL と PIN を表示する
# Windows PowerShell 5.1 / PowerShell 7 の両方で動く。
param([ValidateSet('all', 'tv', 'stop', 'worker', 'tvoff', 'remote')][string]$Mode = 'all')

# 場所の設定。環境変数で上書きできる（FISHTEST_WORKER / FISHTEST_PYTHON / MSYS2_ROOT）
$project = $PSScriptRoot  # このリポジトリ（モニター・TV）
$worker = if ($env:FISHTEST_WORKER) { $env:FISHTEST_WORKER } else { 'C:\fishtest\worker' }  # Fishtest ワーカー本体
$msys = if ($env:MSYS2_ROOT) { $env:MSYS2_ROOT } else { 'C:\msys64' }
$port = 8787
$tvUrl = "http://127.0.0.1:$port/tv"
$tvProfile = Join-Path $env:LOCALAPPDATA 'fishtest-tv-edge'
# モニター用の Python：ストアの python.exe（WindowsApps のスタブ）は避ける
$python = $env:FISHTEST_PYTHON
if (-not $python) {
    $python = Get-Command python -All -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -notmatch 'WindowsApps' } | Select-Object -First 1 -ExpandProperty Source
}
if (-not $python) { $python = 'py' }

function Test-Monitor {
    try { $c = New-Object Net.Sockets.TcpClient('127.0.0.1', $port); $c.Close(); return $true } catch { return $false }
}

function Get-Running([string]$pattern, [string]$name) {
    Get-CimInstance Win32_Process -Filter "Name LIKE '$name'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match $pattern }
}

# 停止：ワーカーは次の結果報告のときに fish.exit を見つけ、終わった対局を送ってから止まる
# （そのとき対局中だった数局だけは失われ、残りはサーバーが他の PC に回す）。ワーカーが fish.exit を自分で消す
if ($Mode -eq 'stop') {
    if (-not (Get-Running 'worker\.py' 'python3%')) {
        Write-Host '  worker   not running' -ForegroundColor DarkGray
        return
    }
    New-Item -Path (Join-Path $worker 'fish.exit') -ItemType File -Force | Out-Null
    Write-Host '  worker   stopping gracefully (fish.exit, same as the Worker Manager app)...' -ForegroundColor Green
    for ($i = 0; $i -lt 300 -and (Get-Running 'worker\.py' 'python3%'); $i++) { Start-Sleep -Seconds 1 }
    if (Get-Running 'worker\.py' 'python3%') {
        Write-Host '  worker   still finishing after 5 min; it will stop at its next report' -ForegroundColor Yellow
    } else {
        Write-Host "  worker   stopped after ${i}s. Monitor and TV keep running (close them, or Alt+F4 the TV)" -ForegroundColor Green
    }
    return
}

# TV を閉じる：専用プロファイルの Edge だけを、子プロセスごと終わらせる（普段の Edge には触れない）
if ($Mode -eq 'tvoff') {
    $tv = Get-Running 'fishtest-tv-edge' 'msedge.exe'
    if (-not $tv) {
        Write-Host '  tv       not open' -ForegroundColor DarkGray
        return
    }
    $tv | ForEach-Object { & taskkill.exe /PID $_.ProcessId /T /F 2>$null | Out-Null }
    Write-Host '  tv       closed' -ForegroundColor Green
    return
}

# モニター：LAN（スマホ）からも開けるよう全インターフェースで待ち受ける。
# 外からの接続は PIN が要る（127.0.0.1 からは不要）。最小化したウィンドウで、閉じれば止まる
function Start-Monitor {
    if (Test-Monitor) {
        Write-Host "  monitor  already running on :$port" -ForegroundColor DarkGray
        return
    }
    Start-Process -FilePath $python -ArgumentList @('monitor.py', '--host', '0.0.0.0', '--worker', "`"$worker`"") -WorkingDirectory $project -WindowStyle Minimized
    Write-Host "  monitor  starting on :$port (minimized window)" -ForegroundColor Green
}

# リモコン：Wi-Fi の IP と PIN を出す。PIN はモニターが初回起動時に tv-cache\remote-pin.txt に作る
if ($Mode -eq 'remote') {
    Start-Monitor
    for ($i = 0; $i -lt 60 -and -not (Test-Monitor); $i++) { Start-Sleep -Milliseconds 500 }
    $pinFile = Join-Path $project 'tv-cache\remote-pin.txt'
    $pin = if (Test-Path $pinFile) { (Get-Content $pinFile -TotalCount 1).Trim() } else { '(not created yet; restart the monitor)' }
    $ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.InterfaceAlias -notmatch 'vEthernet|Loopback' -and $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '127.0.0.1' }
    Write-Host ''
    Write-Host '  Open on your phone (same Wi-Fi):' -ForegroundColor Green
    foreach ($ip in $ips) { Write-Host ("    http://{0}:{1}/remote   ({2})" -f $ip.IPAddress, $port, $ip.InterfaceAlias) }
    Write-Host "  PIN: $pin" -ForegroundColor Yellow
    Write-Host ''
    return
}

# 0. 更新：自動で更新されないものだけをここで更新する
#    - ワーカー本体は起動時と実行中に Fishtest へ版を問い合わせ、古ければ自分で更新して再起動する
#    - Stockfish の履歴（取り込み判定用）はモニターの起動時に取り直す
#    - Edge・Ollama 本体はそれぞれ自動更新される
#    - Ollama のモデル（実況・スカウティングレポート用）は自動では更新されないので、ここで pull する。
#      最新なら 1 秒ほど。新しい版があれば数 GB 落とすことがあるので、裏で動かして TV の起動を待たせない
$models = @($env:NARRATOR_MODEL, $env:BRIEF_MODEL, 'qwen3:8b') | Where-Object { $_ } | Select-Object -Unique
if (Get-Command ollama -ErrorAction SilentlyContinue) {
    foreach ($m in $models) {
        Start-Process -FilePath 'ollama' -ArgumentList @('pull', $m) -WindowStyle Hidden
    }
    Write-Host "  models   checking for updates in the background ($($models -join ', '))" -ForegroundColor Green
} else {
    Write-Host '  models   ollama not found; the TV will run without commentary' -ForegroundColor Yellow
}
if ($Mode -in 'all', 'worker') {
    Write-Host '  worker   updates itself when it starts (asks Fishtest for the current version)' -ForegroundColor DarkGray
}

# 1. ワーカー：普段と同じく MSYS2 の ucrt64 環境で worker.py を動かす（別ウィンドウで出力が見える）
if ($Mode -in 'all', 'worker') {
    if (Get-Running 'worker\.py' 'python3%') {
        Write-Host '  worker   already running' -ForegroundColor DarkGray
    } else {
        Start-Process -FilePath (Join-Path $msys 'msys2_shell.cmd') -ArgumentList @(
            '-defterm', '-ucrt64', '-no-start', '-where', "`"$worker`"", '-c', '"env/bin/python3 worker.py"'
        )
        Write-Host '  worker   started (its own window)' -ForegroundColor Green
    }
}

# 2. モニター
Start-Monitor
if ($Mode -eq 'worker') { return }

# 3. TV：ポートが応答してから開く（先に開くと接続エラーになって再読み込みが要る）。
#    専用プロファイルなので、普段の Edge が開いていても自動再生（実況の声）が効く
if (Get-Running 'fishtest-tv-edge' 'msedge.exe') {
    Write-Host '  tv       already open (Alt+F4 closes it)' -ForegroundColor DarkGray
    return
}
for ($i = 0; $i -lt 60 -and -not (Test-Monitor); $i++) { Start-Sleep -Milliseconds 500 }
if (-not (Test-Monitor)) {
    Write-Host "  tv       monitor did not answer on :$port within 30 s; not opening the TV" -ForegroundColor Red
    return
}
Start-Process -FilePath 'msedge' -ArgumentList @(
    '--kiosk', $tvUrl, '--edge-kiosk-type=fullscreen', '--autoplay-policy=no-user-gesture-required',
    "--user-data-dir=`"$tvProfile`"", '--no-first-run'
)
Write-Host '  tv       opened fullscreen (Alt+F4 closes it)' -ForegroundColor Green
