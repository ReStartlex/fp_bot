# Packs the project into app.zip for deployment.
# Run from project root:  .\deploy\pack.ps1

$ErrorActionPreference = "Stop"

$projectRoot = (Get-Item $PSScriptRoot).Parent.FullName
Set-Location $projectRoot

$outZip = Join-Path $projectRoot "app.zip"
if (Test-Path $outZip) { Remove-Item $outZip -Force }

# What to include
$include = @(
    "src",
    "deploy",
    "requirements.txt",
    "README.md",
    ".env.example",
    "api-docs.md",
    "api-playground.md"
)

# What to exclude from copied directories
$exclude = @(".venv", "__pycache__", ".git", "logs", "data", ".env", "app.zip")

Write-Host "==> Packing project into app.zip..."

$tempDir = Join-Path $env:TEMP "funpay-ns-bot-pack"
if (Test-Path $tempDir) { Remove-Item $tempDir -Recurse -Force }
New-Item -ItemType Directory -Path $tempDir | Out-Null

foreach ($item in $include) {
    $src = Join-Path $projectRoot $item
    if (-not (Test-Path $src)) {
        Write-Warning "Skipped (missing): $item"
        continue
    }
    $dst = Join-Path $tempDir $item
    if ((Get-Item $src).PSIsContainer) {
        Copy-Item $src -Destination $dst -Recurse
        foreach ($ex in $exclude) {
            Get-ChildItem -Path $dst -Recurse -Force -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -eq $ex } |
                Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        }
    } else {
        Copy-Item $src -Destination $dst
    }
}

Compress-Archive -Path (Join-Path $tempDir "*") -DestinationPath $outZip -Force
Remove-Item $tempDir -Recurse -Force

$bytes = (Get-Item $outZip).Length
$sizeKB = [math]::Round($bytes / 1024.0, 1)
Write-Host ("==> Done: {0} ({1} KB)" -f $outZip, $sizeKB)
Write-Host ""
Write-Host "Next steps:"
Write-Host "  scp app.zip root@85.239.42.127:/opt/funpay-ns-bot/"
Write-Host "  scp .env    root@85.239.42.127:/opt/funpay-ns-bot/"
Write-Host "  ssh root@85.239.42.127 'cd /opt/funpay-ns-bot && unzip -o app.zip && bash deploy/install_app.sh'"
