@echo off
cd /d "%~dp0"
echo 正在检查并安装依赖，第一次会比较久，请耐心等...
python -m pip install -r requirements.txt
echo 启动中，服务就绪后会自动打开浏览器: http://localhost:8501
start "" /min powershell -NoProfile -Command "$u='http://localhost:8501';for($i=0;$i -lt 60;$i++){try{[void](Invoke-WebRequest ($u+'/_stcore/health') -TimeoutSec 2 -UseBasicParsing);break}catch{Start-Sleep 1}};Start-Process $u"
python -m streamlit run inspection_app.py --server.port 8501 --server.headless=true
pause
