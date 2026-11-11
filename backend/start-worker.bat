@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 用法：start-worker.bat [ingest^|summary^|project^|regression]
rem 不带参数时仍然是 ingest（与旧快捷方式/旧文档完全兼容）。
set "ROLE=%~1"
if not defined ROLE set "ROLE=ingest"
if not "%~2"=="" goto usage

set "KNOWN="
rem 白名单匹配忽略大小写；命中后把 ROLE 规范化为该固定小写角色，
rem 因为 worker.py 的 argparse choices 严格区分大小写（REGRESSION 等必须落为 regression）。
for %%r in (ingest summary project regression) do if /i "%ROLE%"=="%%r" (set "KNOWN=1" & set "ROLE=%%r")
if not defined KNOWN goto usage

if /i "%ROLE%"=="ingest" goto ingest
echo Starting %ROLE% worker...
".venv\Scripts\python.exe" -X utf8 worker.py --role %ROLE%
pause
exit /b

:ingest
echo Starting ingest worker...
".venv\Scripts\python.exe" -X utf8 worker.py
pause
exit /b

:usage
echo Usage: start-worker.bat [ingest^|summary^|project^|regression]
echo   不带参数等同 ingest；其他角色各自占用一个独立进程。
pause
exit /b 2
