@echo off
chcp 65001 >nul
rem Сборка единственного файла agent.exe (запускать на Windows с Python 3.10+)

set /p URL=Адрес вашего сервера (например http://192.168.0.10:8000):
set /p KEY=AGENT_KEY (тот же, что на сервере):
set /p NAME=Имя устройства (Enter = ПК сына):
if "%NAME%"=="" set NAME=ПК сына

python -c "import json,sys;json.dump({'server_url':sys.argv[1],'agent_key':sys.argv[2],'device_name':sys.argv[3],'show_indicator':True},open('embedded_config.json','w',encoding='utf-8'),ensure_ascii=False)" "%URL%" "%KEY%" "%NAME%"

pip install -r requirements.txt
pyinstaller --onefile --noconsole --name agent --add-data "embedded_config.json;." agent.py

echo.
echo Готово: dist\agent.exe  (адрес и ключ уже внутри)
echo Не выкладывайте этот файл в открытый доступ - в нём ваш ключ.
pause
