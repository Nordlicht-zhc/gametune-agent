# GameTune Agent Desktop UI

GameTune Agent 是一个使用 PySide6 构建的 Windows 桌面界面原型，用于演示游戏性能优化智能体的完整工作流。

当前版本实现任务创建、瓶颈证据展示、诊断实验录入、历史记录和报告界面，并包含可独立测试的确定性核心：CSV 数据导入、逐帧指标计算、证据分析、实验规划、效果评估与 SQLite 持久化。多轮评估会比较当前已接受配置，只有达到重复测试门槛且确认实际执行的结果才能成为最佳已验证配置。模型 API 是可替换的解释层，不影响本地闭环运行，可在“模型设置”页面直接填写兼容接口并选择思考模式。

## 安装与运行

```powershell
python -m pip install -r requirements.txt
python app.py
```

程序会直接打开桌面窗口，不需要浏览器或本地 Web 服务。

运行核心测试：

```powershell
python -m unittest discover -s tests -v
```

## 界面流程

1. 在“新建任务”中填写硬件、性能目标、测试条件和基线数据。
2. 在“概览”中查看瓶颈证据、证据支持程度和下一轮诊断实验。
3. 在“当前实验”中选择 CSV 或手动录入复测结果。
4. 在“历史记录”中比较基线与各轮实验。
5. 达到停止条件后，在“最终报告”中输出最佳已验证配置。
6. 在“模型设置”中应用或测试 API；后续测试结论会由模型总结，并在失败时回退到本地说明。

## 测试数据与验证口径

- 汇总 CSV 使用 `avg_fps,1%_low,gpu_usage` 表头；每行代表一次独立测试，GPU 占用率列可省略。导入保留逐次结果、中位数和极差占中位数的百分比。
- 逐帧 CSV 支持 `frame_time_ms`、`frametime` 或 `MsBetweenPresents` 等毫秒列，至少需要 100 个有效样本。一个采集文件只算一次测试，不把帧数当成重复次数。
- 平均 FPS 为有效帧数除以总帧时间；1% Low 为最慢 1% 帧的平均帧时间换算成 FPS。不同采集工具的同名指标可能使用不同口径，应保持工具与采集模式一致。
- 非数字、非有限数、非正帧时间和缺失必要字段会提示具体 CSV 行号。`gpu_busy` 不被当作百分比占用率导入。
- 基线先完成两次；如果两次结果差异超过 5%，再补第三次。候选设置先做一次筛选，明显退步会立即建议回滚；没有明显下降时继续累计到三次有效运行后才确认收益。有逐次数据时检查波动；手动填写的汇总值和重复次数不包含可验证的波动证据，但连续提交每次测试次数为 1 的结果会按同一配置累计，应在真实采集后复核。

演示和自动化测试使用临时数据库与模拟模型接口，不代表已经完成真实游戏性能提升验证。

分辨率提供常用 16:9、16:10 和 4:3 候选值，也可以直接输入自定义的“宽 × 高”数值。实际可选项取决于显示器、显示模式和显卡缩放设置；分辨率对照实验会优先选择同宽高比的下一档，避免因改变画面比例干扰瓶颈判断。

CS2 任务会按游戏简体中文菜单记录显示模式、游戏最高FPS、V-Sync、全局阴影效果和动态阴影。当前游戏已将全局阴影效果与动态阴影范围拆成两个设置，因此阴影实验必须保持动态阴影不变；竞技任务默认记录“动态阴影：全部”，避免把可见性变化误当作单纯画质收益。游戏最高FPS使用具体数值，`0` 表示不限制。显示文本与当前 CS2 简体中文资源保持一致，内部稳定值保留以兼容已有任务。

高级视频设置还会完整记录当前视频值预设、增强角色对比度、NVIDIA G-Sync、NVIDIA Reflex 低延迟、AMD Anti-Lag 2.0、菜单最高FPS、多重采样抗锯齿、模型/贴图细节、贴图过滤模式、光影细节、粒子细节、环境光遮蔽、高动态范围和 Fidelity FX 超级分辨率。显卡厂商专属选项允许选择“此设备不可用”。

CPU 和 GPU 使用 `knowledge/hardware.json` 中的标准型号目录，并区分桌面与 Laptop 型号。目录不显示或推断固定性能等级；超频、功耗限制、散热、内存和游戏负载都会改变实际表现，因此瓶颈判断只使用当前设备的实测遥测和对照实验。列表末尾可以选择自定义型号。

选择任一 Laptop CPU/GPU 或“自定义 Laptop 型号”后，任务页面会显示笔记本补充信息：持续 GPU 功耗、独显直连/混合输出、供电性能模式和散热模式。规划器会先排除这些平台限制，再进行普通分辨率或画质实验。

## 核心模块

- `models/schemas.py`：任务、基准测试、证据、实验计划和决策结构。
- `tools/import_benchmark.py`：兼容逐帧时间或多轮汇总 CSV。
- `tools/calculate_metrics.py`：平均 FPS、1% Low、中位数、目标完成度和按模式加权的实际变化。
- `tools/analyze_evidence.py`：区分表现症状与可能原因，并把缺失条件计入证据支持程度。
- `agent/planner.py`：根据证据选择下一次单变量实验。
- `agent/evaluator.py`：保留、撤销、复测或结束判断。
- `agent/controller.py`：协调工具并记录完整闭环。
- `agent/state_machine.py`：约束等待输入、分析、等待复测、评估和完成状态转换。
- `agent/validator.py`：在展示建议前校验轮次、必要字段、设置名和允许值。
- `agent/api.py`：模型 Provider 协议、统一请求/响应以及兼容型 HTTP 适配器。
- `agent/prompts.py`：结构化提示词和模型 JSON 输出校验。
- `agent/explainer.py`：模型解释调用与离线回退。
- `storage/repository.py`：SQLite 任务、测试和决策记录。
- 每条测试记录包含配置快照和“已确认执行”标记；SQLite 另外保存当前配置、最佳配置及工作流轮次，控制器可通过 `restore_task(task_id)` 恢复任务上下文。
- `knowledge/cs2.json`：第一版 CS2 可调整项规则。

目标完成度仍使用竞技 35%/65%、平衡 50%/50%、视觉 60%/40% 三组固定偏好，并把超过目标的指标封顶为 100%，只用于显示距离目标还有多远。候选配置是否保留则使用未封顶的实际 FPS 变化：先检查明显退步，再按模式权重合并平均 FPS 与 1% Low 的变化；加权变化太小会要求复测，避免微小分差直接改变配置。视觉模式还会阻止已记录的分辨率或画质档位下降被自动当成优化结果。

## 模型 API 预留接口

核心优化逻辑不依赖模型。模型只负责把已经验证过的结构化决策解释成自然语言；调用失败或输出不合法时，系统自动使用本地说明。

实现其他服务时，只需满足 `ModelProvider`：

```python
class MyProvider:
    name = "my-provider"

    def complete(self, request: ModelRequest) -> ModelResponse:
        # 将 request.messages 转换成目标服务需要的格式
        # 调用服务后统一返回 ModelResponse
        ...
```

控制器注入方式：

```python
controller = GameTuneController(".data/gametune.db", model_provider=MyProvider())
explanation = controller.explain(task, decision, evidence=evidence, plan=plan)
```

项目还提供了不依赖第三方 SDK 的 Chat Completions 兼容适配器。配置使用环境变量，不要把密钥写入源码：

```powershell
$env:GAMETUNE_API_BASE = "https://your-service.example/v1"
$env:GAMETUNE_API_KEY = "your-api-key"
$env:GAMETUNE_MODEL = "your-model-name"
$env:GAMETUNE_API_TIMEOUT = "30"
$env:GAMETUNE_REASONING_EFFORT = "medium"
```

也可以不设置环境变量，直接在桌面端“模型设置”页面填写并测试。填写 API 地址和密钥后可点击“获取模型”，程序会请求兼容接口的 `/models` 并把模型 ID 放入可编辑下拉框。勾选“记住本机 API 配置”后，地址、模型、超时和思考模式保存到用户配置目录，密钥使用当前 Windows 账户的 DPAPI 保护后保存；取消勾选或点击“清除已保存配置”即可移除本机配置。环境变量优先于本机保存配置。思考模式关闭时不发送 `reasoning_effort`；低、中、高分别发送 `low`、`medium`、`high`。未设置环境变量且没有本机配置时，系统保持离线运行；只设置一部分环境变量时会明确报告缺少的配置项。

## 渲染检查

```powershell
python app.py --screenshot .qa/dashboard.png
```
