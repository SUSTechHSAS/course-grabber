@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist config.json (
  copy config.example.json config.json >nul
  echo ==============================================================
  echo  第一次运行：已生成 config.json
  echo  请用记事本打开，填上你学校的域名、接口路径与候选教学班：
  echo      notepad "%cd%\config.json"
  echo.
  echo  想用自动登录的话，再准备凭据文件（只有你能读）：
  echo      %USERPROFILE%\.config\course-grabber\credentials.json
  echo      内容: {"student_id": "你的学号", "password": "你的密码"}
  echo ==============================================================
  pause
  exit /b 0
)

course-grabber.exe %*
