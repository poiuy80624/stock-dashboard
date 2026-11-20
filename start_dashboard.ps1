param([switch]$NoBrowser)

# 一鍵啟動：更新資料 → 啟動網頁 → 開啟瀏覽器
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')

# 對話功能的設定：若這個程序沒帶到（例如剛設定完使用者環境變數），就從使用者環境變數補上
foreach ($n in 'ANTHROPIC_API_KEY', 'AC_CHAT_MODEL', 'AC_CHAT_EFFORT') {
    if (-not (Test-Path "env:$n")) {
        $v = [Environment]::GetEnvironmentVariable($n, 'User')
        if ($v) { Set-Item "env:$n" $v }
    }
}

$port = 8000
$url = "http://127.0.0.1:$port"

Write-Host "更新資料（git pull）..." -ForegroundColor Cyan
git pull --ff-only
if ($LASTEXITCODE -ne 0) {
    Write-Host "git pull 失敗，將使用本機現有資料。" -ForegroundColor Yellow
}

# 已經有網頁在執行就只開瀏覽器
if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
    Write-Host "網頁已在執行：$url" -ForegroundColor Green
    if (-not $NoBrowser) { Start-Process $url }
    exit 0
}

if (-not $NoBrowser) {
    # 等伺服器起來後再開瀏覽器
    Start-Process powershell -WindowStyle Hidden -ArgumentList "-NoProfile", "-Command", "Start-Sleep 4; Start-Process '$url'"
}

Write-Host "網頁啟動中：$url（關閉這個視窗就會停止）" -ForegroundColor Green
uv run uvicorn web.app:app --host 127.0.0.1 --port $port

