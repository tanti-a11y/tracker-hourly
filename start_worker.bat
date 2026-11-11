@echo off
rem Menjalankan worker Shop Analytics Hourly (mode jaga, tanpa batas jam).
rem Dipanggil otomatis saat Windows login (lihat install_autostart.bat), atau bisa diklik manual.
cd /d "%~dp0"
echo.>> worker.log
echo ===== start_worker.bat %date% %time% =====>> worker.log
py -u shop_analytics_hourly.py --stop-hour 0 >> worker.log 2>&1
