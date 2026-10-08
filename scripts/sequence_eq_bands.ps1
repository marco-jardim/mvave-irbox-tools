# Scripted A/B of individual EQ bands on the working area, recorded over USB.
# The player must play continuously while this runs (~50 s). Never saves.
# enable[] index map (from the official app): 0=HPF 1=low shelf 2..6=peak0..4 7=high shelf 8=LPF
param([int]$Seconds = 6, [string]$Out = "captures/seq_eq")
$ErrorActionPreference = "Stop"
function Rec($name) { & uv run python scripts/measure.py --record-only $Seconds --out "$Out/$name" | Out-Null; Write-Host "recorded $name" }
function W($t, $a, $d) { & uv run python scripts/probe.py --trace "$Out/trace.jsonl" write $t $a $d --yes | Select-Object -Last 1 | Out-Null }

New-Item -ItemType Directory -Force $Out | Out-Null
Rec "01_baseline"
# eq on; LPF enable[8]=1 at 1000 Hz (lpHz @0x1C24)
W 5 0x12 01; W 5 0x1C08 01; W 5 0x1C24 e803; W 5 0xA0000000 02; Rec "02_lpf1k"
# LPF off; HPF enable[0]=1 at 800 Hz (hpHz @0x1C26)
W 5 0x1C08 00; W 5 0x1C00 01; W 5 0x1C26 2003; W 5 0xA0000000 02; Rec "03_hpf800"
# HPF off; peak0 enable[2]=1, 100 Hz (default), +120 (expect +12 dB if 0.1 dB units) @0x1C0E
W 5 0x1C00 00; W 5 0x1C02 01; W 5 0x1C0E 78; W 5 0xA0000000 02; Rec "04_peak100_plus120"
# restore from flash
W 4 0xE0000016 16; W 4 0xE0000017 17; Rec "05_restored"
Write-Host "done"
