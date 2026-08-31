# Google Docs Markdown 导入（实验性）

该入口把本地 Markdown 安全转换为 DOCX，再通过 Google Drive API 创建原生 Google 文档。转换由固定版本、经过 SHA-256 校验的 Pandoc 完成，普通用户无需单独安装 Pandoc。

## 最快使用

首次使用需要先在 Google Cloud 做一次配置：

1. 创建或选择一个 Google Cloud 项目，在“API 和服务 / API 库”中启用 **Google Drive API**。
2. 在 Google Auth Platform 配置应用受众。个人账号或跨组织使用请选择 **External（外部）**；保持 **Testing（测试）** 时，在“Test users（测试用户）”中加入稍后要在 Chrome 授权页登录的 Google 账号。具体规则见 [Google Cloud 的受众说明](https://support.google.com/cloud/answer/15549945)。
3. 在“Data Access（数据访问）”中声明 `drive.file`，然后创建 **Desktop app（桌面应用）** 类型的 OAuth 2.0 客户端并下载 JSON。不要创建“Web application（Web 应用）”客户端；桌面应用与本地回调是 [Google 官方支持的安装应用流程](https://developers.google.com/identity/protocols/oauth2/native-app)。
4. 在万能导“设置 > 自动化浏览器”中选择 Chrome。插件会优先用这里配置的 Chrome 打开授权页；未配置时会自动探测 Chrome/Chromium，再回退到系统默认浏览器。
5. 在插件中选择 OAuth JSON，点击“授权 Google Docs”，并在 Chrome 中选择第 2 步加入测试用户的同一个 Google 账号。
6. 选择包含 `.md` 和图片资源的目录，点击“开始导入”。

首次成功授权后，日常使用只需第 6 步；OAuth JSON 无需重复选择。只有凭证被撤销、过期或更换 Google 账号时，才需要重新执行第 5 步。转换引擎会在第一次导入时自动下载并校验，不需要安装 Pandoc。“预览文件数量”和“先测试一篇”只是可选的预检操作，不执行也能直接导入。

## 测试用户与发布状态

- **测试用户邮箱不必与 Cloud 项目所有者或“用户支持邮箱”一致。**它必须与 Chrome 授权页中实际选择的 Google 账号一致；如果浏览器同时登录了多个账号，请明确选择名单中的账号。
- External 应用处于 Testing 时，只有列入 Test users 的账号能够授权，最多可列 100 个测试用户。Google 可能显示测试/未验证应用提示；只有在确认页面显示的是你自己创建的 Cloud 项目和预期权限时才继续。
- 对本插件使用的 Drive 权限，Testing 状态下的授权通常在同意后 7 天到期，离线 refresh token 也会到期。出现凭证过期提示时，重新点击“授权 Google Docs”即可。
- 如果项目属于 Google Workspace/Cloud Identity 组织且只供组织内部使用，可按管理员策略选择 Internal；组织外账号不能授权 Internal 应用。
- 若要让任意外部用户长期使用，需要切换到 In production，并按 Google 当时的要求完成应用发布或验证。导入只请求按文件授权的 `drive.file`，不请求读取用户全部 Drive 的 `drive.readonly`。

## OAuth 权限与凭据安全

插件只请求 Google 推荐的非敏感、按文件授权的 [`drive.file`](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)，用于创建文档、检查由本插件创建的重复项，以及管理用户明确交给该 OAuth 客户端的文件；它不会因此获得读取整个 Google Drive 的权限。授权时会从 OAuth JSON 读取客户端信息，并把刷新令牌和刷新所需的客户端字段保存到 Wandao 的插件私有数据目录。凭证被撤销、过期或权限不足时，插件会提示重新授权。请勿把 OAuth JSON、令牌或私人测试数据提交到 Git、Issue 或 PR。

OAuth JSON 和刷新令牌都应按敏感凭据处理：不要截图其内容，不要通过聊天或网盘公开分享，也不要放入 Markdown 导入目录。授权页只应使用 `accounts.google.com`，令牌交换只应使用 `oauth2.googleapis.com`；如不再使用，可在 Google 账号的第三方应用访问设置中撤销授权。

授权失败时优先核对三项：Drive API 已在创建该 OAuth 客户端的同一 Cloud 项目中启用；客户端类型确实是“桌面应用”；Chrome 当前选择的账号已列入该项目的测试用户。不要通过关闭安全校验或改用来源不明的 OAuth JSON 绕过错误。

## 转换链路

```text
Markdown + 本地图片 → Pandoc AST → DOCX → Google Drive 转换 → Google Docs
```

- 本地图片会嵌入 DOCX，不需要公网图片地址。
- LaTeX 数学公式由 Pandoc 写成 DOCX OMML，再交给 Google Docs 转换。
- 围栏代码块保留等宽字体和语法高亮；Google Docs 不保证保存原始语言标签。
- 同一源文件的相同转换结果重复执行时，会通过私有 `appProperties` 哈希识别并跳过；不同路径的同内容文件仍会分别创建文档。
- 单篇转换或上传失败会继续处理后续文件，并在任务报告中记录相对路径与错误；图片失败会单独列入资源失败。只有图片失败时，正文仍会创建，但任务结果会明确标为“部分成功”。修复原因后可重新执行整批任务，已成功且内容未变的文档会自动跳过。

## 安全边界

- 只读取所选 Markdown 目录中的 `.md` 和其引用的本地图片。
- 拒绝绝对路径、越界路径、越界符号链接及隐式远程图片下载。
- 原始 HTML 在 Pandoc 解析阶段禁用，避免用 HTML 绕过图片路径检查。
- Pandoc 下载地址和发布包 SHA-256 固定；缓存的可执行文件还会复核安装时记录的摘要和精确版本，校验失败时不会执行。
- 仅调用 Google Drive 官方 API。批量任务会提前刷新访问令牌；HTTP 401 会重新刷新令牌，限流和临时服务错误最多自动重试 3 次。

## 已知限制

- Google Docs 能保留公式和代码的视觉效果，但不是无损往返：再次导出 Markdown 时，公式不保证还原为原始 LaTeX，代码块也可能退化为带样式的普通文本并丢失围栏和语言标签。
- 图片会由 Google Docs 处理并可能重新编码；插件保证可见内容和链接处理，不承诺原图字节或元数据完全不变。
- 远程图片不会由插件代为下载；请先保存到 Markdown 目录内。
- 为避免 SVG 中的脚本或外部引用造成风险，首版不导入 SVG；请先转为 PNG、JPEG、GIF、WebP、BMP 或 TIFF。
- Google 文档转换的 DOCX 上限为 50 MB；超过 5 MB 时使用可续传上传。大批量导入仍可能受 Google 项目配额限制。
- 首版在“我的云端硬盘”根级创建文档，不恢复本地文件夹层级。
- Pandoc 无法解析的非标准 LaTeX 会按普通文本保留，插件不会猜测或改写公式含义。

## 第三方组件

转换引擎来自 [Pandoc 官方项目](https://github.com/jgm/pandoc)，版权归 John MacFarlane 及贡献者所有，许可证为 `GPL-2.0-or-later`。插件下载未经修改的官方 Pandoc 3.10.2 发布包，并在解压前校验仓库中固定的 SHA-256；Pandoc 不会被安装到系统环境或修改用户的 `PATH`。

## 测试结果（2026-08-31）

- Windows x86_64 实测本地 Markdown → Pandoc 3.10.2 → DOCX：本地图片已嵌入，公式生成 OMML，围栏代码生成 `SourceCode` 样式。
- 已使用真实 OAuth + Google Drive API 导入含本地图片、公式、代码和表格的 Markdown；首次创建成功，原样重复执行识别并跳过同一文档。
- 已对 141 篇 Markdown 执行递归扫描、本地 DOCX 校验及真实 Drive 复核：141/141 篇有对应原生 Google 文档，重复执行全部正确跳过，文档级失败为 0。
- 压力测试中 33 篇文档的 145 个远程、绝对或越界图片引用被安全拒绝，并全部按“部分成功”记录相对文档路径、资源地址和原因；正文导入不受影响。
- 自动化测试覆盖 OAuth 桌面客户端、最小 `drive.file` 权限、Chrome 优先、图片路径边界、发布包摘要、DOCX 稳定去重、原生文档转换请求，以及 5–50 MB 可续传上传；该入口仍标记为实验性。
