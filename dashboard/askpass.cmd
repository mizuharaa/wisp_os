@echo off
rem ponytail: .cmd shim because SSH_ASKPASS must be one executable path; if
rem Win32-OpenSSH ever refuses batch files, ssh just prompts in the window.
python "%~dp0askpass.py" %*
