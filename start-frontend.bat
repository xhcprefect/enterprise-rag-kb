@echo off
chcp 65001 >nul
cd /d "%~dp0frontend"

if not exist "node_modules" (
    echo 安装前端依赖...
    call npm install
)

echo.
echo 启动前端: http://localhost:5173
echo.
call npm run dev
