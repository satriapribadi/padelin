# Buat pintasan Padelin.lnk untuk menjalankan aplikasi dari kode sumber.
#
# Kenapa .lnk dan bukan .exe: membuat exe baru menghasilkan biner yang belum
# pernah dilihat Windows, dan di mesin dengan Smart App Control aktif biner
# seperti itu diblokir mentah-mentah. Pintasan bukan executable - ia cuma
# menunjuk - jadi tidak kena aturan itu sama sekali, sekaligus tetap bisa
# memakai ikon dan nama sendiri.
#
# Targetnya electron.exe langsung, bukan `npm start`. Hasil akhirnya sama persis
# (npm start = electron .), tapi tanpa jendela konsol yang menganggur di
# belakang, dan tanpa bergantung pada npm/node ada di PATH.
#
#   npm run shortcut              -> taruh di folder repo
#   npm run shortcut -- -Desktop  -> taruh juga di Desktop

param([switch]$Desktop)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$electron = Join-Path $repo 'node_modules\electron\dist\electron.exe'
$icon = Join-Path $repo 'electron\build\icon.ico'

if (-not (Test-Path $electron)) {
    throw "electron.exe tidak ada. Jalankan `npm install` dulu."
}
if (-not (Test-Path $icon)) {
    throw "icon.ico belum dibuat. Jalankan `npm run icon` dulu."
}

function New-Pintasan($tujuan) {
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($tujuan)
    $lnk.TargetPath = $electron
    $lnk.Arguments = '.'
    $lnk.WorkingDirectory = $repo      # wajib: dari sinilah '.' dibaca
    $lnk.IconLocation = "$icon,0"
    $lnk.Description = 'Padelin - jadwal meet, beres.'
    $lnk.WindowStyle = 1
    $lnk.Save()
    Write-Host "Pintasan dibuat: $tujuan"
}

New-Pintasan (Join-Path $repo 'Padelin.lnk')
if ($Desktop) {
    New-Pintasan (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Padelin.lnk')
}
