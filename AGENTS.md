# GameTune Agent

Python/PySide6 游戏性能优化桌面应用。界面文案使用简体中文，文件使用 UTF-8；沿用现有类型标注、dataclass 和 enum 风格。

## 项目入口

`app.py` 是桌面入口；`agent/` 管理规划、校验、评估和状态机；`models/schemas.py` 定义领域数据；`tools/` 处理导入、指标和证据；`knowledge/` 保存游戏规则；`storage/repository.py` 管理 SQLite。安装、API 配置和实验口径按需查阅 `README.md`，具体行为以当前实现为准。

在项目根目录运行 `python app.py`。测试使用标准库 unittest：`python -m unittest discover -s tests -v`；可按模块运行受影响测试。截图使用 `python app.py --screenshot .qa/dashboard.png --page 0`，页码为 0–5。依赖见 `requirements.txt`。

## 领域约束

- 指标计算、有效性与可比性判断、保留或回滚决策由确定性代码负责。模型可从程序生成并校验的候选中选择计划、解释结果，不得创造设置或改写测量值与决策；保留离线回退。
- 诊断依据实测遥测与受控实验，不能由硬件名称或单独的低 GPU 占用推断瓶颈。计划须符合游戏规则，工作流转换通过状态机完成。
- 区分建议、已确认执行和已验证收益；保留配置来源、快照与测试条件，仅使用有效且可比的记录评估。UI 与报告绑定实际领域记录，不使用演示常量代替结果。

## 工作与验证

在请求范围内自主完成实现、必要验证和相关修复，保留无关的现有修改。按改动风险选择验证：文档或低影响文案检查差异即可；行为改动覆盖受影响的回归场景；跨模块核心流程、指标口径或持久化兼容性变更运行全套测试。通过后仅因新修改、失败或未解决风险扩大或重复检查。

测试使用临时数据库和模拟模型接口。视觉或布局变化检查受影响页面；交互变化检查相关操作，截图不能代替交互证据。交付时简述结果、实际验证和未验证之处。

## 数据与配置

API 配置支持 `GAMETUNE_API_BASE`、`GAMETUNE_API_KEY`、`GAMETUNE_MODEL`，以及可选的 `GAMETUNE_API_TIMEOUT`、`GAMETUNE_REASONING_EFFORT`；也可使用桌面模型设置，保存密钥时沿用 Windows DPAPI 保护。不得提交或输出凭据、授权头、本地数据库与采集的用户数据；本地数据和截图分别放在已忽略的 `.data/`、`.qa/`。
