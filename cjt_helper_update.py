#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CJT Helper 自动更新脚本（Native Messaging Host）
"""

import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import urllib.request
import zipfile

CHUNK_SIZE = 64 * 1024
send_lock = threading.Lock()
update_lock = threading.Lock()
cancel_event = threading.Event()
update_thread = None
LOG_FILE = os.path.expanduser('~/.cjt-helper/auto-update.log')


class UpdateCanceled(Exception):
    """用于中断更新流程的取消异常。"""


def send_message(payload):
    """发送消息到 Chrome（遵循 Native Messaging 长度前缀协议）。"""
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    # 多线程场景下保证写入原子性，避免消息交错
    with send_lock:
        sys.stdout.buffer.write(struct.pack('<I', len(data)))
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def write_log(text):
    """写入本地日志，便于排查 Native Host 是否启动。"""
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, 'a', encoding='utf-8') as handler:
            handler.write(text + '\n')
    except Exception:
        # 日志失败不影响更新主流程
        pass


def read_message():
    """读取来自 Chrome 的消息。"""
    raw_length = sys.stdin.buffer.read(4)
    if not raw_length:
        return None
    message_length = struct.unpack('<I', raw_length)[0]
    message_data = sys.stdin.buffer.read(message_length)
    if not message_data:
        return None
    return json.loads(message_data.decode('utf-8'))


def download_with_progress(url, dest_path):
    """下载更新包，并实时上报下载进度。"""
    request = urllib.request.Request(url, headers={'User-Agent': 'cjt-helper-updater'})
    with urllib.request.urlopen(request) as response, open(dest_path, 'wb') as target:
        total = int(response.headers.get('Content-Length') or 0)
        downloaded = 0
        last_percent = -1
        while True:
            if cancel_event.is_set():
                raise UpdateCanceled('已取消自动更新')
            chunk = response.read(CHUNK_SIZE)
            if not chunk:
                break
            target.write(chunk)
            downloaded += len(chunk)
            if total > 0:
                percent = int(downloaded * 100 / total)
                if percent != last_percent:
                    last_percent = percent
                    send_message({
                        'status': 'progress',
                        'phase': 'download',
                        'percent': percent,
                        'text': f'正在下载更新包（{percent}%）'
                    })
            elif downloaded % (CHUNK_SIZE * 20) == 0:
                send_message({
                    'status': 'progress',
                    'phase': 'download',
                    'percent': 0,
                    'text': '正在下载更新包...'
                })


def extract_with_progress(zip_path, target_dir):
    """解压更新包，并实时上报解压进度。"""
    with zipfile.ZipFile(zip_path, 'r') as archive:
        members = archive.infolist()
        total = len(members)
        if total == 0:
            return
        for index, member in enumerate(members, 1):
            if cancel_event.is_set():
                raise UpdateCanceled('已取消自动更新')
            archive.extract(member, target_dir)
            percent = int(index * 100 / total)
            send_message({
                'status': 'progress',
                'phase': 'extract',
                'percent': percent,
                'text': f'正在解压更新包（{percent}%）'
            })


def resolve_target_dir(message):
    """确定更新包解压目录。"""
    target_dir = message.get('targetDir')
    if target_dir:
        return os.path.expanduser(os.path.expandvars(target_dir))
    return os.path.join(os.path.expanduser('~'), '.cjt-helper', 'auto-update')


def ensure_clean_target_dir(target_dir):
    """清理更新目录，避免旧文件导致覆盖失败或混用。"""
    if not os.path.exists(target_dir):
        os.makedirs(target_dir, exist_ok=True)
        return

    # 安全保护：仅清理 ~/.cjt-helper 或以 cjt-helper 结尾的目录
    home_dir = os.path.expanduser('~')
    safe_root = os.path.join(home_dir, '.cjt-helper')
    try:
        is_under_safe_root = os.path.commonpath([target_dir, safe_root]) == safe_root
    except Exception as error:
        write_log(f'skip_clean_error: {error}')
        return

    # 允许清理以 cjt-helper 结尾的目录（用于插件安装目录）
    normalized = os.path.normpath(target_dir)
    allow_by_name = os.path.basename(normalized).lower() == 'cjt-helper'

    if not is_under_safe_root and not allow_by_name:
        write_log(f'skip_clean: {target_dir}')
        return

    for name in os.listdir(target_dir):
        item_path = os.path.join(target_dir, name)
        try:
            # 删除目录或文件，避免旧内容影响新的解压结果
            if os.path.isdir(item_path) and not os.path.islink(item_path):
                shutil.rmtree(item_path)
            else:
                os.remove(item_path)
        except Exception as error:
            raise RuntimeError(f'清理更新目录失败: {error}')


def normalize_path(path):
    """展开路径中的 ~ 和环境变量。"""
    if not path:
        return ''
    return os.path.expanduser(os.path.expandvars(path))


def check_install_dir(path):
    """校验安装目录是否合法（存在且为目录）。"""
    normalized = normalize_path(path)
    if not normalized:
        return False, '', '目录为空'
    if os.path.isdir(normalized):
        return True, normalized, ''
    return False, normalized, '目录不存在或不是文件夹'


def handle_start_update(message):
    """处理自动更新主流程：下载、解压、发送完成状态。"""
    download_url = message.get('downloadUrl')
    if not download_url:
        send_message({'status': 'error', 'text': '缺少下载地址'})
        return

    target_dir = resolve_target_dir(message)
    os.makedirs(target_dir, exist_ok=True)

    write_log('start_update: 开始下载更新包')
    send_message({'status': 'log', 'text': '开始下载更新包...'})

    temp_dir = tempfile.mkdtemp(prefix='cjt_helper_update_')
    zip_path = os.path.join(temp_dir, 'cjt_helper_update.zip')

    try:
        download_with_progress(download_url, zip_path)
        # 下载完成后再清理，避免下载失败导致旧包被清空
        ensure_clean_target_dir(target_dir)
        write_log('download_done: 开始解压')
        send_message({'status': 'log', 'text': '下载完成，开始解压...'})
        extract_with_progress(zip_path, target_dir)
        write_log(f'complete: {target_dir}')
        send_message({'status': 'complete', 'path': target_dir})
    except UpdateCanceled as error:
        write_log(f'canceled: {error}')
        send_message({'status': 'canceled', 'text': str(error)})
    except Exception as error:
        write_log(f'error: {error}')
        send_message({'status': 'error', 'text': str(error)})
    finally:
        try:
            shutil.rmtree(temp_dir)
        except Exception:
            pass


def run_update(message):
    """在独立线程中执行更新，避免阻塞消息读取。"""
    global update_thread
    try:
        handle_start_update(message)
    finally:
        with update_lock:
            update_thread = None


def main():
    global update_thread
    write_log('native host started')
    while True:
        message = read_message()
        if message is None:
            break
        cmd = message.get('cmd')
        if cmd == 'start_update':
            with update_lock:
                if update_thread and update_thread.is_alive():
                    write_log('reject: update already running')
                    send_message({'status': 'error', 'text': '已有更新任务正在执行'})
                    continue
                cancel_event.clear()
                write_log('start_update: thread created')
                update_thread = threading.Thread(target=run_update, args=(message,), daemon=True)
                update_thread.start()
        elif cmd == 'cancel_update':
            # 设置取消标记，由更新线程在合适时机退出
            cancel_event.set()
            write_log('cancel_update: received')
            send_message({'status': 'log', 'text': '已发送取消指令'})
        elif cmd == 'check_install_dir':
            raw_path = message.get('path', '')
            ok, normalized, error_text = check_install_dir(raw_path)
            write_log(f'check_install_dir: {normalized} ok={ok}')
            if ok:
                send_message({'status': 'check_install_dir', 'ok': True, 'path': normalized})
            else:
                send_message({
                    'status': 'check_install_dir',
                    'ok': False,
                    'path': normalized,
                    'text': error_text
                })
        else:
            write_log(f'unknown cmd: {cmd}')
            send_message({'status': 'error', 'text': '未知指令'})


if __name__ == '__main__':
    main()
