---
name: clearframe-manual
description: Help the user explicitly launch or operate the local ClearFrame image cleanup workbench when they name ClearFrame or invoke $clearframe-manual. Manual invocation only; do not activate after image generation, image receipt, or a generic mention of noise or artifacts.
---

# ClearFrame 手动图像处理

这是独立本地工作台的可选操作说明。程序不依赖 skill 安装，不调用 OpenAI API，也不需要读取 API Key。

## 调用边界

- 仅在用户明确请求使用 ClearFrame 或显式调用本 skill 时应用。
- 不在生成、收到或交付图片之后自动执行，不把图片导入视为处理授权。
- 不因为图像来自某个生成模型，就断言其有噪点或必须处理。
- 不把动漫超分模型作为所有照片的必经步骤。照片预设采用保守的 CPU 去噪；动漫模型或 L0 由用户在自定义组合中主动选择。

## 操作

1. 在项目目录检查本地环境是否已经准备好；首次安装使用 `Setup-ClearFrame.cmd`，平时使用 `Start-ClearFrame.cmd`。不要自动安装到 Codex/Claude 的 skills 目录。
2. 启动本地浏览器工作台，帮助用户导入指定图片。导入与改设置不会开始处理。
3. 根据用户选定的场景、方法与强度准备操作。说明预设只决定参数，不会自动分割人物或背景；去噪可能损失细节，动漫模型可能改变微小结构。
4. 等用户点击开始；若用户已经明确委托 agent 处理指定图片并给出可用设置，可以据此启动对应任务。不得延伸到其他图片或自动监控目录。
5. 查看处理状态与实际设备，完成后提供结果和原图对比。原文件不覆盖，不把进度百分比或强度称为画质提升。

具体界面、方法和本地目录说明见 [docs/USER_GUIDE.md](docs/USER_GUIDE.md)。默认使用 ClearFrame 新入口，不启动上游会自动处理导入图片的 `denoise_app.py`。
