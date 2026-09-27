"""插件配置（JSON 文件，默认放在项目目录下）。"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_CONFIG_NAME = "config.json"
log = logging.getLogger(__name__)


@dataclass
class Config:
    # 对称方式：auto=自动识别；mirror=只用镜像；rotate=只用旋转；off=关闭对称功能
    symmetry_mode: str = "auto"
    # mirror/rotate 模式下使用的参考轴（x/y/z，穿过零件重心）
    mirror_axis: str = "x"
    rotate_axis: str = "z"
    rotate_angle: float = 180.0
    # 草图元素匹配容差（毫米）
    tolerance: float = 0.01
    # 自动识别时，变换必须能映射多少比例的草图元素才算「这个零件的对称」
    symmetry_min_ratio: float = 0.9
    # 额外识别的对话框标题（默认已内置中/英/法/德/意等）
    extra_dialog_titles: list[str] = field(default_factory=list)
    # 右键补对称边线（倒圆角对话框打开时，右击＝补齐对称边线，并且不弹出 CATIA 右键菜单）
    right_click_pairing: bool = True
    # 右键触发范围：any=只要选中的是边线就接管右键（推荐，不会弹 CATIA 右键菜单）
    #               dialog=只有倒圆角对话框打开时才接管；off=不接管
    rmb_mode: str = "any"
    # 选中面时自动移除误选（默认关：先弹提醒，让你自己决定）
    auto_remove_wrong_pick: bool = False
    # 对称动作：fillet=直接生成对称倒圆角（推荐）| select=只把对称边线加进选择集
    symmetric_action: str = "fillet"
    # 是否允许"按标签推算"直接生成圆角（有些零件编号不规律时会落到别的边，默认关）
    label_autocreate: bool = False
    # 生成倒圆角用的半径（对话框开着时优先用对话框里的半径）
    fillet_radius: float = 5.0
    # 圆角传播方式：0=不传播 1=相切连续
    fillet_propagation: int = 1
    # 生成倒圆角前先关掉正在运行的倒圆角对话框（避免留下未完成的特征）
    close_dialog_before_fillet: bool = True
    # 轮询间隔（秒）
    poll_interval: float = 0.25
    # 提示气泡停留时间（秒），0 表示不自动消失
    toast_seconds: float = 0.0
    # 全局热键
    hotkey_pair: str = "ctrl+alt+r"
    hotkey_remove_wrong: str = "ctrl+alt+x"
    hotkey_start_fillet: str = "ctrl+alt+f"
    hotkey_quit: str = "ctrl+alt+q"
    # 启动时自动启动 CATIA（如果没开）
    start_catia_if_missing: bool = False
    # 手动指定 CATIA 的 win_b64\code\bin 目录（封闭平台/注册表异常时用；留空=自动探测）
    catia_bin: str = ""
    # ---------------------------------------------------------------- R5 形状级对称
    # 形状级对称引擎总开关（关掉就退回 v0.4.8 的草图级行为）
    shape_engine_enabled: bool = True
    # 优先用形状引擎（True）；默认 False＝先试草图引擎，失败再交给形状引擎
    shape_engine_first: bool = False
    # 形状引擎找到唯一高置信候选时，是否直接生成倒圆角（不需要 label_autocreate）
    shape_engine_autocreate: bool = True
    # 位置容差（mm）、长度/半径相对容差
    shape_tolerance: float = 0.05
    shape_length_tol_ratio: float = 0.02
    shape_radius_tol_ratio: float = 0.02
    # 子段重叠下限（圆角削短相邻边时用）
    shape_overlap_min: float = 0.7
    shape_allow_subsegment: bool = True
    # 接受阈值：分数下限 & 与第二名的差距（低于差距就弹选择框，不盲选）
    shape_min_score: float = 0.72
    shape_score_margin: float = 0.06
    # 镜像平面：整体匹配率下限 / 最多保留几个候选面
    shape_plane_min_ratio: float = 0.7
    # 连续多刀后整体对称率会下降：允许"局部解释"的放宽门槛（低于它就一律拒绝）
    shape_plane_min_ratio_local: float = 0.6
    shape_min_local_pairs: int = 3
    shape_max_planes: int = 4
    # 每次动作最多评估几个镜像平面（性能开关：真实零件上平面越多越慢）
    shape_max_planes_try: int = 2
    # 扫描：最多几条边 / 缓存秒数（模型一变就失效）
    shape_max_edges: int = 600
    shape_cache_seconds: float = 6.0
    # 扫完把用户原来的选择集还回去（否则模型里会一直整圈高亮、越用越卡）
    shape_scan_restore_selection: bool = True
    # "手动选中多条边一次做圆角"这条路径最多接受几条（防止误把整圈边当成你的选择）
    shape_manual_fillet_max_edges: int = 4
    # 按类型搜索边线的语法（旧版本 CATIA 若不同，在这里补；留空=用内置默认）
    shape_edge_queries: list[str] = field(default_factory=list)
    # 评分权重（可调；拓扑项在自动化里拿不到相邻面，权重设 0 即忽略）
    shape_weight_position: float = 0.40
    shape_weight_length: float = 0.20
    shape_weight_geometry: float = 0.25
    shape_weight_topology: float = 0.15
    # 到对称中心距离的一致性判据（Prompt §29.2）
    shape_center_rel_tol: float = 0.05
    shape_center_abs_tol: float = 1.0
    log_level: str = "INFO"

    @classmethod
    def load(cls, path: Path, create_if_missing: bool = True) -> "Config":
        if not path.exists():
            cfg = cls()
            if create_if_missing:
                cfg.save(path)
            return cfg
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("配置文件读取失败(%s)，改用默认配置: %s", path, exc)
            return cls()
        known = {f for f in asdict(cls()).keys()}
        clean = {k: v for k, v in data.items() if k in known}
        try:
            return cls(**clean)
        except TypeError as exc:
            log.warning("配置文件字段不合法(%s)，改用默认配置: %s", exc, path)
            return cls()

    def save(self, path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(asdict(self), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        except Exception as exc:
            log.warning("配置写入失败(%s): %s", path, exc)
