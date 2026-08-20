@echo off
chcp 65001 >nul 2>&1
setlocal EnableExtensions
title 巡查台账汇总分析系统

rem ===================== 可改的设置 =====================
set "APP=inspection_app.py"
set "PORT=8501"
rem =====================================================

rem 双击运行时把工作目录切到本文件所在的文件夹,
rem 这样不管从哪里启动都能找到 inspection_app.py 和 .streamlit\config.toml
cd /d "%~dp0"

rem 本脚本会用 --wait-open 参数再调用自己一次, 在后台等服务起来后开浏览器
if /i "%~1"=="--wait-open" goto WAIT_OPEN

echo ============================================================
echo   巡查台账汇总分析系统
echo   目录: %CD%
echo ============================================================
echo.

rem ---------- 0. 确认文件齐全 ----------
if not exist "%APP%" goto NO_APP
if not exist "requirements.txt" goto NO_REQ

rem ---------- 1. 找 Python ----------
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY (
    py -3 -c "pass" >nul 2>&1 && set "PY=py -3"
)
if not defined PY goto NO_PYTHON

for /f "delims=" %%V in ('%PY% -V 2^>^&1') do set "PYVER=%%V"
echo [1/4] 已找到 Python: %PYVER%

rem ---------- 2. 检查 / 安装依赖 ----------
rem 只在"缺包"或"requirements.txt 有改动"时才装, 避免每次双击都等 pip.
rem 用 find_spec 而不是真的 import: 只看包在不在, 不执行包代码, 快很多.
set "CHECK_CORE=import importlib.util as u,sys;sys.exit(0 if all(u.find_spec(m) for m in ['streamlit','pandas','numpy','plotly','openpyxl','xlsxwriter']) else 1)"

set "NEED_INSTALL=0"
%PY% -c "%CHECK_CORE%" >nul 2>&1
if errorlevel 1 set "NEED_INSTALL=1"

set "REQ_STAMP="
for %%F in ("requirements.txt") do set "REQ_STAMP=%%~tF"
set "OLD_STAMP="
if exist ".deps_installed" set /p OLD_STAMP=<".deps_installed"
if not "%REQ_STAMP%"=="%OLD_STAMP%" set "NEED_INSTALL=1"

if "%NEED_INSTALL%"=="0" (
    echo [2/4] 依赖已就绪, 跳过安装
    goto DEPS_DONE
)

echo [2/4] 正在安装依赖, 第一次会久一点, 请耐心等...
echo.
%PY% -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo 直接安装失败, 改用 --user 再试一次...
    %PY% -m pip install --user -r requirements.txt
)

rem 不看 pip 的返回值, 只看核心包能不能用:
rem python-calamine 和 kaleido 是可选加速/出图组件, 装不上程序会自动降级,
rem 不该因为它们失败就不让启动.
%PY% -c "%CHECK_CORE%" >nul 2>&1
if errorlevel 1 goto PIP_FAILED
>".deps_installed" echo %REQ_STAMP%
echo.
echo       依赖安装完成
rem 注意: 这里不能用 Python 的百分号格式化 —— 批处理会把百分号当变量符号,
rem 所以改成字符串相加.
%PY% -c "import importlib.util as u;a='已装' if u.find_spec('python_calamine') else '未装(自动降级)';b='已装' if u.find_spec('kaleido') else '未装(图表存HTML)';print('       可选组件: calamine='+a+' / kaleido='+b)" 2>nul

:DEPS_DONE

rem ---------- 3. 选端口 ----------
rem 如果这个端口上已经跑着本程序, 就不再开第二个, 直接打开浏览器
set "HAS_CURL=0"
where curl >nul 2>&1 && set "HAS_CURL=1"

if "%HAS_CURL%"=="1" (
    curl -s -o NUL --max-time 2 "http://localhost:%PORT%/_stcore/health" >nul 2>&1
    if not errorlevel 1 (
        echo [3/4] 检测到程序已经在 %PORT% 端口运行, 直接打开网页
        start "" "http://localhost:%PORT%"
        echo.
        echo 如需重启, 请先关掉之前那个黑色命令行窗口.
        timeout /t 5 /nobreak >nul
        exit /b 0
    )
)

rem 端口被别的程序占用就往后找一个空的
set /a TRY=0
:PORT_LOOP
netstat -a -n -p TCP | findstr /r /c:":%PORT% .*LISTENING" >nul 2>&1
if errorlevel 1 goto PORT_OK
set /a PORT+=1
set /a TRY+=1
if %TRY% GEQ 20 goto PORT_OK
goto PORT_LOOP
:PORT_OK
echo [3/4] 使用端口 %PORT%

rem ---------- 4. 启动 ----------
echo [4/4] 正在启动, 浏览器会在服务就绪后自动打开...
echo.
echo        网址: http://localhost:%PORT%
echo        关闭本窗口, 或按 Ctrl+C 即可停止程序
echo ============================================================
echo.

rem 后台等健康检查通过再开浏览器, 避免浏览器打开太早显示"无法连接"
start "打开浏览器" /min cmd /c call "%~f0" --wait-open %PORT%

rem 前台跑 streamlit: 日志看得见, Ctrl+C 也能正常停
rem 显式带上 --server.headless=true, 由本脚本负责开浏览器, 只开一个标签页
%PY% -m streamlit run "%APP%" --server.port %PORT% --server.headless=true

echo.
echo 程序已停止.
pause
exit /b 0


rem ==================== 后台: 等服务起来再开浏览器 ====================
:WAIT_OPEN
set "WPORT=%~2"
if "%WPORT%"=="" set "WPORT=8501"
set "URL=http://localhost:%WPORT%"

where curl >nul 2>&1
if errorlevel 1 (
    rem 很老的系统没有 curl, 那就固定等 10 秒
    timeout /t 10 /nobreak >nul
    goto DO_OPEN
)

set /a N=0
:POLL
set /a N+=1
if %N% GTR 120 goto DO_OPEN
curl -s -o NUL --max-time 2 "%URL%/_stcore/health" >nul 2>&1
if not errorlevel 1 goto DO_OPEN
timeout /t 1 /nobreak >nul
goto POLL

:DO_OPEN
start "" "%URL%"
exit /b 0


rem ==================== 出错处理 ====================
:NO_APP
echo [错误] 当前目录下找不到 %APP%
echo.
echo 请把本 .bat 文件放在和 %APP% 同一个文件夹里再双击.
echo 当前目录: %CD%
echo.
pause
exit /b 1

:NO_REQ
echo [错误] 当前目录下找不到 requirements.txt
echo.
echo 请把本 .bat 文件放在项目根目录 (和 inspection_app.py 同级) 再双击.
echo 当前目录: %CD%
echo.
pause
exit /b 1

:NO_PYTHON
echo.
echo [错误] 没有找到 Python.
echo.
echo 请先安装 Python 3.10 或更高版本: https://www.python.org/downloads/
echo 安装时务必勾选 "Add python.exe to PATH" 这一项,
echo 装完后关掉本窗口重新双击本文件即可.
echo.
pause
exit /b 1

:PIP_FAILED
echo.
echo [错误] 依赖安装失败.
echo.
echo 可以试试这几种办法:
echo   1^) 检查网络; 如果在公司网内, 可换国内源再手动装一次:
echo      %PY% -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
echo   2^) 右键本文件选择 "以管理员身份运行"
echo   3^) 确认 Python 版本不低于 3.10:  %PY% -V
echo.
pause
exit /b 1
