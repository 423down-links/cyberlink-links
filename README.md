# CyberLink 离线包直链获取器

自动抓取 PowerDirector / PhotoDirector 最新离线安装包下载地址，**自动识别真实版本号**（从安装包 7z 中提取主程序读取 PE 版本资源）。

## 在线访问

**GitHub Pages**: https://423down.github.io/cyberlink-links/

## 功能

- 每天定时自动抓取最新离线包链接（GitHub Actions，北京时间 8:00）
- 自动识别真实版本号（从安装包 7z 提取主程序，读取 PE .rsrc 版本资源）
- 显示：下载地址 / MD5 / 令牌 / 大小 / 版本 / 构建时间
- 一键复制
- 国内免登录直接浏览器访问

## 文件结构

```
├── index.html              # 主页面（读取 data.json）
├── data.json               # 数据文件（定时任务自动更新）
├── fetch_versions.py       # 版本抓取脚本（CI 运行）
├── .github/workflows/      # GitHub Actions 定时任务
│   └── update.yml
└── README.md
```

## 版本号自动识别原理

```
安装包结构：PE引导器 + NSIS overlay + 7z归档
                                    ↓
                          7z 有 2 个压缩块
                          Block 0: 大文件（479MB，媒体资源）
                          Block 1: 主程序（约10MB，含 PhotoDirector_365.exe）
                                    ↓
                          提取主程序（425KB）
                                    ↓
                          读取 PE .rsrc 节 VS_VERSION_INFO
                                    ↓
                          FileVersion = 真实版本号
```

- PowerDirector: `PowerDirector_365.exe` → FileVersion
- PhotoDirector: `PhotoDirector_365.exe` → FileVersion

## API 配置

| 产品 | ProductId | API 参数 |
|------|-----------|----------|
| PowerDirector | 407 | PRODUCTVERSION=24.0, VID=4.1.1.14809 |
| PhotoDirector | 411 | PRODUCTVERSION=17.0, VID=4.2.1.14316 |

通用：`sid=ffe03f96, CDKey=CLBiosSC, VERSIONTYPE=1, LANGUAGE=ENU`

## 手动触发更新

仓库 → Actions → 自动更新 CyberLink 版本号 → Run workflow

## 注意事项

- 版本号识别需要下载完整安装包（约 640-690MB），CI 运行约 3-5 分钟
- CyberLink API 服务器会轮询返回新旧版本，脚本多轮调用取最新
- 如果令牌失效，重新运行脚本即可获取新令牌
- GitHub Pages 首次部署需在仓库 Settings → Pages 中开启
