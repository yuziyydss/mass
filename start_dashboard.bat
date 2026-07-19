@echo off
chcp 65001 >nul
echo 正在启动 MASS Dashboard...
echo ================================================
echo 请确保已在 .env 文件中配置好 TUSHARE_TOKEN
echo 每日自动运行时间: 18:30 (可在 .env 中修改 MASS_RUN_TIME)
echo Web 管理界面: http://localhost:8008
echo ================================================
python run_mass_dashboard.py serve
pause
