@echo off
rem Tail-call the shared source launcher, preserving cwd, arguments and exit code.
"%~dp0astra.bat" %*
