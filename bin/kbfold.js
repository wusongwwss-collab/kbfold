#!/usr/bin/env node
'use strict';
/**
 * kbfold npm 壳 —— Node 内置模块 only，零第三方依赖。
 *
 * 职责单一：解析 argv，转发给系统 Python 运行判定内核 hosts/cli/kbfold_cli.py。
 * Python 定位顺序：Windows 先 `py -3`（官方启动器）再回退 `python` / `python3`；
 * 非 Windows 先 `python3` 再回退 `python`。全部缺席 ⇒ 打印友好指引，退出码 2。
 * 本壳不做任何判定、不读写用户文件、不联网。
 */
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');

const CLI = path.join(__dirname, '..', 'hosts', 'cli', 'kbfold_cli.py');

function pythonCandidates() {
  if (process.platform === 'win32') {
    return [['py', ['-3']], ['python', []], ['python3', []]];
  }
  return [['python3', []], ['python', []]];
}

function printNoPython() {
  process.stderr.write(
    'kbfold：未找到可用的 Python 3。\n' +
    '需要 Python 3.10+，https://www.python.org/downloads/\n' +
    '安装后重试：npx kbfold plan <目录> --demo\n'
  );
  process.exit(2);
}

function main() {
  if (!fs.existsSync(CLI)) {
    process.stderr.write('kbfold：缺少判定内核 ' + CLI + '（安装不完整？）\n');
    process.exit(2);
  }
  const userArgs = process.argv.slice(2);
  const candidates = pythonCandidates();
  let i = 0;

  function tryNext() {
    if (i >= candidates.length) {
      printNoPython();
      return;
    }
    const [cmd, pre] = candidates[i++];
    const child = spawn(cmd, pre.concat([CLI], userArgs), {
      stdio: 'inherit',
      windowsHide: true,
    });
    child.on('error', (err) => {
      // ENOENT / EACCES ⇒ 换下一个候选定位方式
      if (err && (err.code === 'ENOENT' || err.code === 'EACCES')) {
        tryNext();
        return;
      }
      process.stderr.write('kbfold：启动 Python 失败（' + (err && err.code ? err.code : err) + '）\n');
      process.exit(2);
    });
    child.on('exit', (code, signal) => {
      if (signal) {
        process.exit(1);
      }
      process.exit(code === null ? 1 : code);
    });
  }

  tryNext();
}

main();
