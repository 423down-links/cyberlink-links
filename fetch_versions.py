#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CyberLink 版本号自动抓取脚本（CI 环境用）

功能：
1. 调用 CyberLink API 获取最新离线包链接和MD5
2. 下载安装包，从 7z 提取主程序，读取 PE 版本资源获取真实版本号
3. 更新 data.json
4. 输出结果供 CI 提交

在 GitHub Actions / Gitee Go 中运行，每天定时执行。
"""

import urllib.request
import urllib.parse
import struct
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
        'product_id': '407',
        'api_params': 'PRODUCTNAME=PowerDirector&PRODUCTVERSION=24.0&VERSIONTYPE=1&sid=ffe03f96&CDKey=CLBiosSC&VID=4.2.1.14316&LANGUAGE=ENU&ostype=Windows',
        'main_exe': 'PowerDirector_365.exe',
        'default_version': '24.0',
    },
    'photodirector': {
        'name': 'PhotoDirector',
        'product_id': '411',
        'api_params': 'PRODUCTNAME=PhotoDirector&PRODUCTVERSION=17.0&VERSIONTYPE=1&sid=ffe03f96&CDKey=CLBiosSC&VID=4.2.1.14316&LANGUAGE=ENU&ostype=Windows',
        'main_exe': 'PhotoDirector_365.exe',
    },
}

API_URL = 'https://www.cyberlink.com/prog/util/downloader/get-link-v2.jsp'
ENTRY_URL = 'https://www.cyberlink.com/prog/trial/user-add.do?source=direct&ProductId={pid}&ostype=Windows'

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': '*/*',
}

DATA_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data.json')

# ============================================================
# 7z 工具检测与安装
# ============================================================

def get_seven_zip():
    """检测或安装 7z 工具"""
    for cmd in ['7z', '7za', '7zz', '/usr/bin/7z', '/usr/local/bin/7z',
                 '/home/user/Doubao/chats/38440390544390658/softofficelink/tools/7zz']:
        try:
            r = subprocess.run([cmd, '--help'], capture_output=True, timeout=5)
            if r.returncode == 0:
                return cmd
        except:
            pass
    print("[*] 未找到 7z，尝试安装...")
    try:
        subprocess.run(['apt-get', 'update', '-qq'], capture_output=True, timeout=60)
        subprocess.run(['apt-get', 'install', '-y', '-qq', 'p7zip-full'], capture_output=True, timeout=120)
        return '7z'
    except Exception as e:
        print(f"[!] apt 安装失败: {e}")
    try:
        print("[*] 下载 7-Zip 静态版...")
        url = 'https://www.7-zip.org/a/7z2301-linux-x64.tar.xz'
        tmp = tempfile.mkdtemp()
        tar_path = os.path.join(tmp, '7z.tar.xz')
        urllib.request.urlretrieve(url, tar_path)
        subprocess.run(['tar', 'xf', tar_path, '-C', tmp], capture_output=True, timeout=30)
        seven_zzs = os.path.join(tmp, '7zzs')
        os.chmod(seven_zzs, 0o755)
        shutil.copy2(seven_zzs, '/usr/local/bin/7zzs')
        return '/usr/local/bin/7zzs'
    except Exception as e:
        print(f"[!] 下载 7z 失败: {e}")
        return None

# ============================================================
# HTTP 工具
# ============================================================

def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), resp.headers

def http_head(url, timeout=15):
    req = urllib.request.Request(url, headers=HEADERS, method='HEAD')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.headers

# ============================================================
# API 抓取
# ============================================================

def get_downloader_token(product_key):
    cfg = PRODUCTS[product_key]
    url = ENTRY_URL.format(pid=cfg['product_id'])
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            final_url = resp.geturl()
            m = re.search(r'/Retail/[^/]+/([A-Z0-9]+)/', final_url)
            if m:
                return m.group(1), final_url
    except Exception as e:
        print(f"  [!] 获取下载器令牌失败: {e}", file=sys.stderr)
    return None, None

def fetch_offline_links(product_key, rounds=15):
    cfg = PRODUCTS[product_key]
    candidates = {}
    for i in range(rounds):
        try:
            url = f"{API_URL}?{cfg['api_params']}"
            data, _ = http_get(url, timeout=20)
            text = data.decode('utf-8', errors='replace')
            try:
                resp = json.loads(text)
                link = resp.get('link', '')
                api_md5 = resp.get('md5', '')
            except:
                m = re.search(r'(https?://build\.cyberlink\.com/Retail/[^"\s<>]+)', text)
                link = m.group(1) if m else ''
                api_md5 = ''
            if link:
                tm = re.search(r'/Retail/[^/]+/([A-Z0-9]+)/', link)
                token = tm.group(1) if tm else 'unknown'
                if token not in candidates:
                    candidates[token] = {'link': link, 'md5': api_md5}
                    print(f"  [{i+1}/{rounds}] 新候选: {token} (md5={api_md5[:16]}...)")
        except Exception as e:
            print(f"  [{i+1}/{rounds}] 错误: {e}", file=sys.stderr)
    return candidates


def verify_link(link):
    try:
        headers = http_head(link, timeout=15)
        size = int(headers.get('Content-Length', 0))
        last_modified = headers.get('Last-Modified', '')
        build_date = ''
        if last_modified:
            try:
                dt = datetime.strptime(last_modified, '%a, %d %b %Y %H:%M:%S %Z')
                build_date = dt.strftime('%Y-%m-%d')
            except:
                build_date = last_modified
        return {'size': size, 'size_mb': round(size/1024/1024, 1),
                'last_modified': last_modified, 'build_date': build_date}
    except Exception as e:
        print(f"  [!] 验证链接失败: {e}", file=sys.stderr)
        return None

# ============================================================
# PE 版本资源解析
# ============================================================

def find_overlay_start(pe_data):
    pe_offset = struct.unpack_from('<I', pe_data, 0x3C)[0]
    num_sections = struct.unpack_from('<H', pe_data, pe_offset + 6)[0]
    size_opt = struct.unpack_from('<H', pe_data, pe_offset + 20)[0]
    section_table = pe_offset + 24 + size_opt
    last_end = 0
    for i in range(num_sections):
        off = section_table + i * 40
        raw_ptr = struct.unpack_from('<I', pe_data, off + 20)[0]
        raw_size = struct.unpack_from('<I', pe_data, off + 16)[0]
        last_end = max(last_end, raw_ptr + raw_size)
    return last_end

def find_7z_in_overlay(overlay_data):
    return overlay_data.find(b'7z\xBC\xAF\x27\x1C')

def get_pe_version(pe_data):
    try:
        pe_offset = struct.unpack_from('<I', pe_data, 0x3C)[0]
        num_sections = struct.unpack_from('<H', pe_data, pe_offset + 6)[0]
        size_opt = struct.unpack_from('<H', pe_data, pe_offset + 20)[0]
        section_table = pe_offset + 24 + size_opt
        for i in range(num_sections):
            off = section_table + i * 40
            name = pe_data[off:off+8].rstrip(b'\x00').decode('ascii', errors='replace')
            raw_ptr = struct.unpack_from('<I', pe_data, off + 20)[0]
            raw_size = struct.unpack_from('<I', pe_data, off + 16)[0]
            if name == '.rsrc':
                rsrc = pe_data[raw_ptr:raw_ptr + raw_size].decode('utf-16-le', errors='replace')
                result = {}
                for k in ['FileVersion', 'ProductVersion', 'ProductName']:
                    m = re.search(k + r'\x00+([^\x00]{2,80})', rsrc)
                    if m:
                        result[k] = m.group(1).strip()
                return result
    except Exception as e:
        print(f"  [!] PE 版本解析失败: {e}", file=sys.stderr)
    return {}


# ============================================================
# 版本号自动识别
# ============================================================

def extract_version(download_url, main_exe, seven_zip, work_dir):
    """从安装包提取主程序，读取版本号"""
    try:
        print("  [1/4] 解析 PE 结构...")
        req = urllib.request.Request(download_url, headers={**HEADERS, 'Range': 'bytes=0-0x2000'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            pe_head = resp.read()
        overlay_start = find_overlay_start(pe_head)
        print(f"        Overlay: 0x{overlay_start:X}")
        req2 = urllib.request.Request(download_url, headers={**HEADERS, 'Range': f'bytes={overlay_start}-{overlay_start+0x5000}'})
        with urllib.request.urlopen(req2, timeout=30) as resp:
            overlay_head = resp.read()
        seven_z_in_overlay = find_7z_in_overlay(overlay_head)
        if seven_z_in_overlay < 0:
            print("  [!] 未找到 7z 签名")
            return {}
        seven_z_abs = overlay_start + seven_z_in_overlay
        print(f"        7z: 绝对 0x{seven_z_abs:X}")
        headers = http_head(download_url)
        total_size = int(headers.get('Content-Length', 0))
        print(f"        总大小: {total_size/1024/1024:.1f} MB")
        print(f"  [2/4] 下载安装包（{total_size/1024/1024:.1f} MB）...")
        archive_path = os.path.join(work_dir, 'installer.exe')
        urllib.request.urlretrieve(download_url, archive_path)
        print(f"        已下载: {os.path.getsize(archive_path)/1024/1024:.1f} MB")
        seven_z_path = os.path.join(work_dir, 'archive.7z')
        with open(archive_path, 'rb') as f:
            f.seek(seven_z_abs)
            seven_z_data = f.read()
        with open(seven_z_path, 'wb') as f:
            f.write(seven_z_data)
        print(f"  [3/4] 提取 {main_exe}...")
        extract_dir = os.path.join(work_dir, 'extracted')
        os.makedirs(extract_dir, exist_ok=True)
        r = subprocess.run([seven_zip, 'x', '-y', seven_z_path, main_exe, f'-o{extract_dir}'],
                          capture_output=True, timeout=180)
        if r.returncode != 0:
            err = r.stderr.decode('utf-8', errors='replace')[:300]
            print(f"  [!] 提取失败: {err}")
            return {}
        exe_path = None
        for root, dirs, files in os.walk(extract_dir):
            for f in files:
                if f.lower() == main_exe.lower():
                    exe_path = os.path.join(root, f)
                    break
            if exe_path:
                break
        if not exe_path:
            print(f"  [!] 未找到 {main_exe}")
            return {}
        print(f"        已提取: {os.path.getsize(exe_path)} 字节")
        print("  [4/4] 读取 PE 版本资源...")
        with open(exe_path, 'rb') as f:
            exe_data = f.read()
        version_info = get_pe_version(exe_data)
        print(f"        版本: {version_info.get('FileVersion', 'N/A')}")
        return version_info
    except Exception as e:
        print(f"  [!] 版本号识别失败: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return {}


# ============================================================
# 主流程
# ============================================================

def fetch_product(product_key, seven_zip, work_dir, get_version=True):
    cfg = PRODUCTS[product_key]
    print(f"\n{'='*60}")
    print(f"抓取 {cfg['name']}")
    print(f"{'='*60}")
    print("\n[1/3] 获取下载器令牌...")
    token, downloader_url = get_downloader_token(product_key)
    print(f"  令牌: {token}")
    print("\n[2/3] 调用 API 获取离线包链接...")
    candidates = fetch_offline_links(product_key, rounds=15)
    print(f"  共 {len(candidates)} 个候选")
    if not candidates:
        print("  [!] 未获取到链接")
        return None
    print("\n[3/3] 验证候选...")
    best = None
    for tok, cand in candidates.items():
        link = cand['link']
        api_md5 = cand.get('md5', '')
        info = verify_link(link)
        if info:
            print(f"  {tok}: {info['size_mb']} MB / {info['build_date']} / md5={api_md5[:16]}...")
            if best is None or info['build_date'] > best['build_date']:
                best = {**info, 'token': tok, 'link': link, 'md5': api_md5}
    if not best:
        print("  [!] 全部验证失败")
        return None
    version_info = {}
    if get_version:
        print(f"\n[4/4] 自动识别版本号...")
        version_info = extract_version(best['link'], cfg['main_exe'], seven_zip, work_dir)
    # 版本号提取失败时使用配置的默认版本号
    version = version_info.get('FileVersion', '') or cfg.get('default_version', '')
    result = {
        'product': cfg['name'],
        'product_key': product_key,
        'version': version,
        'product_name': version_info.get('ProductName', cfg['name']),
        'link': best['link'],
        'token': best['token'],
        'downloader_url': downloader_url,
        'downloader_token': token,
        'size': best['size'],
        'size_mb': best['size_mb'],
        'build_date': best['build_date'],
        'last_modified': best['last_modified'],
        'md5': best.get('md5', ''),
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
    existing = {}
    if os.path.exists(DATA_JSON):
        with open(DATA_JSON, 'r', encoding='utf-8') as f:
            old = json.load(f)
            for p in old.get('products', []):
                existing[p['product_key']] = p
    work_dir = tempfile.mkdtemp(prefix='cyberlink_')
    results = []
    for key in ['powerdirector', 'photodirector']:
        r = fetch_product(key, seven_zip, work_dir, get_version=True)
        if r:
            if key in existing and existing[key].get('link') == r['link']:
                r['md5'] = existing[key].get('md5', '')
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
