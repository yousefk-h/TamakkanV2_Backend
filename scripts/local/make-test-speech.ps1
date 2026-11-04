<# Generates the spoken student answers used by e2e_mariam.py (Windows text-to-speech, 24 kHz mono WAV). #>
param([string]$OutDir = (Join-Path $env:TEMP 'e2e'))
Add-Type -AssemblyName System.Speech
New-Item -ItemType Directory -Force $OutDir | Out-Null
$lines = @(
 'Hello Mariam. My name is Sam and I am ready to practice English today.',
 'I really like listening to music every day. My favorite music is pop, because the songs are happy and easy to sing. I usually listen when I walk to work and when I cook dinner.',
 'Yes, I also play the guitar a little. I started learning last year, and now I can play three songs. My dream is to play for my family at a party next summer.')
$i = 1
foreach ($t in $lines) {
  $s = New-Object System.Speech.Synthesis.SpeechSynthesizer
  $fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(24000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
  $s.SetOutputToWaveFile((Join-Path $OutDir "utt$i.wav"), $fmt); $s.Speak($t); $s.Dispose(); $i++
}
Get-ChildItem (Join-Path $OutDir '*.wav') | Select-Object Name, Length
