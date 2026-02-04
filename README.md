# python-to-exe
如果你没有 Windows 电脑或虚拟机，这是最简单、最干净的方法。利用 GitHub 的免费云端服务器（Windows 环境）来帮你打包。

步骤：
1、将你的 install_host.py 代码上传到一个 GitHub 仓库。
2、在仓库中创建目录 .github/workflows。
3、在该目录下创建一个文件 build.yml，内容如下：

```yaml
name: Build Windows Exe

on: [push]

jobs:
  build:
    runs-on: windows-latest  # 指定在 Windows 环境运行

    steps:
    - uses: actions/checkout@v3

    - name: Set up Python
      uses: actions/setup-python@v4
      with:
        python-version: '3.9' # 根据需要选择版本

    - name: Install dependencies
      run: |
        python -m pip install --upgrade pip
        pip install pyinstaller

    - name: Build EXE
      run: |
        # 这里执行打包命令
        pyinstaller --onefile --clean install_host.py

    - name: Upload Artifact
      uses: actions/upload-artifact@v3
      with:
        name: native-messaging-host-exe
        path: dist/install_host.exe

```

结果：
当你提交代码后，GitHub Actions 会自动运行。大约 1-2 分钟后，你在该 Action 的页面底部就能下载到打包好的 install_host.exe 文件。这个文件就是原生的 Windows 可执行文件。
