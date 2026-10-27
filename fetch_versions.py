#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CyberLink 官方版本号自动探测脚本（CI 环境用）

功能：
1. 调用官方 thank-you API 获取当前试用通道下载器直链
2. 解出下载器内 info.ini 的 DisplayBuildNumber（= 官方试用通道基线版本）
3. 走官方完整离线包接口 get-link-v2.jsp 拿完整安装器直链，版本号取自包文件名
4. 更新 data.json

说明（2026-10-27 排查结论）：
- 版本号与下载包同源由完整安装器保证：get-link-v2.jsp 是下载器运行时真正调用的
  官方接口，匿名可用，返回**完整安装器**直链（不是 downloader）+ md5，文件名带真实
  构建号 → 版本号取自包名，杜绝「显示 1019、下载到 0904 旧包」的错配。
- 参数必须凑齐，缺一个就 {"error":"000"}；SR 前缀决定产品（VDE=PD、PTD=PhD）。
- 官方匿名通道只铺「试用」基线（PowerDirector 长期停在 25.0.0904.0，
  PhotoDirector 甚至给的是安装框架版本 17.6.x），故试用值只作参考，不作产品版本。
- **不再从 nsane 等论坛下载站取版本**：论坛只是转载发布，标题里的版本是订阅 updater
  先推的构建，匿名拿不到对应完整包 → 会出现「版本号比包新」的错配。根因必须靠官方
  下载器 / 接口令牌解决。
- PhotoDirector 的试用值是安装框架版本（17.6.x），≠ 产品版本（18.x），
  用 trial_version_trusted=False 排除。
"""

import urllib.request
import urllib.parse
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
        # 完整离线包通道（2026-10-27）：与 PhotoDirector 同理，走下载器运行时真正
        # 调用的官方 get-link-v2.jsp，直接返回完整安装器直链 + md5，文件名带真实构建号。
        # 旧的 DirectorSuite365.exe 是 0904 的包（2026-09-14 修改），
        # 与页面显示的 25.0.1019.0 不同源 —— 正是「下载到旧版」的原因。
        'full_installer': True,
        'full_name': 'PowerDirector',
        'full_version': '25.0',
        # SR 取自下载器 info.ini 的 SR_No（VDE 前缀 = PowerDirector）；
        # 接口用 SR 识别产品线，返回的始终是最新构建（实测 VDE260910-01 → 1019 包）。
        'full_sr': ['VDE260910-01'],
        'full_lang': 'Enu',
        'download_url': 'https://build.cyberlink.com/Retail/PowerDirector/I8KJVKIFAO5/PowerDirector_1019_25.0.10_365_Essential_VDE261019-01.exe',
        'token': 'I8KJVKIFAO5',
        'base_version': '25.0.1019.0',
        'size': 722417392,
        'size_mb': 688.9,
        'build_date': '2026-10-20',
        'md5': 'a05038b60fd85f591ba82c5a88c4aa16',
    },
    'photodirector': {
        'name': 'PhotoDirector',
        'product_id': '211',
        # 完整离线包通道（2026-10-27）：下载器运行时调用的官方 get-link-v2.jsp
        # 直接返回完整安装器直链 + md5，文件名带真实构建号。
        # 旧的 DirectorSuite365.exe 是 0908 的包，与显示的版本号不同源。
        'full_installer': True,
        'full_name': 'PhotoDirector',
        'full_version': '17.0',
        # SR 前缀决定产品（PTD=PhotoDirector），两个都是下载器 info.ini 的真实 SR
        'full_sr': ['PTD260827-01', 'PTD260514-01'],
        'full_lang': 'Enu',
        'download_url': 'https://build.cyberlink.com/Retail/PhotoDirector/5F73OXUQ5M4N8J/PhotoDirector_18.0.10.1011.2120_bdded9f25_92_Essential_PTD260827-01.exe',
        'token': '5F73OXUQ5M4N8J',
        'base_version': '18.0.1011.0',
        'size': 697933848,
        'size_mb': 665.6,
        'build_date': '2026-10-12',
        'md5': '3e73107715cc97d0b3281c451431ca25',
        # 试用通道的 DisplayBuildNumber 是安装框架版本（17.6.x），≠ 产品版本
        'trial_version_trusted': False,
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


# 完整离线包接口（下载器运行时真正调用的那个）：
# 参数缺一个就返回 {"error":"000"}；SR 前缀决定产品（PTD=PhotoDirector）；
# downloader.cyberlink.com 在部分网络被 DNS 污染，www 同路径可用。
GETLINK_HOSTS = ['https://downloader.cyberlink.com', 'https://www.cyberlink.com']


def full_ver_from_name(fname):
    """从完整包文件名解产品版本（官方两种命名都支持）：

    - PhotoDirector_18.0.10.1011.2120_bdded9f25_92_Essential_PTD260827-01.exe
      连续 5 段数字：第 3 段是套件位（恒 10），第 4 段才是构建号 → 18.0.1011.0；
    - PowerDirector_1019_25.0.10_365_Essential_VDE261019-01.exe
      构建号在前、后接 major.minor.suite → 25.0.1019.0。
    """
    m = re.search(r'(\d+)\.(\d+)\.(\d+)\.(\d+)\.(\d+)', fname)
    if m:
        p = [int(x) for x in m.groups()]
        if p[2] < 100 <= p[3]:
            return f'{p[0]}.{p[1]}.{p[3]}.0'
        return canon_ver('.'.join(m.groups()))
    # 构建号在前：产品_1019_25.0.10_365_...
    m2 = re.search(r'_(\d{3,5})_(\d+)\.(\d+)\.(\d+)[_\.]', fname)
    if m2:
        return f'{m2.group(2)}.{m2.group(3)}.{int(m2.group(1))}.0'
    return ''


def fetch_full_installer(cfg):
    """调官方 get-link-v2.jsp 拿完整离线安装包直链。
    返回 {'url','md5','ver'}；失败返回 None。
    """
    name = cfg.get('full_name') or cfg['name']
    ver = str(cfg.get('full_version') or '')
    lang = cfg.get('full_lang') or 'Enu'
    srs = cfg.get('full_sr') or []
    if isinstance(srs, str):
        srs = [srs]
    for sr in srs:
        qs = ('?PRODUCTNAME={n}&PRODUCTVERSION={v}&VERSIONTYPE=trial&SR={sr}'
              '&VID=4.2.1.14316&LANGUAGE={l}&ostype=Windows').format(
                  n=urllib.parse.quote(name), v=urllib.parse.quote(ver),
                  sr=urllib.parse.quote(sr), l=urllib.parse.quote(lang))
        for host in GETLINK_HOSTS:
            try:
                data, _h, _f = http_get(host + '/prog/util/downloader/get-link-v2.jsp' + qs,
                                        timeout=25, referer='https://www.cyberlink.com/')
                obj = json.loads(data.decode('utf-8', 'ignore'))
            except Exception as e:
                print(f"  [!] 完整包接口不可用({host}): {e}", file=sys.stderr)
                continue
            link = str(obj.get('link') or '').strip()
            if not link:
                print(f"  [!] 完整包接口无直链(SR={sr}): {obj.get('error', obj)}", file=sys.stderr)
                continue
            fname = urllib.parse.unquote(link.rsplit('/', 1)[-1])
            if name.lower() not in fname.lower():
                print(f"  [!] 接口返回别的产品: {fname[:70]}", file=sys.stderr)
                continue
            out = {'url': link, 'md5': str(obj.get('md5') or ''), 'ver': full_ver_from_name(fname)}
            print(f"  完整离线包: {fname}")
            return out
    return None


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

    # 3. 完整离线包通道：官方接口直接给完整安装器直链，
    #    版本号取自包文件名 → 版本与下载包同源，不再用旧的 DirectorSuite 包。
    print("[2.5/3] 官方完整离线包接口（get-link-v2.jsp）...")
    full = fetch_full_installer(cfg) if cfg.get('full_installer') else None

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
    if full and full.get('ver'):
        version = full['ver']
        print(f"  ✓ 采用完整包版本: {version}（与下载包同源）")

    # 4. HEAD 刷新离线包大小/日期
    link = full['url'] if full else cfg['download_url']
    md5 = (full.get('md5') or cfg['md5']) if full else cfg['md5']
    info = verify_link(link) or {}
    build_date = info.get('build_date') or cfg['build_date']
    if full:
        # 版本就来自这个包，日期直接取包时间（不与旧配置值比大小）
        build_date = info.get('build_date') or build_date
    result = {
        'product': cfg['name'],
        'product_key': product_key,
        'version': version,
        'product_name': cfg['name'],
        'link': link,
        'token': cfg['token'],
        'downloader_url': downloader_url,
        'size': info.get('size', cfg['size']),
        'size_mb': info.get('size_mb', cfg['size_mb']),
        'build_date': build_date,
        'last_modified': info.get('last_modified', ''),
        'md5': md5,
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
