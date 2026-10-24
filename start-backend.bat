@echo off
chcp 65001 >nul
cd /d "%~dp0backend"

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo [提示] 已从 .env.example 生成 .env，请按需修改配置
)

if not exist ".venv" (
    echo [1/2] 创建虚拟环境...
    python -m venv .venv
    echo [2/2] 安装依赖...
    .venv\Scripts\python.exe -m pip install -r requirements.txt
)

echo.
echo 启动后端: http://localhost:8000/docs
echo.
.venv\Scripts\python.exe run.py
