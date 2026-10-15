#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CyberLink 官方版本号自动探测脚本（CI 环境用）

功能：
1. 调用官方 thank-you API 获取当前试用通道下载器直链
2. 解出下载器内 info.ini 的 DisplayBuildNumber（= 官方试用通道基线版本）
3. 用 base_version/version_display 防降级：探测值不比基线新 → 显示已核实版本
4. 更新 data.json

说明（2026-10-15 排查结论）：
- 试用下载器 info.ini 的 DisplayBuildNumber 即「官方当前分发基线」：
  PowerDirector=25.0.0904.0（VDE260910-01）、PhotoDirector=17.6.1826.0（框架版本）。
- 官方新构建（PowerDirector 25.0.1007.0 / PhotoDirector 18.0.1011.0）只走订阅
  updater 通道，匿名公开通道铺开前，用防降级显示已核实版本；官方换包后探测值
  超过 base_version 即自动对齐，无需人工改配置。
- 不再用旧 get-link-v2.jsp + VID 枚举 + PE 资源解析（离线包是纯资源包读不到主
  程序版本），全部走官方根源接口。
"""

import urllib.request
import re
import json
import os
import sys
import subprocess
import tempfile
import shutil
from datetime import datetime, timezone

# ============================================================
# 配置
# ============================================================

PRODUCTS = {
    'powerdirector': {
        'name': 'PowerDirector',
        'product_id': '4',
        'download_url': 'https://build.cyberlink.com/Retail/PowerDirector/CGQC8ZH75VM6/PowerDirector_DirectorSuite365.exe',
        'token': 'CGQC8ZH75VM6',
        'base_version': '25.0.0904.0',
        'version_display': '25.0.1007.0',
        'size': 724699336,
        'size_mb': 691.1,
        'build_date': '2026-10-08',
        'md5': 'be7750903c07baa523c0ab0328a1b27a',
    },
    'photodirector': {
        'name': 'PhotoDirector',
        'product_id': '211',
        'download_url': 'https://build.cyberlink.com/Retail/PhotoDirector/Y2QIW32JB74CH/PhotoDirector_DirectorSuite365.exe',
        'token': 'Y2QIW32JB74CH',
        'base_version': '18.0.1011.0',
        'version_display': '18.0.1011.0',
        'size': 669828616,
        'size_mb': 638.8,
        'build_date': '2026-10-14',
        'md5': '02769dcdca897ea1574c2de844c4d76e',
    },
}

THANKYOU_API = 'https://www.cyberlink.com/api/download/thank-you-page?ProductId={pid}&ostype=Windows'
THANKYOU_REFERER = 'https://www.cyberlink.com/prog/trial/download-notice.jsp?ProductId={pid}&ostype=Windows'

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

DATA_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data.json')

# ============================================================
# HTTP 工具
# ============================================================

def http_get(url, timeout=30, referer=None):
    headers = {'User-Agent': UA}
    if referer:
        headers['Referer'] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), resp.headers, resp.geturl()

def http_head(url, timeout=15):
    req = urllib.request.Request(url, headers={'User-Agent': UA}, method='HEAD')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.headers

# ============================================================
# 版本号归一与比较
# ============================================================

def canon_ver(v):
    """CyberLink 版本号归一：统一成下载器 info.ini DisplayBuildNumber 的 4 段式。
    5 段 PE 式 '25.0.0.0904.0'（build 在第 4 段，第 3 段恒 0）与 4 段式
    '25.0.0904.0'（build 在第 3 段）归一为同一写法，避免误判「更新」。
    """
    s = str(v).strip()
    parts = []
    for p in s.split('.'):
        m = re.match(r'(\d+)', p)
        if not m:
            return s
        parts.append(int(m.group(1)))
    if len(parts) == 5 and parts[2] == 0:
        return f'{parts[0]}.{parts[1]}.{parts[3]:04d}.{parts[4]}'
    return s

def ver_tuple(v):
    return tuple(int(p) for p in str(v).split('.') if p.isdigit())

def ver_newer(a, b):
    """a 是否严格新于 b（按段比较，缺失段补 0）"""
    ta, tb = ver_tuple(a), ver_tuple(b)
    n = max(len(ta), len(tb))
    return (ta + (0,) * (n - len(ta))) > (tb + (0,) * (n - len(tb)))

# ============================================================
# 7z 工具检测
# ============================================================

def get_seven_zip():
    for cmd in ['7z', '7za', '7zz', '/usr/bin/7z', '/usr/local/bin/7z']:
        try:
            r = subprocess.run([cmd, '--help'], capture_output=True, timeout=5)
            if r.returncode == 0:
                return cmd
        except Exception:
            pass
    print("[*] 未找到 7z，尝试安装...")
    try:
        subprocess.run(['apt-get', 'update', '-qq'], capture_output=True, timeout=60)
        subprocess.run(['apt-get', 'install', '-y', '-qq', 'p7zip-full'], capture_output=True, timeout=120)
        return '7z'
    except Exception as e:
        print(f"[!] apt 安装失败: {e}")
    return None

# ============================================================
# 官方探测
# ============================================================

def get_downloader_url(product_key):
    """官方 thank-you API → buildLink(user-add.do) 302 → 下载器直链"""
    cfg = PRODUCTS[product_key]
    pid = cfg['product_id']
    api = THANKYOU_API.format(pid=pid)
    referer = THANKYOU_REFERER.format(pid=pid)
    data, _headers, _final = http_get(api, timeout=20, referer=referer)
    info = json.loads(data.decode('utf-8', 'ignore'))
    build_link = info.get('buildLink', '')
    if not build_link:
        return '', ''
    if build_link.startswith('/'):
        build_link = 'https://www.cyberlink.com' + build_link
    try:
        _data, _h, final_url = http_get(build_link, timeout=20, referer=referer)
        return final_url, final_url
    except Exception as e:
        print(f"  [!] 跟随下载器跳转失败: {e}", file=sys.stderr)
        return '', ''

def extract_display_build(downloader_url, seven_zip, work_dir):
    """解出官方下载器内 info.ini 的 DisplayBuildNumber（= 官方当前分发基线版本）"""
    if not downloader_url:
        return ''
    try:
        req = urllib.request.Request(downloader_url, headers={'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=90) as resp:
            total = int(resp.headers.get('Content-Length', 0) or 0)
            if total > 40 * 1024 * 1024:
                print(f"  [!] 下载器体积过大({total})，跳过")
                return ''
            data = resp.read(40 * 1024 * 1024)
        exe_path = os.path.join(work_dir, 'downloader.exe')
        with open(exe_path, 'wb') as f:
            f.write(data)
        out_dir = os.path.join(work_dir, 'dl_out')
        os.makedirs(out_dir, exist_ok=True)
        subprocess.run([seven_zip, 'x', exe_path, '-o' + out_dir, '-y', '*/info.ini'],
                       capture_output=True, timeout=60)
        for root, _dirs, files in os.walk(out_dir):
            if 'info.ini' in files:
                with open(os.path.join(root, 'info.ini'), 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                m = re.search(r'DisplayBuildNumber\s*=\s*([0-9][0-9.]*)', content)
                if m:
                    print(f"  下载器声明基线版本: {m.group(1)}")
                    return m.group(1)
                break
    except Exception as e:
        print(f"  [!] 读取下载器版本失败: {e}", file=sys.stderr)
    return ''

def verify_link(link):
    """HEAD 刷新离线包大小/日期"""
    try:
        headers = http_head(link, timeout=15)
        size = int(headers.get('Content-Length', 0) or 0)
        last_modified = headers.get('Last-Modified', '')
        build_date = ''
        if last_modified:
            try:
                dt = datetime.strptime(last_modified, '%a, %d %b %Y %H:%M:%S %Z')
                build_date = dt.strftime('%Y-%m-%d')
            except Exception:
                build_date = last_modified
        return {'size': size, 'size_mb': round(size / 1024 / 1024, 1),
                'last_modified': last_modified, 'build_date': build_date}
    except Exception as e:
        print(f"  [!] 验证链接失败: {e}", file=sys.stderr)
        return None

# ============================================================
# 主流程
# ============================================================

def fetch_product(product_key, seven_zip, work_dir):
    cfg = PRODUCTS[product_key]
    print(f"\n{'='*60}")
    print(f"探测 {cfg['name']}")
    print(f"{'='*60}")

    # 1. 官方 thank-you API 拿下载器直链
    print("[1/2] 官方 thank-you API → 下载器直链...")
    downloader_url, _final = get_downloader_url(product_key)
    if not downloader_url:
        print("  [!] 未拿到官方下载器直链")
    else:
        print(f"  {downloader_url[:90]}...")

    # 2. 解下载器 info.ini 的 DisplayBuildNumber（官方当前分发基线）
    print("[2/2] 解下载器 info.ini 取官方基线版本...")
    detected = extract_display_build(downloader_url, seven_zip, work_dir)
    detected = canon_ver(detected) if detected else ''

    # 3. 防降级：探测值不比基线新 → 显示已核实版本；官方换包后自动对齐
    version = cfg['version_display']
    if detected and ver_newer(detected, cfg['base_version']):
        version = detected
        print(f"  ✓ 官方探测到更新版本: {version}（自动对齐）")
    else:
        if detected:
            print(f"  探测值 {detected} 未超过基线 {cfg['base_version']}，展示已核实版本 {version}")

    # 4. HEAD 刷新离线包大小/日期
    info = verify_link(cfg['download_url']) or {}
    result = {
        'product': cfg['name'],
        'product_key': product_key,
        'version': version,
        'product_name': cfg['name'],
        'link': cfg['download_url'],
        'token': cfg['token'],
        'downloader_url': downloader_url,
        'size': info.get('size', cfg['size']),
        'size_mb': info.get('size_mb', cfg['size_mb']),
        'build_date': info.get('build_date', cfg['build_date']),
        'last_modified': info.get('last_modified', ''),
        'md5': cfg['md5'],
        'fetched_at': datetime.now(timezone.utc).isoformat(),
    }
    print(f"\n结果: {cfg['name']} v{result['version']}")
    print(f"  链接: {result['link']}")
    print(f"  大小: {result['size_mb']} MB / 构建: {result['build_date']}")
    return result

def main():
    seven_zip = get_seven_zip()
    if not seven_zip:
        print("[!] 无法获取 7z 工具，退出")
        sys.exit(1)
    print(f"[*] 使用 7z: {seven_zip}")
    work_dir = tempfile.mkdtemp(prefix='cyberlink_')
    results = []
    for key in ['powerdirector', 'photodirector']:
        r = fetch_product(key, seven_zip, work_dir)
        if r:
            results.append(r)
    shutil.rmtree(work_dir, ignore_errors=True)
    output = {
        'updated_at': datetime.now(timezone.utc).isoformat(),
        'products': results,
    }
    with open(DATA_JSON, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n{'='*60}")
    print(f"已更新 {DATA_JSON}")
    print(f"{'='*60}")
    for r in results:
        print(f"  {r['product']}: v{r['version']} ({r['size_mb']} MB, {r['build_date']})")
    return 0 if results else 1


if __name__ == '__main__':
    sys.exit(main())
