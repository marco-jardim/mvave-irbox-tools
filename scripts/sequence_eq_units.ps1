# EQ band behaviour and units, recorded over USB. Player plays continuously (~45 s). Never saves.
# eq_on (0x12) must be followed by refresh(1); band params by refresh(2).
param([int]$Seconds = 6, [string]$Out = "captures/seq_eq3")
$ErrorActionPreference = "Stop"
function Rec($name) { & uv run python scripts/measure.py --record-only $Seconds --out "$Out/$name" | Out-Null; Write-Host "recorded $name" }
function W($t, $a, $d) { & uv run python scripts/probe.py --trace "$Out/trace.jsonl" write $t $a $d --yes | Select-Object -Last 1 | Out-Null }

New-Item -ItemType Directory -Force $Out | Out-Null
W 5 0x12 01; W 5 0xA0000000 01; Rec "01_eqon_flat"
W 5 0x1C08 01; W 5 0x1C24 e803; W 5 0xA0000000 02; Rec "02_lpf1k"
W 5 0x1C08 00; W 5 0x1C02 01; W 5 0x1C0E 78; W 5 0xA0000000 02; Rec "03_peak100_+120"
W 5 0x1C0E 88; W 5 0xA0000000 02; Rec "04_peak100_-120"
W 5 0x1C0E 3c; W 5 0xA0000000 02; Rec "05_peak100_+60"
W 4 0xE0000016 16; W 4 0xE0000017 17; Rec "06_restored"
Write-Host "done"
