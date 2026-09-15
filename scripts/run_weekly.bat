@echo off
REM ============================================================
REM BB 增量更新 —— 由 Windows 计划任务 BB_WeeklyUpdate 触发
REM （每周六、周日 20:00，见 README 的「定时任务」一节）
REM
REM 只下载新增/被修改的内容；没有新内容时也会写一份报告。
REM 退出码：0=成功 1=其他错误 2=需要人工登录 3=有下载失败 4=已有实例在跑
REM 注意：本脚本默认只在你登录 Windows 时才会被任务计划唤起。
REM ============================================================
cd /d "%~dp0.."

set PY=python
set LOG=logs\update.log
if not exist logs mkdir logs

echo ===== %date% %time% START ===== >> "%LOG%"
"%PY%" bb_crawler.py --update >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%
echo ===== %date% %time% DONE rc=%RC% ===== >> "%LOG%"

exit /b %RC%
