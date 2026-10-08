# Scripted A/B of volume and EQ writes on the working area, recorded over USB.
# The player must play continuously while this runs (~60 s). Never saves.
param([int]$Seconds = 6, [string]$Out = "captures/seq")
$ErrorActionPreference = "Stop"
$py = "uv", "run", "python"
function Rec($name) { & uv run python scripts/measure.py --record-only $Seconds --out "$Out/$name" | Out-Null; Write-Host "recorded $name" }
function W($t, $a, $d) { & uv run python scripts/probe.py --trace "$Out/trace.jsonl" write $t $a $d --yes | Select-Object -Last 1 | Out-Null }

New-Item -ItemType Directory -Force $Out | Out-Null
Rec "01_baseline"
W 5 0x2B 14;            Rec "02_vol20_norefresh"
W 5 0xA0000000 03;      Rec "03_vol20_refresh3"
W 5 0x2B 46; W 5 0xA0000000 03; Rec "04_vol70_restored"
W 5 0x12 01; W 5 0x1C24 e803; Rec "05_eqon_lp1k_norefresh"
W 5 0xA0000000 02;      Rec "06_eqon_lp1k_refresh2"
W 5 0xA0000000 01;      Rec "07_eqon_lp1k_refresh1"
W 4 0xE0000016 16; W 4 0xE0000017 17; Rec "08_restored_by_reselect"
Write-Host "done"
