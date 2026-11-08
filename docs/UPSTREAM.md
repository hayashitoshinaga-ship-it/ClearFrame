# 上游来源与改造边界

## 来源

- 上游仓库：[CXP-2024/gpt-anime-denoise](https://github.com/CXP-2024/gpt-anime-denoise)
- 派生基线提交：[`a5005a82f70d1d7c79145968c7c64906b283eb18`](https://github.com/CXP-2024/gpt-anime-denoise/commit/a5005a82f70d1d7c79145968c7c64906b283eb18)
- 本地分支：`local/clearframe-studio`
- Git 上游 remote 名：`upstream`
- 远端 fork：[hayashitoshinaga-ship-it/ClearFrame](https://github.com/hayashitoshinaga-ship-it/ClearFrame)，本地 remote 名为 `origin`。
- 上游 README 原文：[UPSTREAM_README.md](UPSTREAM_README.md)。这是从基线复制的归档文本，其中的模型名称、效果和性能结论保留原作者表述，并未因此成为 ClearFrame 的独立验证结论。

ClearFrame 保留上游算法核心，新增独立的本地手动工作台：场景预设、保守照片去噪、自定义处理组合、统一强度、任务进度、硬件状态以及原图/结果对比。导入图片和改变参数不再自动开始计算；可选 skill 改为仅显式调用。

上游的 `denoise.py`、`denoise_app.py`、原启动与安装脚本及相关资源保留，以便追溯和兼容。ClearFrame 新入口为 `Start-ClearFrame.cmd` 与 `studio.server`。旧入口可能仍有拖入即处理、参数变化即重算等上游行为，不应与新入口混用。上游文档归档中的相对链接以原仓库根目录为基准，可在上游仓库查看对应资源。

## 致谢

感谢 [CXP-2024](https://github.com/CXP-2024) 与 [hihihihi202](https://space.bilibili.com/3546568969947566) 的原项目，以及上游致谢的贡献者 [AkihiM](https://github.com/AkihiM)、[juanhanyu](https://github.com/juanhanyu)、[a705876883-arch](https://github.com/a705876883-arch)。完整的上游贡献说明保存在 README 归档中。

## 许可边界

代码沿用根目录的 MIT [LICENSE](../LICENSE)，保留原有版权与许可文本。模型权重与示例图片是不同的资产，不因代码采用 MIT 就一并获得相同许可。

- ClearFrame 动漫超分使用 Real-ESRGAN 动漫模型；权重在需要时从上游发布地址下载，不随应用源码重新打包。模型使用请参见 [Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN) 的许可和说明。
- 上游还包含 APISR、Real-CUGAN 等命令行路径，本工作台的自定义阶段没有把它们列为可选模型。对应模型分别参见 [APISR](https://github.com/Kiteretsu77/APISR) 和 [Real-CUGAN](https://github.com/bilibili/ailab) 的许可；保留源码不等于更改这些模型的许可。
- `examples/` 是上游演示资产。上游注明其中包含第三方角色、参考构图以及采用非商业许可的角色设计，并声明示例图不适用代码 MIT、不可商用；细项见 [上游许可说明](UPSTREAM_README.md#许可)。

ClearFrame 不把上游少量案例的统计推广到所有图片，不承诺无损去噪、人物细节完全不变、照片都适合动漫模型，或某个设备上的固定处理时间。用户应在原尺寸下查看具体图像的处理结果。
