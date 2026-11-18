@echo off
rem Host de native messaging para DESARROLLO (sin instalar): usa el venv del repo.
rem En un equipo con Micnaga instalado el manifiesto apunta a micnaga-host.exe.
"%~dp0..\..\.venv\Scripts\python.exe" -m micnaga.watcher.extension %*
