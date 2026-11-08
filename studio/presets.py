"""Scenario presets for a manual, local image-processing workflow."""
from copy import deepcopy
import math


MODES = [
    {"id": "anime_character", "label": "二次元人物", "description": "角色立绘、同人插画与动画色块。", "default_method": "default", "default_strength": 40,
     "recommendation": "动漫重建配合轻度平滑，优先清理头发和衣服里的细碎纹理。", "risks": "细发丝、腮红、眼部高光与淡阴影可能被简化；建议与原图比较。"},
    {"id": "anime_landscape", "label": "二次元风景", "description": "动画背景、建筑与绘制的自然景观。", "default_method": "default", "default_strength": 30,
     "recommendation": "使用较克制的平滑，保留建筑边缘与环境层次。", "risks": "树叶、远景窗格、小字和云层细节可能被当作噪纹。"},
    {"id": "photo_portrait", "label": "实拍人像", "description": "真实照片中的人物与皮肤质感。", "default_method": "default", "default_strength": 30,
     "recommendation": "默认仅做保守的非局部均值降噪，保留自然皮肤质感。", "risks": "高强度会损失毛孔、睫毛与细发丝；不会修正五官或自动美颜。"},
    {"id": "photo_landscape", "label": "实拍风景", "description": "真实建筑、自然风景与夜景照片。", "default_method": "default", "default_strength": 35,
     "recommendation": "默认先降低亮度与色彩噪声，需要时再轻度锐化。", "risks": "高强度可能损失草木与远处纹理；锐化也可能强调残留噪声。"},
]
METHODS = [
    {"id": "default", "label": "场景默认", "principle": "按所选场景使用不同组合：二次元采用动漫超分重建与 L0；实拍采用非局部均值降噪。", "effect": "在清理纹理与保留细节之间取较克制的起点。", "caution": "预设不代表每张图都适用，请保留原图作判断。"},
    {"id": "gentle", "label": "轻柔保真", "principle": "沿用场景默认流程，同时降低内部处理参数与结果混合量。", "effect": "尽量保留淡阴影、细线与自然质感。", "caution": "明显噪纹可能仍有残留；保真指较少改动，并非无损。"},
    {"id": "texture_clean", "label": "强化清理", "principle": "提高场景对应降噪强度；实拍在降噪后添加克制的边缘锐化。", "effect": "得到更平整、干净的色块或降低较明显颗粒。", "caution": "可能抹去有用纹理、产生塑料感或轻微锐化光晕。"},
    {"id": "custom", "label": "自定义组合", "principle": "自行选择处理阶段，按降噪 → 动漫重建 → L0 平滑 → 锐化的固定顺序执行。", "effect": "有针对性地选择需要的步骤；强度同时调节参数与原图混合比例。", "caution": "动漫重建和 L0 用于实拍时可能使照片插画化，尤其影响皮肤和细纹。"},
]
STAGES = [
    {"id": "photo_denoise", "label": "照片降噪", "principle": "OpenCV 非局部均值（NLM）比较邻近的相似小块，降低随机亮度和色彩噪声。", "effect": "减少颗粒与彩色噪点，适合作为实拍照片的保守起点。", "caution": "CPU 运算；大图可能较慢，强度过高会损失真实纹理。"},
    {"id": "anime_sr", "label": "动漫重建", "principle": "先缩小以平均细密纹样，再用 Real-ESRGAN anime 6B 重建并恢复原尺寸。", "effect": "清理鱼鳞状纹理，形成较干净的动漫线条和色块。", "caution": "会学习性重建细节，不保证像素或笔触不变；首次使用需下载约 18 MB 模型。"},
    {"id": "l0", "label": "L0 色块平滑", "principle": "通过梯度稀疏优化压低细小色差，尽量保留较强的轮廓。", "effect": "让平涂区域更平整，减少细碎起伏。", "caution": "低对比细线、腮红和渐变也可能被抹掉，不能自动辨认哪些纹理有用。"},
    {"id": "sharpen", "label": "轻度锐化", "principle": "将原像素与轻度模糊后的差值按受限幅度加回，突出局部边缘。", "effect": "适度增强轮廓的清晰感。", "caution": "不会恢复已经丢失的真实细节，也可能放大噪声和边缘光晕。"},
]
STAGE_ORDER = tuple(s["id"] for s in STAGES)
DEFAULTS = {"mode": "anime_character", "method": "default", "strength": 40, "device": "auto", "stages": ["anime_sr", "l0"]}


def get_config():
    return deepcopy({"modes": MODES, "methods": METHODS, "stages": STAGES, "defaults": DEFAULTS})


def validate_settings(settings):
    if not isinstance(settings, dict):
        raise ValueError("处理设置必须是一个对象。")
    mode = settings.get("mode", DEFAULTS["mode"])
    if not isinstance(mode, str) or mode not in {m["id"] for m in MODES}:
        raise ValueError("未知的场景分类。")
    method = settings.get("method", "default")
    if not isinstance(method, str) or method not in {m["id"] for m in METHODS}:
        raise ValueError("未知的处理方式。")
    strength = settings.get("strength", next(m["default_strength"] for m in MODES if m["id"] == mode))
    if isinstance(strength, bool) or not isinstance(strength, (int, float)) or not 0 <= strength <= 100 or not math.isfinite(strength):
        raise ValueError("处理强度必须是 0 到 100 之间的有限数值。")
    device = settings.get("device", "auto")
    if device not in ("auto", "cpu", "cuda"):
        raise ValueError("计算设备只能选择自动、CPU 或 GPU。")
    stages = settings.get("stages", [])
    if method == "custom":
        if not isinstance(stages, list) or any(not isinstance(s, str) or s not in STAGE_ORDER for s in stages):
            raise ValueError("自定义组合包含未知处理阶段。")
        if len(set(stages)) != len(stages):
            raise ValueError("自定义阶段不能重复。")
        if not stages:
            raise ValueError("请至少选择一个自定义处理阶段。")
        stages = [s for s in STAGE_ORDER if s in stages]
    else:
        stages = ["anime_sr", "l0"] if mode.startswith("anime_") else ["photo_denoise"]
        if method == "texture_clean" and mode.startswith("photo_"):
            stages.append("sharpen")
    return {"mode": mode, "method": method, "strength": float(strength), "device": device, "stages": stages}


def resolve_recipe(settings):
    result = validate_settings(settings)
    amount = result["strength"] / 100
    factor = {"default": 1.0, "gentle": 0.55, "texture_clean": 1.3, "custom": 1.0}[result["method"]]
    power = min(1.0, amount * factor)
    # The output blend keeps low slider values continuous with the exact zero-strength identity.
    mix = power ** 0.65
    landscape = result["mode"].endswith("landscape")
    result["parameters"] = {
        "downscale": round(0.9 - 0.45 * power, 4),
        "l0_lambda": (0.00005 + 0.0017 * power ** 1.5) * (0.6 if landscape else 1.0),
        "nlm_h": (1.0 + 9.0 * power) * (0.85 if result["mode"] == "photo_portrait" else 1.0),
        "nlm_color_h": 1.0 + 7.0 * power,
        "sharpen_percent": round(15 + 55 * power),
        "result_mix": mix,
    }
    return result
