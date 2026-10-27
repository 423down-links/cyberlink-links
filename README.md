# CyberLink 离线包直链获取器

自动抓取 PowerDirector / PhotoDirector 最新离线安装包下载地址，并**自动识别真实版本号**。

## 在线访问

**GitHub Pages**: https://423down.github.io/cyberlink-links/

## 功能

- 每 2 小时自动抓取一次（GitHub Actions，UTC 偶数小时 → 北京时间 08~06 点整点）
- 自动识别真实版本号，且保证**版本号与下载包同源**（官方 get-link-v2 完整离线包接口）
- 显示：下载地址 / MD5 / 令牌 / 大小 / 版本 / 构建时间
- 一键复制
- 国内免登录直接浏览器访问

## 文件结构

```
├── index.html              # 主页面（读取 data.json，EMBEDDED_DATA 兜底由脚本自动写入）
├── data.json               # 数据文件（定时任务自动更新）
├── fetch_versions.py       # 版本抓取脚本（CI 运行）
├── .github/workflows/      # GitHub Actions 定时任务
│   └── update.yml
└── README.md
```

## 版本号自动识别原理

版本号有**三条来源**，按以下优先级取：

### 1. 完整离线包通道（`get-link-v2.jsp`，当前主用）

下载地址与版本号来自**同一个包**，不会出现「页面显示 1019、点下载却是 0904 旧包」。
接口是下载器运行时真正调用的那个（从下载器 NSIS 脚本头解出），匿名可用：

```
GET /prog/util/downloader/get-link-v2.jsp
      ?PRODUCTNAME={产品}&PRODUCTVERSION={主版本}.0&VERSIONTYPE=trial
      &SR={下载器 info.ini 里的 SR_No}&VID=4.2.1.14316&LANGUAGE=Enu&ostype=Windows
        ↓ 参数缺一个就返回 {"error":"000"}
{"link":".../{产品}_…_Essential_….exe","md5":"…"}   # 返回完整安装器，不是 downloader
```

- 版本号取自包文件名：
  PowerDirector `PowerDirector_1019_25.0.10_365_…` → `25.0.1019.0`；
  PhotoDirector `PhotoDirector_18.0.10.1011.2120_…` → `18.0.1011.0`
- `SR` 只用于识别产品线（`PTD`=PhotoDirector、`VDE`=PowerDirector），返回的始终是该产品最新构建
- 命中该通道时**自动跳过 nsane**：订阅 updater 的新构建匿名拿不到对应离线包，
  硬取会再次造成「版本号与下载包不同源」

### 2. 官方试用通道（根源接口）

```
GET /api/download/thank-you-page?ProductId={id}&ostype=Windows
        ↓ buildLink → 302 跟随
build.cyberlink.com/Retail/{产品}/{token}/CyberLink_{产品}_Downloader.exe
        ↓ 下载（约 9MB，7z/NSIS 封装）
解出 $_1_/info.ini
        ↓
DisplayBuildNumber = 官方试用通道当前基线版本
```

- PowerDirector → `25.0.0904.0`（VDE260910-01）
- PhotoDirector → `17.6.1826.0`（**这是安装框架版本，不是产品版本**）

### 3. 零售版版本源（nsane Software Updates 板块）

官方匿名通道**只铺试用基线**，零售新构建（Ultimate / Ultra 买断版）走买断/零售通道，
匿名官方接口取不到（已逐个实测）：

| 官方渠道 | 实测结果 |
|---------|---------|
| `support.cyberlink.com/patch-download` 补丁中心 | PowerDirector 只到 **2025** 线，无 2026/2027 |
| `www.cyberlink.com/api/download/box` 零售兑换 | 需有效产品密钥 + 人机验证 |
| `ProductId=270`（Ultimate Suite） | 302 正常，但 S3 对象匿名可下为 **403 AccessDenied** |
| 全量 ProductId 扫描（1~700） | 仅 `4`（PD trial）与 `211`（PhD）可用 |
| 零售离线包内 `info.ini` | 纯资源包，**无 info.ini**（HTTP Range + py7zr 只拉 0.07MB 可验证） |

因此改从 **nsane 论坛「Software Updates」板块列表页**取零售最新版：
该板块惯例是「一个软件更新开一个新帖」，**标题即精确版本号**
（如 `CyberLink PowerDirector Ultimate 2027 v25.0.1019`），列表页匿名可读（正文需登录）。
板块每页只有 25 帖，目标帖常被挤到后面几页，故翻 4 页并按 URL slug 匹配。

### 为什么不用 `version_display` 写死

旧方案把人工核实过的零售版号写进配置（`version_display`），**等于把版本钉死在常量上**——
官方再发新版它也不会动，只能人工改一次。这就是页面长期停在旧版的根因。

现在版本号完全由检测得出：官方完整包/试用通道铺开新版本 → 自动切到官方值；
零售先发 → 自动切到零售值。**不再需要人工改配置。**

- PhotoDirector 的试用值是框架版本，用 `trial_version_trusted=False` 排除
- 日期口径：完整包命中时取包 S3 `Last-Modified`（版本就来自这个包）；
  否则取「零售版发布日」与「包 Last-Modified」中较晚者（官方换包滞后时用发布日）

## 手动触发更新

仓库 → Actions → 自动更新 CyberLink 版本号 → Run workflow

## 注意事项

- 单轮耗时约 10 秒（下载 9MB 下载器 + 一次列表页抓取）
- 完整包直链里的 token 由官方实时下发，失效后重跑脚本即可获取新 token
- `downloader.cyberlink.com` 在部分网络被 DNS 污染，脚本自动回退 `www.cyberlink.com` 同路径
- nsane 板块列表页需带完整浏览器头（`Accept` / `Accept-Language`），否则 403/406
- GitHub Pages 首次部署需在仓库 Settings → Pages 中开启