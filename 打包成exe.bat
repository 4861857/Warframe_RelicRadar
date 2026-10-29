@echo off
chcp 65001 >nul
REM ============================================================
REM  一键打包成单个 exe（无黑框窗口版），发给朋友直接双击运行
REM  前置：已安装 Python 3.10+，并已执行过  pip install -r requirements.txt
REM        以及  pip install pyinstaller
REM ============================================================
echo 正在打包，请稍候（首次可能需 1~3 分钟）...
pyinstaller --onefile --windowed ^
  --name "星际战甲报酬估价" ^
  --collect-all onnxruntime ^
  --collect-all rapidocr_onnxruntime ^
  --hidden-import mss ^
  --hidden-import keyboard ^
  --hidden-import rapidfuzz ^
  wf_price_tool.py
echo.
if exist "dist\星际战甲报酬估价.exe" (
  echo 成功！exe 已生成在 dist\ 目录下，把整个 dist 里的 exe 发给朋友即可。
) else (
  echo 打包失败，请检查上面的报错信息。
)
pause
