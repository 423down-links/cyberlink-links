#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CyberLink 官方版本号自动探测脚本（CI 环境用）

功能：
1. 调用官方 thank-you API 获取当前试用通道下载器直链
2. 解出下载器内 info.ini 的 DisplayBuildNumber（= 官方试用通道基线版本）
3. 再从 nsane「Software Updates」板块标题取零售版（Ultimate/Ultra 买断版）最新版本，
   与官方试用通道取较新者
4. 更新 data.json

说明（2026-10-25 排查结论）：
- 试用下载器 info.ini 的 DisplayBuildNumber 即「官方试用通道当前基线」：
  PowerDirector=25.0.0904.0（VDE260910-01）、PhotoDirector=17.6.1826.0（框架版本）。
- 官方匿名通道只铺「试用」基线，零售新构建（PowerDirector 25.0.1019.0 等）走买断/
  零售通道，匿名官方接口取不到（补丁中心只到 2025 线、零售兑换需密钥、ProductId=270
  的 Ultimate Suite 通道 S3 对象 403）。
- 旧方案用 version_display 写死人工核实的零售版号，等于把版本钉死在常量上，
  官方再发新版也不会动 → 这正是「版本长期停在旧版」的根因。
- 现改为：零售版版本号由 nsane 板块标题自动解析，与官方试用通道**双向取较新者**。
  官方试用通道铺开新版本后自动对齐；零售先发时也能跟上，不再需要人工改配置。
- PhotoDirector 的试用值是安装框架版本（17.6.x），≠ 产品版本（18.x），
  用 trial_version_trusted=False 排除。
- 不再用旧 get-link-v2.jsp + VID 枚举 + PE 资源解析（离线包是纯资源包读不到主
  程序版本），全部走官方根源接口 + 零售版本源。
"""

import urllib.request
import re
import json
import os
import sys
import html as html_mod
import subprocess
import tempfile
import shutil
from datetime import datetime, timezone, timedelta

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
        'size': 724699336,
        'size_mb': 691.1,
        'build_date': '2026-09-14',
        'md5': 'be7750903c07baa523c0ab0328a1b27a',
        # 零售版版本源（见 fetch_retail_version 说明）
        'retail_nsane': True,
        'retail_keywords': ['powerdirector'],
    },
    'photodirector': {
        'name': 'PhotoDirector',
        'product_id': '211',
        'download_url': 'https://build.cyberlink.com/Retail/PhotoDirector/Y2QIW32JB74CH/PhotoDirector_DirectorSuite365.exe',
        'token': 'Y2QIW32JB74CH',
        'base_version': '18.0.1011.0',
        'size': 669828616,
        'size_mb': 638.8,
        'build_date': '2026-09-08',
        'md5': '02769dcdca897ea1574c2de844c4d76e',
        'retail_nsane': True,
        'retail_keywords': ['photodirector'],
        # 试用通道的 DisplayBuildNumber 是安装框架版本（17.6.x），≠ 产品版本
        'trial_version_trusted': False,
    },
}

THANKYOU_API = 'https://www.cyberlink.com/api/download/thank-you-page?ProductId={pid}&ostype=Windows'
THANKYOU_REFERER = 'https://www.cyberlink.com/prog/trial/download-notice.jsp?ProductId={pid}&ostype=Windows'

# 零售版（Ultimate/Ultra 买断版）版本源：nsane 论坛「Software Updates」板块。
# 官方匿名通道（thank-you API → 试用下载器）只给试用基线（PowerDirector 长期停在
# 25.0.0904.0），零售新构建（如 25.0.1019.0）走买断/零售通道，匿名官方接口取不到；
# 该板块惯例是「一个软件更新开一个新帖」，标题即精确版本号，列表页匿名可读。
NSANE_SOFTWARE_UPDATES = 'https://nsaneforums.com/forum/48-software-updates/'

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


def fetch_retail_version(cfg):
    """零售版（Ultimate/Ultra 买断版）最新版本号。

    官方匿名通道只给「试用基线」（PowerDirector 长期停在 25.0.0904.0），
    零售新构建走买断通道取不到 → 改从 nsane「Software Updates」板块标题取：
    板块惯例是「一个软件更新开一个新帖」，标题即精确版本号，列表页匿名可读。

    返回 (version, date)；取不到返回 ('', '')。
    """
    keywords = [str(k).strip().lower() for k in (cfg.get('retail_keywords') or [])
                if str(k).strip()]
    if not keywords:
        return '', ''

    headers = {
        'User-Agent': UA,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    try:
        req = urllib.request.Request(NSANE_SOFTWARE_UPDATES, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode('utf-8', 'ignore')
    except Exception as e:
        print(f"  [!] 零售版版本源抓取失败: {e}", file=sys.stderr)
        return '', ''

    best_ver = ''
    best_date = ''
    for m in re.finditer(
            r"<a href='(https://nsaneforums\.com/topic/[^']+)'[^>]*title='([^']*)'", html):
        title = html_mod.unescape(m.group(2)).strip()
        low = title.lower()
        if not any(k in low for k in keywords):
            continue
        vm = re.search(r'(\d+)\.(\d+)\.(\d{4})(?:\.(\d+))?', title)
        if not vm:
            continue
        ver = f'{vm.group(1)}.{vm.group(2)}.{vm.group(3)}.{vm.group(4) or "0"}'
        if not best_ver or ver_newer(ver, best_ver):
            best_ver = ver
            # 该行所属 <li> 块里第一个 <time> 是发帖时间（板块惯例：一个更新一个新帖）
            blk_start = html.rfind('data-rowID=', 0, m.start())
            blk_end = html.find('data-rowID=', m.start())
            blk = html[blk_start:blk_end if blk_end > 0 else m.start() + 4000]
            tm = re.search(r"<time datetime='([^']+)'", blk)
            if tm:
                try:
                    dt = datetime.strptime(tm.group(1), '%Y-%m-%dT%H:%M:%SZ')
                    best_date = (dt + timedelta(hours=8)).strftime('%Y-%m-%d')
                except Exception:
                    pass
    if best_ver:
        print(f"  零售版版本(nsane): {best_ver}" + (f" @ {best_date}" if best_date else ""))
    return best_ver, best_date

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
    print("[1/3] 官方 thank-you API → 下载器直链...")
    downloader_url, _final = get_downloader_url(product_key)
    if not downloader_url:
        print("  [!] 未拿到官方下载器直链")
    else:
        print(f"  {downloader_url[:90]}...")

    # 2. 解下载器 info.ini 的 DisplayBuildNumber（官方试用通道当前基线）
    print("[2/3] 解下载器 info.ini 取官方试用通道基线...")
    detected = extract_display_build(downloader_url, seven_zip, work_dir)
    detected = canon_ver(detected) if detected else ''

    # 3. 零售版（Ultimate/Ultra 买断版）最新版本：官方试用通道长期滞后于零售新构建，
    #    零售源与官方通道取较新者 → 官方铺开新版本时自动对齐，零售先发时也能跟上。
    print("[3/3] 零售版版本源（nsane Software Updates 板块）...")
    retail_ver, retail_date = ('', '')
    if cfg.get('retail_nsane'):
        retail_ver, retail_date = fetch_retail_version(cfg)

    version = cfg.get('version_display') or cfg['base_version']
    # 官方试用通道的 DisplayBuildNumber 对 PhotoDirector 是「安装框架版本」（17.6.x），
    # ≠ 产品版本（18.x），不能拿它当产品版本号。
    trial_trusted = cfg.get('trial_version_trusted', True)
    if detected and trial_trusted and ver_newer(detected, cfg['base_version']):
        version = detected
        print(f"  ✓ 官方试用通道探测到更新版本: {version}（自动对齐）")
    elif detected:
        if trial_trusted:
            print(f"  官方试用通道值 {detected} 未超过基线 {cfg['base_version']}")
        else:
            print(f"  官方试用通道值 {detected}（框架版，不作产品版本）")
    if retail_ver:
        if not version or ver_newer(retail_ver, version):
            version = retail_ver
            print(f"  ✓ 采用零售版版本: {version}")
        else:
            print(f"  零售版 {retail_ver} 不比当前 {version} 新，保留当前")

    # 4. HEAD 刷新离线包大小/日期
    info = verify_link(cfg['download_url']) or {}
    build_date = info.get('build_date') or cfg['build_date']
    # 零售版发布日晚于包时间时（官方换包滞后）用发布日，否则新版本日期会被写回旧包时间
    if retail_date and retail_date > (build_date or ''):
        build_date = retail_date
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
        'build_date': build_date,
        'last_modified': info.get('last_modified', ''),
        'md5': cfg['md5'],
        'fetched_at': datetime.now(timezone.utc).isoformat(),
    }
    print(f"\n结果: {cfg['name']} v{result['version']}")
    print(f"  链接: {result['link']}")
    print(f"  大小: {result['size_mb']} MB / 构建: {result['build_date']}")
    return result

def embed_index_html(output):
    """把 data.json 的内容同步进 index.html 的 EMBEDDED_DATA 兜底块。

    手写兜底数据会随时间腐化（曾长期停在 25.0.0904.0 + 已失效的旧 token），
    这里改成每次跑完自动写入，保证兜底显示与 data.json 一致。
    """
    index_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html')
    if not os.path.exists(index_path):
        return
    with open(index_path, 'r', encoding='utf-8') as f:
        html = f.read()
    m = re.search(r'var EMBEDDED_DATA = \{.*?\n\};', html, re.S)
    if not m:
        print("[!] index.html 里没找到 EMBEDDED_DATA 块，跳过嵌入")
        return
    # 只保留页面用到的字段，保持块体与原手写格式一致
    slim = {
        'updated_at': output.get('updated_at', ''),
        'products': [{
            'product': p.get('product', ''),
            'product_key': p.get('product_key', ''),
            'version': p.get('version', ''),
            'link': p.get('link', ''),
            'token': p.get('token', ''),
            'size': p.get('size', 0),
            'build_date': p.get('build_date', ''),
            'md5': p.get('md5', ''),
        } for p in output.get('products', [])],
    }
    block = 'var EMBEDDED_DATA = ' + json.dumps(slim, ensure_ascii=False, indent=2) + ';'
    html = html[:m.start()] + block + html[m.end():]
    with open(index_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"[*] 已同步 index.html 的 EMBEDDED_DATA")


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
    # 时间戳只在「实质内容变了」时才刷新：否则每轮跑都因时间戳变化触发一次
    # 无意义提交（CI 每次都推送同样的版本号），徒增仓库历史噪音。
    prev = {}
    if os.path.exists(DATA_JSON):
        try:
            with open(DATA_JSON, 'r', encoding='utf-8') as f:
                prev = json.load(f)
        except Exception:
            prev = {}
    if prev:
        def _core(d):
            return [{k: v for k, v in p.items() if k not in ('fetched_at',)}
                    for p in d.get('products', [])]
        if _core(prev) == _core(output):
            output['updated_at'] = prev.get('updated_at', output['updated_at'])
            for i, p in enumerate(output['products']):
                if i < len(prev.get('products', [])):
                    p['fetched_at'] = prev['products'][i].get('fetched_at', p.get('fetched_at'))
            print("[*] 版本/大小/日期均无变化，保留原时间戳")
    with open(DATA_JSON, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    embed_index_html(output)
    print(f"\n{'='*60}")
    print(f"已更新 {DATA_JSON}")
    print(f"{'='*60}")
    for r in results:
        print(f"  {r['product']}: v{r['version']} ({r['size_mb']} MB, {r['build_date']})")
    return 0 if results else 1


if __name__ == '__main__':
    sys.exit(main())
