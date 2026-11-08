@echo off
rem Start the hub on Windows: double-click this file, or run it from a terminal.
rem
rem   start.bat              http://127.0.0.1:8000, auto-reload on file changes
rem   start.bat 8080         a different port
rem   start.bat --no-reload  no file watcher, closer to how it runs in production
rem   start.bat --no-browser do not open the browser
rem
rem The first run creates .venv, installs the hub with its dependencies and the spaCy model, and
rem writes a .env with freshly generated keys. It never installs system packages, and it never
rem overwrites an existing .env. run.sh does the same on Linux and macOS.

setlocal EnableExtensions
cd /d "%~dp0"

set "PORT=8000"
set "RELOAD=--reload"
set "BROWSER=1"
:args
if "%~1"=="" goto args_done
if /i "%~1"=="--no-reload" (set "RELOAD=" & shift & goto args)
if /i "%~1"=="--no-browser" (set "BROWSER=" & shift & goto args)
if /i "%~1"=="--help" goto usage
if /i "%~1"=="-h" goto usage
echo %~1| findstr /r "^[0-9][0-9]*$" >nul
if not errorlevel 1 (set "PORT=%~1" & shift & goto args)
echo Unknown argument: %~1 (try --help)
exit /b 2
:args_done

set "PY=.venv\Scripts\python.exe"

rem 1. Virtual environment, made with Python 3.12 or newer.
if exist "%PY%" goto venv_ready
set "BASE=py -3"
where py >nul 2>&1 || set "BASE=python"
%BASE% -c "import sys; sys.exit(sys.version_info < (3, 12))" >nul 2>&1
if errorlevel 1 (
  call :fail "PolGate needs Python 3.12 or newer." "Install it from https://www.python.org/downloads/ and run start.bat again."
  exit /b 1
)
echo Creating the virtual environment in .venv ...
%BASE% -m venv .venv
if errorlevel 1 (
  call :fail "Could not create .venv." "%BASE% -m venv .venv"
  exit /b 1
)
:venv_ready

rem 2. Dependencies, polanon included.
"%PY%" -c "import fastapi, sqlmodel, spacy, polanon" >nul 2>&1
if not errorlevel 1 goto deps_ready
echo Installing the hub and its dependencies, which takes a few minutes the first time ...
"%PY%" -m pip install --upgrade pip >nul
"%PY%" -m pip install -e .
if errorlevel 1 (
  call :fail "Installing the dependencies failed; the messages above say why." ".venv\Scripts\python.exe -m pip install -e ."
  exit /b 1
)
:deps_ready

rem 3. Language model for the Polish name detector.
set "SPACY_MODEL=pl_core_news_md"
if exist .env for /f "usebackq tokens=1,* delims==" %%a in (".env") do if "%%a"=="SPACY_MODEL" if not "%%b"=="" set "SPACY_MODEL=%%b"
"%PY%" -c "import spacy; spacy.load('%SPACY_MODEL%')" >nul 2>&1
if not errorlevel 1 goto model_ready
echo Downloading the spaCy model %SPACY_MODEL% ...
"%PY%" -m spacy download %SPACY_MODEL%
if errorlevel 1 (
  call :fail "Downloading the spaCy model failed." ".venv\Scripts\python.exe -m spacy download %SPACY_MODEL%"
  exit /b 1
)
:model_ready

rem 4. Configuration. A first run gets a working .env with freshly generated keys.
if exist .env goto env_ready
echo No .env found, creating one from .env.example with generated keys.
copy /y .env.example .env >nul
"%PY%" -c "import secrets, pathlib; from cryptography.fernet import Fernet; p = pathlib.Path('.env'); keys = {'SECRET_KEY': secrets.token_urlsafe(48), 'VAULT_KEY': Fernet.generate_key().decode()}; lines = [k + '=' + keys[k] if (k := line.split('=', 1)[0]) in keys and '=' in line else line for line in p.read_text(encoding='utf-8').splitlines()]; p.write_text('\n'.join(lines) + '\n', encoding='utf-8')"
if errorlevel 1 (
  call :fail "Could not write the keys into .env." "Fill in SECRET_KEY and VAULT_KEY in .env by hand, or delete .env and run start.bat again."
  exit /b 1
)
echo Set LLM_API_KEY in .env to talk to a real provider.
:env_ready

rem 5. Database schema. Columns a newer model added are applied automatically; anything this cannot
rem    put right on its own stops the run, because the hub would start and then fail on every message.
set "SCHEMA_REPORT=%TEMP%\polgate-schema-%RANDOM%.txt"
"%PY%" -m polgate.schema > "%SCHEMA_REPORT%" 2>&1
if errorlevel 1 (
  echo.
  echo   The database does not match the current models:
  echo.
  type "%SCHEMA_REPORT%"
  del "%SCHEMA_REPORT%" >nul 2>&1
  echo.
  pause
  exit /b 1
)
findstr /b "added:" "%SCHEMA_REPORT%" >nul && echo Schema updated: new columns were added.
del "%SCHEMA_REPORT%" >nul 2>&1

rem 6. Warn about anything that limits what you can test, without blocking the run.
set "LLM_KEY=" & set "ADMIN_USER=admin" & set "ADMIN_PW=" & set "JUDGE=mock"
for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
  if "%%a"=="LLM_API_KEY" set "LLM_KEY=%%b"
  if "%%a"=="ADMIN_USERNAME" if not "%%b"=="" set "ADMIN_USER=%%b"
  if "%%a"=="ADMIN_PASSWORD" set "ADMIN_PW=%%b"
  if "%%a"=="JUDGE_BACKEND" if not "%%b"=="" set "JUDGE=%%b"
)
if not defined LLM_KEY (
  echo Note: LLM_API_KEY is empty. Masking, uploads and the admin panel work;
  echo       a provider that needs a key refuses messages until you set one in .env.
)
if "%ADMIN_PW%"=="admin" (
  echo Note: ADMIN_PASSWORD is still 'admin'. Change it in .env, or from the account page
  echo       once you have signed in, before this machine is reachable by anyone else.
)
if /i "%JUDGE%"=="ollama" (
  where ollama >nul 2>&1 || (
    echo Note: JUDGE_BACKEND=ollama but the ollama command is not installed.
    echo       Stage 2 will fail and the fallback policy will apply.
  )
)
if defined ADMIN_PW (set "PASSWORD_HINT=the password from .env") else (set "PASSWORD_HINT=the password in data\initial-admin-password (written on first start)")

echo.
echo   Hub starting on http://127.0.0.1:%PORT%
echo   Sign in as '%ADMIN_USER%' with %PASSWORD_HINT%
echo   Press Ctrl+C to stop
echo.

rem Open the browser once the server has had a moment to start.
if defined BROWSER start "" /b cmd /c "ping -n 5 127.0.0.1 >nul & start http://127.0.0.1:%PORT%"
"%PY%" -m uvicorn polgate.main:app --host 127.0.0.1 --port %PORT% %RELOAD%
exit /b %errorlevel%

:usage
echo Start the hub on Windows: double-click this file, or run it from a terminal.
echo.
echo   start.bat              http://127.0.0.1:8000, auto-reload on file changes
echo   start.bat 8080         a different port
echo   start.bat --no-reload  no file watcher, closer to how it runs in production
echo   start.bat --no-browser do not open the browser
exit /b 0

:fail
echo.
echo   %~1
echo.
if not "%~2"=="" echo       %~2
echo.
pause
exit /b 1
