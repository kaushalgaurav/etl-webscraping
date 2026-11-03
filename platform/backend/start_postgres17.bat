@echo off
REM Start the PostgreSQL 17 cluster that holds the `platform` database.
REM
REM PostgreSQL 18 was installed later and owns the default port 5432. A v18
REM server cannot read a v17 data directory, so pointing the app at 5432 gives
REM 'database "platform" does not exist' — it is a different, empty cluster.
REM This starts v17 alongside it on 5433, which is what backend/.env expects.
REM
REM v17 has no Windows service (registering one needs administrator rights),
REM so this has to run after each reboot. Running it when the server is
REM already up is harmless: pg_ctl reports it and exits.

set PGBIN=C:\Users\HP\AppData\Local\PostgreSQL17\Library\bin
set PGDATA=C:\Users\HP\AppData\Local\PostgreSQL17\data
set PGLOG=%LOCALAPPDATA%\Temp\pg17.log

"%PGBIN%\pg_ctl.exe" -D "%PGDATA%" status >nul 2>&1
if %ERRORLEVEL% EQU 0 (
  echo PostgreSQL 17 is already running.
) else (
  echo Starting PostgreSQL 17 on port 5433...
  "%PGBIN%\pg_ctl.exe" -D "%PGDATA%" -o "-p 5433" -l "%PGLOG%" start
)
