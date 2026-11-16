"""导出角色 / 敌人 SD 小人：``bundle_unit_<5位编号>_bc`` → 逐动作的动画 WebP。

SD 小人是什么
-------------
战斗里的 Q 版小人不是图片序列，而是 **SpriteStudio6（SS6）** 的结构化动画：
``da_mot_<编号>_BC`` 里 ``TableParts`` 是部件树（84 个：骨骼、头、手臂、武器…），
``TableAnimation`` 每段是一套动作（``mot_stand`` 待機 / ``mot_move`` 出撃 /
``mot_attack_01`` 攻撃1 / ``mot_attack_02`` 攻撃2 / ``mot_victory`` 勝利 /
``mot_damage`` 被弾 …），每个部件又是逐帧的 position / scaling / opacity 曲线。
所有部件共用包里那一张 1024² 图集（``dc_<编号>_BC`` 的 ``TableCellMap`` 给出每格的
矩形与 pivot）。

这里做的是"按部件树合成每一帧、再编码成一串动画帧" —— 每个动作一个循环 WebP，
画面顺序/时长取官方 ``FramePerSecond``，所以页面上会像游戏里一样动。

坐标约定（踩过的坑）
--------------------
* SS6 画布 **y 轴向上**，Pillow 向下：所以纵向要取反（``W/2 - y``）；
* pivot 的 y 量的是**格子底边到支点的距离**，所以换算成 Pillow 的左上角要写
  ``(ch - py)``；漏了它整张图会上下颠倒；
* 图集里的格子**不需要翻转**：翻了头发会朝上（四种组合逐一试过，只有
  "y 取反 + 不翻图"是对的）；
* 父子变换是**标准仿射连乘**（``parent · local``）：子部件的 Position 在父级
  坐标系里，父级一旋转它就得跟着转。SV4 曾经把位置改成"不吃父旋转"，
  结果攻击/受击时手臂武器整体脱离身体（详见 ``SV`` 的说明）。

用法（cwd = userdata/，与其它解包脚本一致）
------------------------------------------
    python export_sd.py --chars 1,2,3          # 指定角色
    python export_sd.py --units 10001,10002    # 指定敌人/NPC（5 位编号）
    python export_sd.py --npc                  # 全部 NPC / 敌人
    python export_sd.py --all --force          # 全量重做（改了口径就靠它）
    python export_sd.py --chars 1 --force      # 重做已存在的

产物
----
    web/media/img/sd/<编号><动作>.webp            透明底，动画 WebP
        动作 = stand 待机 / move 出击 / attack1 攻击1 /
               attack2 攻击2 / victory 胜利 / damage 受击
    web/data/sdmap.js                          window.__SDMAP = {v, units:[...]}
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import re
import time

import UnityPy
import numpy as np
from PIL import Image

WEB = os.path.join("..", "web")
OUT_IMG = os.path.join(WEB, "media", "img", "sd")
OUT_JS = os.path.join(WEB, "data", "sdmap.js")
SRC = "char_story/files"

def finding_pose(tree: dict, word):
    """在树里找**名字最贴近某个动作关键词**的那段动画。

    ``word`` 可以是多个候选词（元组 / 列表）：官方四套命名混用，
    攻击 1 就见过 ``attack_01`` / ``mot_attack_01`` / ``attack_1`` 三种写法，
    只按一个词匹配会整段漏掉（2026-11-15 全量重导时在 00100 上撞到）。

    排序规则「越像精确名越优先」：
      0 = 名字就是候选词（``stand`` / ``attack_1``）
      1 = 以候选词结尾（``mot_stand`` / ``attack_01``）
      2 = 只是包含（``attack_01_sp`` 这类变体）
    以前只按"名字最短"排，``attack_01`` 与 ``attack_02`` 同时命中 ``attack``
    时会随机吃到其中一个，攻击 2 永远导不出来（2026-11-15 反馈）。
    """
    wants = [str(w).lower() for w in
             (word if isinstance(word, (tuple, list)) else (word,))]
    hits = []
    for a in tree.get("TableAnimation") or []:
        n = str(a.get("Name") or "").lower()
        for wi, w in enumerate(wants):
            if w in n:
                hits.append((a, wi, rank_named(n, w, wi)))
                break
    if not hits:
        return None
    hits.sort(key=lambda t: (t[2], len(str(t[0].get("Name") or ""))))
    return hits[0][0]


def rank_named(name: str, want: str, weight: int = 0):
    """名字与关键词的贴近程度（见 ``finding_pose`` 的 0/1/2 说明）。"""
    if name == want:
        return (weight, 0)
    if name.endswith(want) or name.endswith("_" + want):
        return (weight, 1)
    return (weight, 2)


#: 页面卡片里展示的动作 —— **跟游戏内一致，6 种**（2026-11-15 用户反馈）：
#:   待机 stand / 出击 move / 攻击1 attack1 / 攻击2 attack2 /
#:   胜利 victory / 受击 damage
#: 以前只有 4 个（stand/ready/attack/victory）：漏了攻击 2 与受击，
#: 还把「ready（構え）」当成出击摆出来 —— 数量对不上、动作也是错的。
#: 每项 = (产物后缀, 动作名关键词（可多个）, 抽帧步长)。
ANIM_POSES = (
    ("stand",   ("stand",),                                    2),
    ("move",    ("move",),                                     1),
    # 攻击的动作名有四代写法混着：attack_01 / attack_1 / attack01 / attack1，
    # 还有 attack1_1、attack1-1 这种带后缀的；个别单位直接叫 attack_03/04
    # （等于官方的"第 1、2 个攻击"）。候选词按优先级排列，命中哪个都算。
    # 2026-11-16：用户用逐动作实拍核对，**数据里的攻击编号与游戏相反**。
    # 408 的 attack_2_1 才是游戏里的「攻击1」（起手大弧抡鞭），attack_1_1 是「攻击2」。
    ("attack1", ("attack_02", "attack_2", "attack02", "attack2", "attack-2",
                 "attack1-2", "attack_04"),                    1),
    ("attack2", ("attack_01", "attack_1", "attack01", "attack1", "attack-1",
                 "attack_03", "attack"),                        1),
    # victory 见过一堆官方 typo：victoy / vicotry / victoey / victry / vicory / vic；
    # damage 见过 damege / damega。不是我们写错，是包内原始拼写就少字母。
    ("victory", ("victory", "victoy", "vicotry", "victoey", "victry",
                 "vicory", "vic", "win"),                     1),
    ("damage",  ("damage", "damege", "damega"),              1),
)

#: **只导这些动作**（2026-11-16 用户决定）。
#: 复原 SD 小人的其它动作（move / attack_1_1 / attack_2_1 / damage /
#: victory_1 / special_1 / special_2 / special_4 / action）口径还没完全对上，
#: 站上先只放**待机**：卡片墙预览、灯箱都只有这一张，页面 --bar 里也写明了
#: 原因。等口径稳定后把名字加回这个元组即可（其余代码不用动）。
EXPORT_CLIP_NAMES = ("stand",)


def all_clips(tree):
    """要导出的动画条名字（当前只有 ``stand``），保持 ``EXPORT_CLIP_NAMES`` 的次序。

    2026-11-16 用户要求：不再只导 6 个固定动作，把包里所有动作都复原到页面，
    且**页面用包内原名**（stand / move / ready / attack_1_1 / attack_2_1 /
    damage / victory_1 / special_1 / special_2 / special_4 / action …），
    不再翻译成"待机/出击/攻击1"这种中文键。各角色段数不同，所以按实际枚举。
    当天晚些时候又改为**先只上线待机**（见 ``EXPORT_CLIP_NAMES``）。
    """
    have = {}
    for a in (tree.get("TableAnimation") or []):
        n = (a.get("Name") or "").strip()
        if n and n not in have:
            have[n] = True
    out = [n for n in EXPORT_CLIP_NAMES if n in have]
    if out:
        return out
    # 兜底：包内名字不是 stand 的（个别单位叫 mot_stand / stand_1）
    return [n for n in have if "stand" in n.lower()][:1]


def clip_step(name):
    """抽样步长。stand 是慢速微动，按 2 帧取 1（已验证 1.33s 与游戏一致）；
    其余一律逐帧（快速挥击少采样会变成"突跳/转圈"）。"""
    return 2 if name.lower() == "stand" else 1


#: 旧版本产物，重导后不再被引用（留着会让页面/磁盘出现两套同名动作）
LEGACY_SUFFIX = ("ready", "attack")

#: 导出倍率。图集格子就是 1:1 像素（整只小人约 179×299）—— **保持原尺寸**，
#: 页面上也不再按卡片大小缩放（用户 2026-11-15：小人是像素画风，放大会糊成一团）。
SCALE = 1.0

#: 抽帧步长：游戏里 30fps × 60 帧 = 2 秒，但待机是很慢的微动，
#: 每 3 帧取 1（20 帧 / 100ms 一步）肉眼看不出差别，**体积降到 1/3**
#: （60 帧 654 KB → 20 帧 192 KB，一个角色 0.2 MB 才有资格默认开）。
ANIM_STEP = 3
#: 动画 WebP 的编码质量。72 是实测拐点：再高只涨体积、像素风看不出差别。
ANIM_Q = 72

#: WebP 编码的 effort（0 最快 … 6 最慢）。**这里绝对不能用 6**：
#: 2026-11-15 实测同一个角色的 14 帧动画，method=6 要 12.34 秒、
#: method=4 只要 0.18 秒（68 倍），文件只从 97 KB 变 104 KB；
#: 静态图是 0.84s → 0.01s（84 倍）。一个角色原本 24.5 秒里有 22.8 秒
#: 花在 `Image.save()` 上 —— 用户装 366 部剧情跑了两小时还没完，就是这个。
WEBP_METHOD = 3

#: 导出格式版本。写进 sdmap 每个条目的 ``sv``；同一条目版本更旧就**强制重做**。
#:   1 = 静态 4 张（旧）
#:   2 = 待机动画 + 图集按 TableCellMap 名字选（修掉"拿错图集只剩碎片"）
#:   3 = 四动作全动画 + 多图集分组 + 逐帧 Cell + 精确仿射
#:   4 = 父子合成改回 SS6 语义（位置不受父旋转）+ 隐藏位不沿父链传染
#:   5 = 【改正】父级旋转**必须**带动子部件位置（SV4 那版把子坐标系摆错了：
#:       手臂/武器/头发在攻击、受击这类大角度动作里整体错位，2026-11-15 全量重导）
#:       + 动作补齐到游戏内的 6 种（stand / move / attack1 / attack2 / victory / damage）
#:   6 = 【根因修复】19 个"网格部件"（身体/胸/头/头发/胯/裙/三条花环鞭）改成
#:       **三角网格 + 骨骼蒙皮 + 逐顶点 Deform**渲染（此前一律当矩形贴图，所以
#:       "部位形状与绑定全错"）；旋转符号改回 +θ（此前是镜像，四肢朝反方向折）；
#:       补上 PartsColor 顶点色与混合模式（ADD/SUB/MUL/SCR）。
#:   7 = 【绘制序逐帧化】Priority（绘制序号）是**逐帧轨道**，此前只取
#:       TableValue[0] 当静态值。408 的「胜利」在第 22/29 帧把前臂
#:       F_arm_FU00/FD00/hand_F00a 的绘制序从 87/84/81 抬到 187/184/181
#:       （画到头发/头前面），后臂压到 33/30/24（藏到背后）；「攻击2」
#:       第 6 帧把后手 F_hand_B00a 从 224 压到 24。只读第 0 帧 → 举起的手臂
#:       永远被头发盖住，看起来就是"手臂没抬/绑定错"。
#:   8 = 【武器关节旋转不参与继承】花环/鞭子那三份网格各挂一条 9 节骨骼链，
#:       链顶端是关节表里的 j_weapon / j_weapon_1 / j_weapon_2
#:       （CatalogParts.TableIDPartsJoint）。408 的 j_weapon_1 在待机里带
#:       +58.01° 的自身旋转， filhos 全继承 → 整条花环被甩成横贯身体的长蛇、
#:       末端拖到膝下（游戏里是卷在肩/胸前的环）。把武器关节的自身旋转归零后
#:       待机/出击/攻击2 的花环立刻回到"卷在身上、贴着前手"的形态，胜利里
#:       花环被丢开（j_weapon_1 的位置设到 -123.5,27）也与游戏一致。
#:       只对名字以 j_weapon 开头的关节生效，不影响其它骨骼/关节。
#:   9~12 = 【已作废并删除】曾加入'武器关节自旋归零''鞭骨链挂到鞭根'
#:       '沿圆弧卷曲''花鞭换贴图份+固定角度缩放'等修补。它们都建立在错误
#:       测量上：纯数学复算原始解包值，9 节鞭骨链本身就是一条长 152、跨度
#:       70x127 的弯弧（与游戏一致），并不需要任何修正。这些臆造修补反而把
#:       花环压成一团/拉成长蛇，已于本版全部移除。
#:  13 = 回到纯数据渲染：_frame_state 不再对武器做任何特殊处理。
#:  20 = 【轨道解码根因修复】TableCode 是 ``(indexValue << 15) | frame``：
#:       高 13 位是"这一帧用哪个 TableValue"，多个帧可以共享同一个值。
#:       以前按 ``vals[i]`` 取 → 只要某条轨道用到了共享值（damage 的 N_hip_all
#:       25 帧只有 19 个值、move / attack 同理），后面的关键帧就全部错位，
#:       表现是"待机正常、受击/移动/攻击完全不对"。同时：
#:       · VertexCorrection 四角按 KindVertex 原序（LU,RU,RD,LD），
#:         以前 (0,1,3,2) 把 RD/LD 对调，脸部表情歪掉
#:       · Deform / VertexCorrection 的关键帧位置改读 TableCode 帧号
#:         （以前按"值个数/总帧数"均分）
#:       · 处理 FLIP_X/FLIP_Y（绕 pivot 翻格）与 FLIP_TEXTURE_X/_Y（只翻 UV）
#:       · 处理 StatusParts 的 HIDE_FULL（隐藏整棵子树）
#:       · 删除"j_weapon 位置 >100 就隐藏其下蒙皮件"的臆造补丁（数据已有 HIDE）
#:  21 = 按 408 实拍反馈再修三处：
#:       · 武器绘制序号兜底：attack_2_1 的 weapon_1 在"翻面"段会掉到 25
#:         （比裙子 31 / 身体 57 还小），照画就穿进身体 —— 低于 200 时按 699 画
#:       · 同一轴正负混合的 Scaling 轨道整条按 1（导出工具的 2D "翻面"过渡，
#:         战斗里 SD 小人始终正面；照算会宽度 1→0→1 收缩，看着像原地转一圈，
#:         且第 6 帧起整体镜像 → 右手攻击变左手）
#:       · Deform（TypePack=3）的 TableCode 高位带插值标志（0x38000000 类），
#:         值索引要掩 11 位，否则整条顶点变形时间轴错位
#:  22 = 恢复负缩放（2D 翻转）并区分"跳变"与"插值"：
#:       · _mat_local 不再 abs()。负缩放是成对设计 —— attack_2_1 的
#:         N_chara_all=-1 配 F_head_00=-1（身翻脸不翻）；damage/victory 的
#:         F_head_00=-1 让头部整体镜像（游戏受击段头饰确实翻到另一侧）、
#:         F_leg_BD00=-1 决定小腿朝向。abs 掉就是"左右手反、闭眼反、小腿歪"。
#:       · _scl_track 改为"幅度线性插值 + 符号按关键帧跳变"：翻面在第 6 帧
#:         瞬间完成，不会出现宽度 1→0→1 的"原地转一圈"。
#:  23 = 缩放符号的最终口径（按 408 录屏逐帧定）：
#:       · **符号会变**的轨道（attack_2_1 的 N_chara_all/F_head_00）整条统一
#:         到末段符号 → 全程同一朝向，鞭子始终在右手边（此前"整条按 1"会让
#:         鞭子停在左侧 = 用户说的"还是左手拿武器"）
#:       · **常数 −1** 的轨道（damage 的 F_head_00、damage/victory 的
#:         F_leg_BD00、move 的 F_arm_FD00）取绝对值 → 不再把头发/脸整块镜像
#:  24 = 站点先只上线**待机**（``EXPORT_CLIP_NAMES``）：其它动作口径未定，
#:       卡片墙/灯箱每个角色只剩一张 stand。同时：
#:       · ``_fill_tri`` 只栅格化三角形内部像素（此前连包围盒外圈一起采样
#:         混合，网格三角形多为细长条，七成算力白费）→ 单帧 135ms → 78ms
#:       · 导出改成多进程（``--jobs``，默认按核数）→ 全量 821 从 ~25 分钟降到 3~4 分钟
#:       · 清理 poses 里已经没有的旧动作 webp（只动已升级到本 SV 的条目）
#:  25 = 【回归修复】删掉 SV20 加的"StatusParts 的 0x10000000 = 隐藏整棵子树"。
#:       那个位与可见性无关，它普遍出现在完全正常的部件上（00136 的
#:       joint_10..13、00338 的 weapon_1、00022 的 joint_1..2 …），照它隐藏
#:       子树会让整批角色"没头发 / 没头 / 没衣服 / 没武器 / 部位错乱"。
#:       可见性只看 Status.Flags 的 HIDE(0x20000000)。
#:  26 = 按官方 IL2CPP 还原出的参数表补齐三处（`_tools/il2cpp/out/ss6_schema.md`）：
#:       · 补 `ScalingLocal`（与 Scaling 相乘，全库只有 5 个部件用）
#:       · 补 `OffsetPivot`（改支点，3 个部件用）
#:       · `PartsColor` 抽样 113 帧全是 `Bound=OVERALL` → 取 VertexColor[0]
#:         当整体 tint 是对的；混合以 `MUL` 为主（102/113），MIX/ADD 少量
#:       · 确认 `IDPartsNextDraw`（27% 部件有值）**不参与层级**：官方排序键
#:         只有 `(priority << shift) | idParts`，它只是图集批处理链
#:  27 = **Scaling 口径定案：普通线性插值、保留符号**（依据官方
#:       `Control/AnimationParts.cs` 806-807 `transform.localScale = value`，
#:       没有 Mathf.Abs）。此前所有"取绝对值 / 正负混合整条按 1 / 统一到末段
#:       符号"的补丁全部删除 —— 那三种都会把 `N_chara_all=-1`（整身翻转）或
#:       `F_head_00=-1`（脸翻回正面）二选一地丢掉，正是"左右手反 / 闭眼反 /
#:       小腿歪"的同一个病根。
#:       同时 `ScalingLocal` 不再乘进 `Scaling`：官方它是独立轨道（只标
#:       UPDATE_SCALELOCAL，绘制阶段用，不传给子级）。
SV = 27

#: 2026-11-16 实拍核对：这几个编号的待机在游戏里是**整体左右镜像**呈现的
#: （0012 武器在画面左、0013 身体朝向相反），而资源包动画数据里没有对应的
#: 镜像位（无负缩放、无 FLIP、无 Instance 变换）。游戏应在运行时按角色/站位
#: 做了水平翻转。这里在导出最后一步对成图做镜像；不改任何动画参数口径。
FLIP_CHARS = {12, 13}

#: 注意：2026-11-16 曾按"轮廓 IoU 匹配"给 0017 / 0019 裁帧区间，
#: 但那个判据被上半身主导，裁出来的区间里腿依旧并拢、球依旧甩到头上。
#: 已撤销 —— 它们是**姿态/绑定错**，不是"播了游戏不播的帧"。



# ---------------------------------------------------------------- 轨道解码
#: ``TableCode`` 的位布局（**这是整套动画对不上的根因**）
#: ---------------------------------------------------------------
#: Unity 版 SpriteStudio6 把每条轨道压成 ``TableCodeValue[].TableCode`` +
#: ``TableValue[]``。官方 ``KindTypePack.STANDARD_CPE``（数据里 TypePack=1）
#: 的编码是::
#:
#:     code = (indexValue << 15) | frame
#:
#: 低 15 位是帧号，高 17 位是**该帧用哪一个 TableValue**。
#: 多个帧可以共享同一个值（"变化点提取"压缩），所以 **codes 的个数常常
#: 多于 values 的个数**（408 的 damage：25 帧只有 19 个值）。
#:
#: 以前这里按 ``vals[i]`` 一一对应取（`i` = code 序号）：只要某个动画里
#: 出现共享值，后面所有关键帧就全部错位 —— 帧号还在、值却错位。表现就是
#: 「待机这种每帧都有值的动作正常，damage/move 这种压缩过的动作完全不对」，
#: 也正是用户反馈的「动作跟游戏完全不一样、各个部位没绑定」。
#:
#: 帧号掩码取 0x7FFF（官方 ``bitMaskFrameKey``），**不能**用 0x1FFF：
#: 0x1FFF 会把高位的值索引切进帧号里。
MASK_FRAME_KEY = 0x7FFF
SHIFT_INDEX_VALUE = 15


def _cpe_pairs(box, total):
    """``(TableCode, TableValue)`` → ``[(frame, value)...]``，按帧号升序。

    共享值轨道（``indexValue != i``）按索引取；索引越界时退回按位置取，
    保证老数据/异常数据不会整条轨道变空。
    """
    vals = box.get("TableValue") or []
    if not vals:
        return []
    codes = []
    for cv in box.get("TableCodeValue") or []:
        codes.extend(cv.get("TableCode") or [])
    if not codes:
        return [(i, vals[i]) for i in range(min(total, len(vals)))]
    out = []
    for i, c in enumerate(codes):
        c = int(c)
        frame = c & MASK_FRAME_KEY
        # 只取 11 位：Deform（TypePack=3，CPE_INTERPOLATE）会在 bit27..29
        # 额外置插值标志（0x38000000 / 0x38010017 ...）。不掩掉就会读成
        # "值索引 0x7000" 而越界；掩 11 位后 0x7002 → 2、0x7004 → 4，
        # 正好是它要引用的 TableValue 下标。
        idx = (c >> SHIFT_INDEX_VALUE) & 0x7FF
        if idx >= len(vals):
            continue
        out.append((min(frame, total - 1), vals[idx]))
    out.sort(key=lambda p: p[0])
    return out


def _lerp(a, b, t):
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a + (b - a) * t
    if isinstance(a, dict) and isinstance(b, dict):
        return {k: _lerp(a.get(k, 0) or 0, b.get(k, 0) or 0, t) for k in set(a) | set(b)}
    return a if t < 0.5 else b


def _track(ap, key, total):
    """位置 / 旋转 / 缩放 / 不透明度：逐帧展开，关键帧之间线性插值。"""
    box = ap.get(key) or {}
    vals = box.get("TableValue") or []
    if not vals:
        return None
    if len(vals) == 1:
        return [vals[0]] * total
    pts = _cpe_pairs(box, total)
    if not pts:
        return [vals[0]] * total
    out, j = [], 0
    for f in range(total):
        while j + 1 < len(pts) and pts[j + 1][0] <= f:
            j += 1
        f0, v0 = pts[j]
        if j + 1 < len(pts):
            f1, v1 = pts[j + 1]
            t = 0.0 if f1 == f0 else (f - f0) / (f1 - f0)
            out.append(_lerp(v0, v1, max(0.0, min(1.0, t))))
        else:
            out.append(v0)
    return out


def _scl_track(ap, total):
    """Scaling 轨道 = **普通线性插值、保留符号**（官方实现，不做任何改写）。

    依据 ``Library/Control/AnimationParts.cs`` 806-807 / 843-845：

        valueTRSPrimary.z = 1.0f;
        transform.localScale = valueTRSPrimary;   // 直接赋值，没有 Mathf.Abs

    游戏把 ``Scaling`` 原样当 ``localScale`` 用：**负值就是负值**，会连同
    子部件一起水平镜像；同一轴从 +1 变到 -1 时也是普通插值（中间经过 0）。
    这里不再做 abs、也不再把"正负混合"整条压成 1 —— 那些都是我此前凭观感
    加的补丁，正好是"左右手反 / 闭眼反 / 小腿歪"的来源。

    数据里确实带负值的轨道（408）：
        attack_2_1  N_chara_all   +1 → -1   整身翻转
        attack_2_1  F_head_00     +1 → -1   把脸镜像回正面（与上一条成对）
        damage      F_head_00     -1        头部整块镜像
        damage/victory_1  F_leg_BD00  -1    右小腿朝向
        move        F_arm_FD00    -1        前臂朝向
    """
    return _track(ap, "Scaling", total)


def _cell_track(ap, total):
    """Cell 轨道 → 每帧 ``[IndexCell, IndexCellMap]``，逐帧跳变（不插值）。"""
    box = ap.get("Cell") or {}
    vals = box.get("TableValue") or []
    if not vals:
        return None

    def pair(v):
        if not isinstance(v, dict):
            return None
        ci = v.get("IndexCell")
        mi = v.get("IndexCellMap")
        return [-1 if ci is None else int(ci), -1 if mi is None else int(mi)]

    if len(vals) == 1:
        return [pair(vals[0])] * total
    pts = _cpe_pairs(box, total)
    if not pts:
        return [pair(vals[0])] * total
    out, j = [], 0
    for f in range(total):
        while j + 1 < len(pts) and pts[j + 1][0] <= f:
            j += 1
        out.append(pair(pts[j][1]))
    return out


def _priority_track(ap, total):
    """Priority（绘制序号）→ 每帧一个整数，逐帧跳变。"""
    box = ap.get("Priority") or {}
    vals = box.get("TableValue") or []
    if not vals:
        return None

    def num(v):
        if isinstance(v, dict):
            v = v.get("Priority")
        return int(v or 0)

    if len(vals) == 1:
        return [num(vals[0])] * total
    pts = _cpe_pairs(box, total)
    if not pts:
        return [num(vals[0])] * total
    out, j = [], 0
    for f in range(total):
        while j + 1 < len(pts) and pts[j + 1][0] <= f:
            j += 1
        out.append(num(pts[j][1]))
    return out


def _status_track(ap, total):
    """Status 轨道 → 每帧的 ``Flags`` 整数（0 表示没有可见性数据）。

    隐藏 = ``0x20000000``；另有 ``FLIP_X/FLIP_Y``（0x08000000/0x04000000）
    与 ``FLIP_TEXTURE_X/_Y``（0x02000000/0x01000000）。
    """
    box = ap.get("Status") or {}
    vals = box.get("TableValue") or []
    if not vals:
        return [0] * total
    if len(vals) == 1:
        return [int((vals[0] or {}).get("Flags") or 0)] * total
    pts = _cpe_pairs(box, total)
    if not pts:
        return [int((vals[0] or {}).get("Flags") or 0)] * total
    out, j = [], 0
    for f in range(total):
        while j + 1 < len(pts) and pts[j + 1][0] <= f:
            j += 1
        out.append(int((pts[j][1] or {}).get("Flags") or 0))
    return out


def _status_vis(ap, total):
    """兼容旧调用点：隐藏位 → 1.0/0.0。"""
    return [0.0 if (fl & 0x20000000) else 1.0 for fl in _status_track(ap, total)]


def _log(msg):
    print(msg, flush=True)


#: 「本机从这个包里导不出 SD 小人」的编号登记（cwd = userdata）。
#: 有些包只有特效 / Instance 骨架，当前解析器合不出图；它们不该再进
#: "应有张数"的分母，否则页面永远显示差几个、补全按钮永远亮着（2026-11-15
#: 反馈「跑完了还是没补上」）。将来解析能力提升、导出成功会自动撤销登记。
#: 服务端读同一份文件。
STATE_FILE = os.path.join("cdn", "state", "sd_unavailable.json")


def _load_unavailable() -> set[int]:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return {int(x) for x in (json.load(f) or [])}
    except (OSError, ValueError, TypeError):
        return set()


def _set_unavailable(num: int, bad: bool) -> None:
    """合并写回：只有「确认这个包没有 SD」/「后来又导出了」才改，幂等。"""
    ids = _load_unavailable()
    before = set(ids)
    if bad:
        ids.add(num)
    else:
        ids.discard(num)
    if ids == before:
        return
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(ids), f)
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass


def manifest_index():
    """清单 → {包名: 条目}；清单在 ``cdn/Manifest*Ex.json``（cwd = userdata）。"""
    for base in (os.path.join("cdn"), ".", os.path.join("..", "server", "cdn", "data")):
        p1 = os.path.join(base, "Manifest1Ex.json")
        if not os.path.exists(p1):
            continue
        idx = {}
        for fn in ("Manifest1Ex.json", "Manifest2Ex.json"):
            p = os.path.join(base, fn)
            if not os.path.exists(p):
                continue
            with open(p, encoding="utf-8") as f:
                for e in json.load(f)["data"]:
                    idx[e["bundle_name"]] = e
        return idx
    raise SystemExit("找不到资源清单：请先在控制台「基础数据」里下载一次")


# ---------------------------------------------------------------- SS6 解析
class Unit:
    """一个 ``bundle_unit_*`` 包里跟"画一张图"有关的东西。"""

    def __init__(self, path):
        self.atlases: list = []
        self.cellmaps: list[list] = []
        # 每个格子还可能带**三角网格**（TableCell.Mesh）：身体 / 头发 / 胯 / 裙 /
        # 花环鞭这 19 个"网格部件"的真实形状与蒙皮顶点全在这里。以前完全没读，
        # 一律当矩形贴图，这就是「所有 SD 小人绑定都错、部位错乱」的根因。
        self.cellmesh: list[list] = []
        self.trees: dict[str, dict] = {}
        env = UnityPy.load(path)
        dcs = []
        skipped = []
        # 一个包里常有多张图集（身体 / 特效 / 分身），TableCellMap 也有多条，
        # 每条各自指向一张贴图。以前把所有 cell 平铺成一个列表、只留第一张图，
        # 于是 IndexCell 越过第一张 cellmap 长度的部件会从错误的图上取格子 ——
        # 这正是「头发和脚乱飞」的主因之一。
        texs: dict[str, list] = {}
        for obj in env.objects:
            if obj.type.name == "Texture2D":
                try:
                    d = obj.read()
                    img = d.image.convert("RGBA")
                    texs.setdefault(str(getattr(d, "m_Name", "") or ""), []).append(img)
                except Exception:                                # noqa: BLE001
                    pass
                continue
            if obj.type.name != "MonoBehaviour":
                continue
            try:
                name = str(obj.read().m_Name or "")
            except Exception:                                    # noqa: BLE001
                continue
            # 特效树（da_*_effect* / *_ef）永远不含身体部件，却常有几十万字符的
            # typetree（408 的 effect_front 就 567 KB）。以前是先解析再判断，
            # 白解析；名字在 read() 里已经有了，直接跳过省掉这部分。身体树
            # da_408_BC_mot 本身 6 MB，那部分省不掉。
            low = name.lower()
            if "effect" in low or low.endswith("_ef") or "_ef_" in low:
                skipped.append(obj)      # 兜底：万一整包没有身体树再回头解析
                continue
            try:
                tt = obj.read_typetree()
            except Exception:                                    # noqa: BLE001
                continue
            if "TableCellMap" in tt:
                dcs.append(tt)
            elif "TableAnimation" in tt:
                self.trees[name] = tt

        # 兜底：整包都被"特效名"过滤掉了（个别单位的身体树名字里带 effect）
        if not self.trees and skipped:
            for obj in skipped:
                try:
                    tt = obj.read_typetree()
                except Exception:                                # noqa: BLE001
                    continue
                if "TableAnimation" in tt:
                    self.trees[str(obj.read().m_Name or "")] = tt

        for dc in dcs:
            for cm in dc.get("TableCellMap") or []:
                cname = str(cm.get("Name") or "")
                sz = cm.get("SizeOriginal") or {}
                want = (float(sz.get("x") or 0), float(sz.get("y") or 0))
                pool = texs.get(cname) or []
                if not pool:
                    pool = [im for k, v in texs.items()
                            if cname and k.startswith(cname) for im in v]
                if not pool:
                    continue
                img = (min(pool, key=lambda im: abs(im.width - want[0])
                           + abs(im.height - want[1])) if want[0] else pool[0])
                rx = img.width / want[0] if want[0] else 1.0
                ry = img.height / want[1] if want[1] else 1.0
                cells = []
                for c in cm.get("TableCell") or []:
                    r = c.get("Rectangle") or {}
                    pv = c.get("Pivot") or {}
                    cells.append([
                        float(r.get("x") or 0) * rx, float(r.get("y") or 0) * ry,
                        float(r.get("width") or 0) * rx, float(r.get("height") or 0) * ry,
                        float(pv.get("x") or 0) * rx, float(pv.get("y") or 0) * ry,
                    ])
                self.atlases.append(img)
                self.cellmaps.append(cells)
                mm = []
                for c in cm.get("TableCell") or []:
                    me = c.get("Mesh") or {}
                    co = me.get("TableCoordinate") or []
                    mm.append(([(float(p.get("x") or 0) * rx,
                                 float(p.get("y") or 0) * ry) for p in co],
                               [int(i) for i in (me.get("TableIndexVertex") or [])]))
                self.cellmesh.append(mm)

        # 兼容旧调用点 / 诊断脚本
        self.atlas = self.atlases[0] if self.atlases else None
        self.cells = self.cellmaps[0] if self.cellmaps else []

    def cell_at(self, ci, mi):
        """按 SS6 的 ``(IndexCell, IndexCellMap)`` 取格子与它所属的图集。"""
        g = mi if isinstance(mi, int) and 0 <= mi < len(self.cellmaps) else 0
        if not isinstance(ci, int) or ci < 0:
            return None, None
        cells = self.cellmaps[g] if g < len(self.cellmaps) else []
        if ci >= len(cells):
            return None, None
        return cells[ci], (self.atlases[g] if g < len(self.atlases) else None)

    def cell_mesh(self, ci, mi):
        """格子的三角网格：``(顶点[(x,y)...], 三角形索引[...])``。"""
        g = mi if isinstance(mi, int) and 0 <= mi < len(self.cellmesh) else 0
        arr = self.cellmesh[g] if g < len(self.cellmesh) else []
        if not isinstance(ci, int) or ci < 0 or ci >= len(arr):
            return [], []
        return arr[ci]

    def body_tree(self, num):
        """身体结构 = **待机动作里真的绑定了图集格子**的那棵树。

        老逻辑按「部件最多 + 有 stand」选，会选中 ``da_<编号>_BC`` 外壳：
        它 99~153 个部件看着最像身体，但 ``Cell.TableValue`` 全空（只是挂
        特效 / Instance 的骨架），合成出来是一张全透明图。真身体是
        ``da_<编号>_BC_mot`` / ``da_ins_<编号>_BC`` / ``*_mot`` 这类。
        2026-11-15 实测：8 个补不全的 NPC 里有 7 个都栽在这里；连已经导出
        过图的 10001 用老逻辑重做也会失败（旧图只是被"文件已存在就跳过"
        掩盖了）。所以判据改成 stand 动画里带 Cell 的部件数，总部件数只
        用来打破平手。
        """
        best = (None, None, -1)
        for name, t in self.trees.items():
            anim = finding_pose(t, "stand")
            if anim is None:
                continue
            n_cells = sum(1 for ap in (anim.get("TableParts") or [])
                          if (ap.get("Cell") or {}).get("TableValue"))
            if not n_cells:
                continue
            score = n_cells * 100 + len(t.get("TableParts") or [])
            if score > best[2]:
                best = (name, t, score)
        if best[0]:
            return best[0], best[1]
        return None, None


def _mat_mul(m, n):
    """2D 仿射矩阵相乘（``(a,b,c,d,e,f)`` 同 PIL 的 AFFINE 顺序：
    ``x' = a x + b y + c``，``y' = d x + e y + f``）。"""
    a1, b1, c1, d1, e1, f1 = m
    a2, b2, c2, d2, e2, f2 = n
    return (a1 * a2 + b1 * d2, a1 * b2 + b1 * e2, a1 * c2 + b1 * f2 + c1,
            d1 * a2 + e1 * d2, d1 * b2 + e1 * e2, d1 * c2 + e1 * f2 + f1)


def _mat_local(pos, rot_z, scl):
    """SS6 的局部变换：``T(pos) · R(z) · S(scl)``（y 轴向上、角度逆时针为正）。

    2026-11-15 修正：以前这里是 ``radians(-rot_z)``，把旋转方向做成了镜像 ——
    屏幕上 +θ 变成顺时针，于是所有角色的四肢/鞭子都朝反方向折。
    官方口径（AnimationParts.cs 3687 ``matrixCorrection * localToWorld``）
    是「模型空间 +θ，最后统一翻转 y 交给屏幕」，等价于这里直接用 +θ 再让
    ``_draw_cell`` 做 ``screen=(W/2+X, W/2-Y)``。408 待机实测：改前双臂下垂、
    花环鞭竖垂，改后与游戏内截图一致（双手胸前横握、鞭横贯身体）。
    """
    import math
    r = math.radians(float(rot_z or 0.0))
    cs, sn = math.cos(r), math.sin(r)
    # SS6 弹出动画首帧 Scaling=0 是合法值，不能用 `or 1.0` 把它吃掉。
    # **保留符号**：负缩放是 2D 翻转，而且是成对使用的 ——
    #   attack_2_1：N_chara_all = -1（整体镜像，右手攻击）+ F_head_00 = -1
    #               （把脸镜像回正面），两者相乘后只有身子翻、脸不翻。
    #   damage / victory_1：F_head_00 = -1 → 头部整体镜像（游戏里受击段
    #               头饰/发型确实翻到另一侧）、F_leg_BD00 = -1 → 小腿朝向。
    # 之前统一 abs() 的结果就是"头不翻、腿歪、左右手反、闭眼反"。
    sx = 1.0 if scl[0] is None else float(scl[0])
    sy = 1.0 if scl[1] is None else float(scl[1])
    # R·S
    a, b = cs * sx, -sn * sy
    d, e = sn * sx, cs * sy
    return (a, b, float(pos[0] or 0.0), d, e, float(pos[1] or 0.0))


#: 轨道展开缓存：key=(anim 对象 id, 帧数)，一个动画只展开一次。
#: 以前 77 帧 × 84 部件 × 4 轨道 = 2.6 万次 _track()，占了导出时间的 1/5。
_TRACK_CACHE = {}


def _anim_tracks(anim, total):
    key = (id(anim), total)
    hit = _TRACK_CACHE.get(key)
    # 把 anim 本体留在缓存里：否则对象被回收后 id 可能复用，命中到别的角色
    if hit is not None and hit[0] is anim:
        return hit[1]
    rows = []
    zero_pos = [{"x": 0, "y": 0}] * total
    one_scl = [{"x": 1, "y": 1}] * total
    zero_rot = [{"x": 0, "y": 0, "z": 0}] * total
    one_opa = [1.0] * total
    for ap in anim.get("TableParts") or []:
        cv = (ap.get("Cell") or {}).get("TableValue") or []
        rows.append({
            "pos": _track(ap, "Position", total) or zero_pos,
            "scl": _scl_track(ap, total) or one_scl,
            "rot": _track(ap, "Rotation", total) or zero_rot,
            "opa": _track(ap, "RateOpacity", total) or one_opa,
            # ScalingLocal：第二条缩放轨道（官方 UPDATE_SCALELOCAL），与 Scaling
            # 相乘。全库抽样 9409 个部件里只有 5 个用到，但漏了就是尺寸错。
            "scll": _track(ap, "ScalingLocal", total) or one_scl,
            # OffsetPivot：改格子的支点（不改尺寸）。同样极少用（3/9409）。
            "piv": _track(ap, "OffsetPivot", total) or zero_pos,
            "vis": _status_vis(ap, total),
            # 可见性之外的位：FLIP_X/FLIP_Y（绕 cell 原点翻转）与
            # FLIP_TEXTURE_X/_Y（只翻 UV）。victory_1 的 F_arm_FU00 就有 2 帧
            # 带 FLIP_TEXTURE_X，不处理那两帧袖子会左右反。
            "flags": _status_track(ap, total),
            # 注意：**不要**用 StatusParts 的 0x10000000 判断隐藏。
            # 2026-11-16 曾把它当"隐藏整棵子树"(HIDE_FULL)，结果 00136 的
            # joint_10..13、00338 的 weapon_1 等大批部件被连带隐藏 ——
            # 整批角色"没头发 / 没头 / 没衣服 / 没武器"。实测该位与可见性
            # 无关（0x501c0000 这类在完全正常的部件上也普遍存在）。
            # 可见性只看 Status.Flags 的 HIDE(0x20000000)。
            "cell": _cell_track(ap, total) or [[
                int(cv[0].get("IndexCell") or -1) if cv else -1,
                int(cv[0].get("IndexCellMap") or 0) if cv else 0,
            ]] * total,
            "prv": _priority_track(ap, total) or [],
        })
    if len(_TRACK_CACHE) > 8:
        _TRACK_CACHE.clear()
    _TRACK_CACHE[key] = (anim, rows)
    return rows


def _at(arr, f):
    return arr[min(f, len(arr) - 1)] if arr else None


def ctrace_ok(pair):
    return isinstance(pair, (list, tuple)) and len(pair) >= 2


def _flatten_m(tree, anim, unit, frame, total):
    """某一帧 → ``[(cell, atlas, (a,b,c,d,e,f), opacity)]``，矩阵已合成到世界坐标。

    轨道只在第一次调用时展开（``_anim_tracks``），之后每帧只做取值与矩阵合成。
    """
    parts = tree.get("TableParts") or []
    parent_of = {}
    for p in parts:
        pid, par = p.get("ID"), p.get("IDParent")
        parent_of[-1 if pid is None else int(pid)] = -1 if par is None else int(par)

    rows = _anim_tracks(anim, total)
    f = min(frame, total - 1)
    local = {}
    for pi, ap in enumerate(anim.get("TableParts") or []):
        bp = parts[pi] if pi < len(parts) else {}
        pid = int(bp.get("ID") if bp.get("ID") is not None else pi)
        t = rows[pi] if pi < len(rows) else None
        if t is None:
            continue
        p0 = _at(t["pos"], f) or {}
        s0 = _at(t["scl"], f) or {}
        r0 = _at(t["rot"], f) or {}
        o0 = _at(t["opa"], f)
        vis = t["vis"]
        ctr = t["cell"]
        pair = ctr[f] if (ctr and f < len(ctr)) else None
        if isinstance(pair, (list, tuple)) and len(pair) >= 2:
            ci, mi = pair[0], pair[1]
        else:
            ci, mi = -1, 0
        cell, atlas = unit.cell_at(ci, mi)
        local[pid] = {
            "cell": cell, "atlas": atlas,
            # Status.Flags 的隐藏位**只作用于本部件**：SS6 里大量"分组/容器"
            # 节点自己不带图却被标隐藏，若把它混进 opa 再沿父链连乘，
            # 整棵子树都会消失（真夏のフウカ 246 部件只画出 1 个就是这个原因）。
            # 真正能继承的是 RateOpacity，隐藏只在这里单独判断。
            "opa": 1.0 if o0 is None or isinstance(o0, dict) else float(o0),
            "vis": (vis[f] if f < len(vis) else 1.0),
            "vec": (float(p0.get("x") or 0), float(p0.get("y") or 0),
                    1.0 if s0.get("x") is None else float(s0.get("x")),
                    1.0 if s0.get("y") is None else float(s0.get("y")),
                    float(r0.get("z") or 0) if isinstance(r0, dict) else 0.0),
            "pr": int(t["prv"][f]) if t["prv"] else 0,
        }

    cache = {}

    def world_mat(pid, guard=0):
        """SS6 的父子合成：**标准仿射连乘** ``parent · local``。

        为什么必须是完整连乘（SV5 修正）
        ------------------------------
        SS6 里子部件的 Position 是**父级坐标系**里的坐标，父级一旋转，
        子部件的"锚点"跟着转；旋转角累加、缩放相乘、透明度相乘。
        SV4 那版为了"修甩飞"把位置改成「只吃父级缩放、不吃父级旋转」，
        结果手臂/武器/头发在攻击、受击这些大角度动作里整体错位 ——
        子部件还留在未旋转的位置上，父级却被转过来了（2026-11-15 用户反馈
        「所有 SD 小人的动作绑定都有问题，部位错乱」）。
        真正的修法是完整矩阵，而不是拆开位置和旋转。

        ``guard`` 只是防御：数据异常（环状父子）时别递归爆栈。
        """
        if pid in cache:
            return cache[pid]
        if guard > 12:
            return ((1.0, 0.0, 0.0, 0.0, 1.0, 0.0), 1.0)
        it = local[pid]
        par = parent_of.get(pid, -1)
        pm, po = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0), 1.0
        if par in local and par != pid:
            pm, po = world_mat(par, guard + 1)
        x, y, sx, sy, rz = it["vec"]
        m = _mat_mul(pm, _mat_local((x, y), rz, (sx, sy)))
        v = (m, po * it["opa"])
        cache[pid] = v
        return v

    out = []
    for pid in sorted(local, key=lambda i: (local[i]["pr"], i)):
        it = local[pid]
        if not it["cell"] or it["vis"] <= 0:
            continue
        m, opa = world_mat(pid)
        if opa <= 0.02:
            continue
        out.append((it["cell"], it["atlas"], m, opa))
    return out


_AFFINE = getattr(getattr(Image, "Transform", Image), "AFFINE", 2)


def _draw_cell(canvas, atlas, cell, m, opa, W, clamp=True):
    """把一个格子按**完整仿射矩阵**贴到画布上。

    以前是「crop → resize → rotate(绕几何中心) → 再 resize 回 bbox」的近似：
    旋转角度多了一个负号、支点 pivot 没参与、父级剪切被压扁，四肢/头发
    大角度时就会乱飞。这里改成一次 PIL AFFINE：把
    「格子像素(U,V) → 屏幕像素(sx,sy)」的正向矩阵求逆，在小 patch 上做
    反向映射，pivot / 旋转 / 缩放 / 剪切全部由一个矩阵精确表达。
    """
    if atlas is None or cell is None:
        return
    cx, cy, cw, ch, px, py = cell
    cx, cy = int(round(cx)), int(round(cy))
    cw, ch = int(round(cw)), int(round(ch))
    if cw < 1 or ch < 1:
        return
    a, b, tx, d, e, ty = m
    if clamp and (abs(a) > 24 or abs(b) > 24 or abs(d) > 24 or abs(e) > 24):
        return
    # 四角（SS6 局部：pivot 为原点、y 向上）。
    # 权威口径（AnimationParts.cs 3647-3652）：scaleMeshY=-sy（Y 轴取反），
    # top = +pivot.y·sy，bottom = -(size.y-pivot.y)·sy；bundle 的 Pivot.y 是
    # 格子顶边起算（y-down），不要再当底边距离。左上角 LU/RU 在 top 边，
    # LD/RD 在 bottom 边。此 bbox 必须与下方正向映射的 ly=py-(V-cy) 一致。
    pts = []
    for lx, ly in ((-px, py), (cw - px, py),
                   (cw - px, -(ch - py)), (-px, -(ch - py))):
        X = a * lx + b * ly + tx
        Y = d * lx + e * ly + ty
        pts.append((W / 2 + X, W / 2 - Y))
    x0 = min(q[0] for q in pts); y0 = min(q[1] for q in pts)
    x1 = max(q[0] for q in pts); y1 = max(q[1] for q in pts)
    xi = max(0, min(W - 1, int(math.floor(x0))))
    yi = max(0, min(W - 1, int(math.floor(y0))))
    w = max(1, min(W - xi, int(math.ceil(x1)) - xi))
    h = max(1, min(W - yi, int(math.ceil(y1)) - yi))

    # 正向：(U,V) -> (sx,sy)，其中 U,V 是图集内绝对像素
    A, B = a, -b
    C = W / 2 + tx - a * px + b * py - a * cx + b * cy
    D, E = -d, e
    F = W / 2 - ty + d * px - e * py + d * cx - e * cy
    det = A * E - B * D
    if abs(det) < 1e-6:                       # 缩放 0 / 退化矩阵：这一帧不画
        return
    ia, ib = E / det, -B / det
    jd, je = -D / det, A / det
    ic = (B * F - C * E) / det
    jf = (D * C - A * F) / det
    # patch 内 (i,j) 对应画布 (xi+i, yi+j)，反查回「格子内」坐标
    cu = ia * xi + ib * yi + ic - cx
    cv = jd * xi + je * yi + jf - cy
    try:
        sub = atlas.crop((cx, cy, cx + cw, cy + ch))
        patch = sub.transform((w, h), _AFFINE, (ia, ib, cu, jd, je, cv),
                              resample=Image.BICUBIC)
    except Exception:                                        # noqa: BLE001
        return
    if opa < 0.999:
        patch.putalpha(patch.getchannel("A").point(lambda v: int(v * opa)))
    canvas.alpha_composite(patch, (xi, yi))


# ---------------------------------------------------------------- 网格 / 蒙皮
#: 图集 → numpy 浮点数组（0~1，直通 alpha），避免每帧重复转换。
_NP_ATLAS: dict = {}


def _atlas_np(atlas):
    if atlas is None:
        return None
    key = id(atlas)
    hit = _NP_ATLAS.get(key)
    if hit is not None and hit[0] is atlas:
        return hit[1]
    arr = np.asarray(atlas.convert("RGBA"), dtype=np.float32) / 255.0
    if len(_NP_ATLAS) > 16:
        _NP_ATLAS.clear()
    _NP_ATLAS[key] = (atlas, arr)
    return arr


#: 纹理采样方式：``False`` = 双线性（官方 SS6 的 texture filter），
#: ``True`` = 最近邻。SD 小人是**像素画 + 1:1 导出**，最近邻本该更快也更锐，
#: 但会不会和游戏观感不一致要 A/B 看图定（见 ``--nearest`` 开关）。
SAMPLE_NEAREST = False


def _fill_tri(cv, at, p0, p1, p2, s0, s1, s2, opa, tint=None, op=0):
    """把一个带纹理的三角形画进 float32 画布（直通 alpha 混合）。

    官方 DrawMesh（AnimationParts.cs 3806-4160）给的是**逐顶点坐标 + 逐顶点
    UV + 三角形索引**，没有"矩形"这个概念。所以这里用栅格化而不是贴图变形：
    网格部件（身体/头发/胯/裙/花环鞭）只有这样才会出现真实的轮廓和弯曲。
    """
    H, W = cv.shape[0], cv.shape[1]
    bx0 = max(0, int(math.floor(min(p0[0], p1[0], p2[0]))))
    by0 = max(0, int(math.floor(min(p0[1], p1[1], p2[1]))))
    bx1 = min(W, int(math.ceil(max(p0[0], p1[0], p2[0]))))
    by1 = min(H, int(math.ceil(max(p0[1], p1[1], p2[1]))))
    if bx1 <= bx0 or by1 <= by0:
        return
    e0x, e0y = p1[0] - p0[0], p1[1] - p0[1]
    e1x, e1y = p2[0] - p0[0], p2[1] - p0[1]
    det = e0x * e1y - e0y * e1x
    if abs(det) < 1e-9:
        return
    ys = np.arange(by0, by1, dtype=np.float32) + 0.5
    xs = np.arange(bx0, bx1, dtype=np.float32) + 0.5
    gx = xs[None, :] - p0[0]
    gy = ys[:, None] - p0[1]
    a = (gx * e1y - gy * e1x) / det
    bb = (gy * e0x - gx * e0y) / det
    inside = (a >= -1e-4) & (bb >= -1e-4) & ((a + bb) <= 1.0 + 1e-4)
    # 只对**三角形覆盖到的像素**做采样与混合。
    # 以前是拿整个包围盒去算（外圈像素算完再靠 `* inside` 把 alpha 归零丢
    # 掉），而网格部件的三角形多为细长条（裙摆、鞭子、发丝），包围盒里真正
    # 落在三角形内的常常不到三成 —— 那七成采样/4 次 gather/混合全是白干。
    # 这是本函数 83% 占比的主因，改完单帧从 135ms 降到 ~60ms。
    ii, jj = np.nonzero(inside)
    n = ii.size
    if n == 0:
        return
    aa = a[ii, jj]
    bbb = bb[ii, jj]
    uu = s0[0] + aa * (s1[0] - s0[0]) + bbb * (s2[0] - s0[0])
    vv = s0[1] + aa * (s1[1] - s0[1]) + bbb * (s2[1] - s0[1])
    ah = at.shape[0]
    aw = at.shape[1]
    u1 = np.clip(uu, 0, aw - 1.001)
    v1 = np.clip(vv, 0, ah - 1.001)
    iu = np.floor(u1).astype(np.int32)
    iv = np.floor(v1).astype(np.int32)
    if SAMPLE_NEAREST:
        col = at[iv, iu]
    else:
        fu = (u1 - iu)[:, None]
        fv = (v1 - iv)[:, None]
        du = np.minimum(iu + 1, aw - 1)
        dv = np.minimum(iv + 1, ah - 1)
        c00 = at[iv, iu]
        c01 = at[iv, du]
        c10 = at[dv, iu]
        c11 = at[dv, du]
        col = ((c00 * (1 - fu) + c01 * fu) * (1 - fv)
               + (c10 * (1 - fu) + c11 * fu) * fv)
    if tint is not None:
        # 部件色（PartsColor.VertexColor + RateAlpha）：不乘上去，斩击/光效
        # 会以贴图原色（灰白）糊在角色身上。
        col[:, 0] *= tint[0]
        col[:, 1] *= tint[1]
        col[:, 2] *= tint[2]
        col[:, 3] *= tint[3]
    sa = col[:, 3]
    if opa < 0.999:
        sa = sa * opa
    if not sa.any():
        return
    dst = cv[by0:by1, bx0:bx1]
    src = dst[ii, jj]
    da = src[:, 3]
    if op == 1:                                   # ADD
        oa = np.minimum(1.0, da + sa)
        safe = np.maximum(oa, 1e-6)
        rgb = (col[:, :3] * sa[:, None] + src[:, :3] * da[:, None]) / safe[:, None]
    elif op == 3:                                 # MUL
        rgb = src[:, :3] * (1.0 - sa[:, None] + (col[:, :3] * sa[:, None]))
        oa = da
    elif op == 5:                                 # SCR
        rgb = 1.0 - (1.0 - src[:, :3]) * (1.0 - col[:, :3] * sa[:, None])
        oa = np.minimum(1.0, da + sa)
    elif op == 2:                                 # SUB
        rgb = np.maximum(0.0, src[:, :3] - col[:, :3] * sa[:, None])
        oa = da
    else:                                         # MIX（默认）
        oa = sa + da * (1.0 - sa)
        safe = np.maximum(oa, 1e-6)
        rgb = (col[:, :3] * sa[:, None]
               + src[:, :3] * (da * (1.0 - sa))[:, None]) / safe[:, None]
    out = np.empty((n, 4), dtype=dst.dtype)
    out[:, :3] = rgb
    out[:, 3] = oa
    dst[ii, jj] = out


def _parts_color_at(ap, f, total):
    """部件色 + 混合模式（PartsColor 的 VertexColor / RateAlpha / Operation）。"""
    pc = ap.get("PartsColor") or {}
    vals = pc.get("TableValue") or []
    codes = ((pc.get("TableCodeValue") or [{}])[0].get("TableCode")) or []
    if not vals:
        return None, 0
    idx = 0
    if codes and len(codes) == len(vals):
        for i, c in enumerate(codes):
            if (int(c) & 0x1FFF) <= f:
                idx = i
    v = vals[min(idx, len(vals) - 1)] or {}
    vc = v.get("VertexColor") or []
    ra = v.get("RateAlpha") or []
    if not vc:
        return None, int(v.get("Operation") or 0)
    c0 = vc[0] or {}
    a = float(ra[0]) if ra else 1.0
    return ((float(c0.get("r") if c0.get("r") is not None else 1.0),
             float(c0.get("g") if c0.get("g") is not None else 1.0),
             float(c0.get("b") if c0.get("b") is not None else 1.0),
             float(c0.get("a") if c0.get("a") is not None else 1.0) * a),
            int(v.get("Operation") or 0))


def _frame_state(tree, anim, unit, frame, total):
    """一帧的部件状态 + 世界矩阵（模型空间：y 向上、+θ 逆时针）。"""
    parts = tree.get("TableParts") or []
    cat = tree.get("CatalogParts") or {}
    bone_list = list(cat.get("TableIDPartsBone") or [])
    mesh_ids = set(cat.get("TableIDPartsMesh") or [])
    rows = _anim_tracks(anim, total)
    f = min(frame, total - 1)

    st = {}
    for pi, ap in enumerate(anim.get("TableParts") or []):
        bp = parts[pi] if pi < len(parts) else {}
        pid = int(bp.get("ID") if bp.get("ID") is not None else pi)
        t = rows[pi] if pi < len(rows) else None
        if t is None:
            continue
        p0 = _at(t["pos"], f) or {}
        s0 = _at(t["scl"], f) or {}
        sl0 = _at(t.get("scll") or [], f) or {}
        pv0 = _at(t.get("piv") or [], f) or {}
        r0 = _at(t["rot"], f) or {}
        o0 = _at(t["opa"], f)
        pair = t["cell"][f] if (t["cell"] and f < len(t["cell"])) else None
        ci, mi = (int(pair[0]), int(pair[1])) if ctrace_ok(pair) else (-1, 0)
        st[pid] = {
            "par": -1 if bp.get("IDParent") is None else int(bp.get("IDParent")),
            "name": str(bp.get("Name") or ""),
            "ismesh": pid in mesh_ids,
            "cell": (ci, mi),
            "opa": 1.0 if o0 is None or isinstance(o0, dict) else float(o0),
            "vis": float(t["vis"][f]) if (t["vis"] and f < len(t["vis"])) else 1.0,
            "flags": int(t["flags"][f]) if (t.get("flags") and f < len(t["flags"])) else 0,
            "prv": int(t["prv"][f]) if t["prv"] else 0,
            # Scaling 原样进变换。ScalingLocal 官方是**另一条独立轨道**：
            # 只置 UPDATE_SCALELOCAL 标志、在绘制阶段用，**不传给子级**
            # （Control/AnimationParts.cs 876-887：ValueGet 到 ScaleLocal 里）。
            # 所以不能乘进 vec，否则子部件会被连带缩放。
            "vec": (float(p0.get("x") or 0), float(p0.get("y") or 0),
                    1.0 if s0.get("x") is None else float(s0.get("x")),
                    1.0 if s0.get("y") is None else float(s0.get("y")),
                    float(r0.get("z") or 0) if isinstance(r0, dict) else 0.0),
            "pivo": (float(pv0.get("x") or 0), float(pv0.get("y") or 0)),
            "mesh": (bp.get("Mesh") or {}),
            "deform": _deform_at(ap, f, total),
            "vc": _vc_at(ap, f, total),
            "tint": _parts_color_at(ap, f, total)[0],
            "op": _parts_color_at(ap, f, total)[1],
        }

    cache = {}

    def world(pid, guard=0):
        if pid in cache:
            return cache[pid]
        it = st[pid]
        par = it["par"]
        pm = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)
        if par in st and par != pid and guard < 32:
            pm = world(par, guard + 1)
        x, y, sx, sy, rz = it["vec"]
        m = _mat_mul(pm, _mat_local((x, y), rz, (sx, sy)))
        cache[pid] = m
        return m

    for pid in st:
        world(pid)
    return st, cache, bone_list


#: VertexCorrection / Deform 的逐顶点坐标轨道 → 某一帧的顶点表。
#: 关键帧位置必须读 ``TableCode`` 的帧号（以前按"值个数 / 总帧数"均分，
#: 关键帧不在均分点上时整段顶点变形的时间轴就错了）。
def _coord_track(ap, key, sub, total):
    box = ap.get(key) or {}
    vals = box.get("TableValue") or []
    if not vals:
        return []
    pts = []
    for fr, v in _cpe_pairs(box, total):
        co = (v or {}).get(sub) or []
        pts.append((fr, [(float(q.get("x") or 0), float(q.get("y") or 0)) for q in co]
                    if co else None))
    return pts


def _coords_at(pts, f):
    if not pts:
        return None
    cur = pts[0]
    for k, (fr, _v) in enumerate(pts):
        if fr <= f:
            cur = pts[k]
        else:
            break
    c0 = cur[1]
    if c0 is None:
        return None
    tgt = cur
    for fr, v in pts:
        if fr > cur[0] and v is not None:
            tgt = (fr, v)
            break
    if tgt is cur or tgt[0] == cur[0] or len(tgt[1]) != len(c0):
        return c0
    t = (f - cur[0]) / (tgt[0] - cur[0])
    if t <= 1e-3 or t >= 1.0:
        return c0 if t <= 0.5 else tgt[1]
    return [(c0[i][0] * (1 - t) + tgt[1][i][0] * t,
             c0[i][1] * (1 - t) + tgt[1][i][1] * t) for i in range(len(c0))]


def _deform_at(ap, f, total):
    """Deform 轨道取某一帧的**逐顶点偏移**（顶点动画）。"""
    return _coords_at(_coord_track(ap, "Deform", "TableCoordinate", total), f)


#: VertexCorrection 四角顺序 = ``KindVertex`` 的顺序 **LU, RU, RD, LD**
#: （官方 Attribute.VertexCorrection.Coordinate 固定 4 项，按 KindVertex 排）。
#: 本渲染器的 corners 也是 [左上,右上,右下,左下]，所以**必须原序对应**。
#: 以前写成 (0,1,3,2)（把 RD/LD 对调），于是每个部件的四个角被交叉平移，
#: 眼睛/嘴/脖子这些靠顶点变形做表情的部位整体歪掉。
VC_ORDER = (0, 1, 2, 3)


def _vc_at(ap, f, total):
    """VertexCorrection（顶点变形）轨道：普通 sprite 四角的逐帧偏移。"""
    return _coords_at(_coord_track(ap, "VertexCorrection", "Coordinate", total), f)


def _part_geom(unit, st, worlds, bone_list, pid, W):
    """一个部件的绘制几何：屏幕坐标顶点 + 图集 UV + 三角形索引。"""
    it = st[pid]
    ci, mi = it["cell"]
    cell, atlas = unit.cell_at(ci, mi)
    if cell is None or atlas is None:
        return None
    cx, cy, cw, ch, px, py = cell
    # OffsetPivot：改支点（官方 Parts.OffsetPivot 轨道），与 pivot 同向累加
    _opx, _opy = it.get("pivo") or (0.0, 0.0)
    px += _opx
    py += _opy
    cx, cy = int(round(cx)), int(round(cy))
    cw, ch = int(round(cw)), int(round(ch))
    if cw < 1 or ch < 1:
        return None
    M = worlds[pid]
    opa = it["opa"]
    pm = it["mesh"]
    bind = pm.get("TableVertex") or []
    rate_uv = pm.get("TableRateUV") or []
    cverts, ctris = unit.cell_mesh(ci, mi)

    if it["ismesh"] and len(cverts) >= 3 and len(ctris) >= 3 and len(rate_uv) == len(cverts):
        verts = []
        deform = it["deform"]
        for i, (tx, ty) in enumerate(cverts):
            lx = tx - px
            ly = py - ty
            if i < len(bind):
                bones = bind[i].get("TableBone") or []
                if bones:
                    sx = sy = 0.0
                    for bn in bones:
                        idx = int(bn.get("Index") or 0)
                        if not (0 <= idx < len(bone_list)):
                            continue
                        bpid = int(bone_list[idx])
                        bm = worlds.get(bpid)
                        if bm is None:
                            continue
                        off = bn.get("CoordinateOffset") or {}
                        wx, wy = _mat_pt(bm, float(off.get("x") or 0),
                                         float(off.get("y") or 0))
                        wgt = float(bn.get("Weight") or 0)
                        sx += wx * wgt
                        sy += wy * wgt
                    if deform and i < len(deform):
                        # 骨骼路径：Deform 按**向量**叠加（官方 4082 行）
                        dx, dy = _mat_vec(M, deform[i][0], deform[i][1])
                        sx += dx
                        sy += dy
                    verts.append((sx, sy))
                    continue
            if deform and i < len(deform):
                lx += deform[i][0]
                ly += deform[i][1]
            verts.append(_mat_pt(M, lx, ly))
        uvs = [(cx + math.floor(float(p.get("x") or 0) * cw),
                cy + math.floor(float(p.get("y") or 0) * ch)) for p in rate_uv]
        tris = [t for t in ctris if t < len(verts)]
        return {"atlas": atlas, "verts": [_sx(v, W) for v in verts], "uvs": uvs,
                "tris": tris, "opa": opa, "tint": it["tint"], "op": it["op"]}

    # ---- 普通 sprite：矩形四角 → 2 个三角形
    lx0, lx1 = -px, cw - px
    ly1, ly0 = py, -(ch - py)
    corners = [(lx0, ly1), (lx1, ly1), (lx1, ly0), (lx0, ly0)]
    vc = it.get("vc")
    if vc and len(vc) >= 4:
        # 顶点变形在**局部空间**加到四角上，再乘世界矩阵（官方 CoordinateDraw += ）
        corners = [(x + vc[VC_ORDER[k]][0], y + vc[VC_ORDER[k]][1])
                   for k, (x, y) in enumerate(corners)]
    flags = int(it.get("flags") or 0)
    # FLIP_X / FLIP_Y（0x08000000 / 0x04000000）= 以**格子的原点（pivot）为轴**
    # 翻转整格美术；FLIP_TEXTURE_X / _Y（0x02000000 / 0x01000000）= 只翻 UV。
    # 数据里两者都会出现（victory_1 的 F_arm_FU00 就有 2 帧 FLIP_TEXTURE_X）。
    if flags & 0x08000000:
        corners = [(-x, y) for x, y in corners]
    if flags & 0x04000000:
        corners = [(x, -y) for x, y in corners]
    verts = [_mat_pt(M, x, y) for x, y in corners]
    uvs = [(cx, cy), (cx + cw, cy), (cx + cw, cy + ch), (cx, cy + ch)]
    if flags & (0x08000000 | 0x02000000):
        uvs = [(cx + cw, u[1]) if u[0] == cx else (cx, u[1]) for u in uvs]
    if flags & (0x04000000 | 0x01000000):
        uvs = [(u[0], cy + ch) if u[1] == cy else (u[0], cy) for u in uvs]
    return {"atlas": atlas, "verts": [_sx(v, W) for v in verts], "uvs": uvs,
            "tris": [0, 1, 2, 0, 2, 3], "opa": opa,
            "tint": it["tint"], "op": it["op"]}


#: 武器（花瓣鞭）的绘制序号下限。
#: 数据里武器正常取 224/225（比身体 57、头 90 都大 = 画在最上层），但
#: attack_2_1 的 ``weapon_1`` 在"翻面"那一段会掉到 **25**（比裙子 31、腿 36
#: 还小），照画就穿进身体里 —— 也就是用户报的"花瓣鞭子穿模到后面"。
#: 官方就是靠把武器抬到 699 来表示"压在最前"（同一轨道里恰好有这个值），
#: 所以低于 200 的武器序号按 699 兜底。
PRIO_WEAPON_FLOOR = 200
PRIO_WEAPON_TOP = 699


def _draw_prio(st, pid):
    it = st[pid]
    prv = int(it["prv"])
    if str(it["name"]).startswith("weapon") and prv < PRIO_WEAPON_FLOOR:
        return PRIO_WEAPON_TOP
    return prv


def _frame_geoms(unit, tree, anim, frame, total, W):
    st, worlds, bone_list = _frame_state(tree, anim, unit, frame, total)
    order = [pid for pid, it in st.items()
             if it["cell"][0] >= 0 and it["vis"] > 0 and it["opa"] > 0.02]
    order.sort(key=lambda p: (_draw_prio(st, p), p))
    for pid in order:
        g = _part_geom(unit, st, worlds, bone_list, pid, W)
        if g:
            yield g


def _frame_box(unit, tree, anim, frame, total, W):
    """不画，只算这一帧的屏幕包围盒（用来给整只小人定统一取景框）。"""
    box = None
    for g in _frame_geoms(unit, tree, anim, frame, total, W):
        v = g["verts"]
        if not v:
            continue
        x0 = min(p[0] for p in v); x1 = max(p[0] for p in v)
        y0 = min(p[1] for p in v); y1 = max(p[1] for p in v)
        box = (x0, y0, x1, y1) if box is None else (
            min(box[0], x0), min(box[1], y0), max(box[2], x1), max(box[3], y1))
    return box


def _render_canvas(unit, tree, anim, frame, total, W):
    """一帧 → ``PIL.Image``。所有部件统一走三角形栅格化（矩形=2 个三角形）。"""
    cv = np.zeros((W, W, 4), dtype=np.float32)
    for g in _frame_geoms(unit, tree, anim, frame, total, W):
        at = _atlas_np(g["atlas"])
        if at is None:
            continue
        v, u = g["verts"], g["uvs"]
        tris, opa = g["tris"], g["opa"]
        tint, op = g["tint"], g["op"]
        if len(v) < 3:
            continue
        if tris:
            for t in range(0, len(tris) - 2, 3):
                i0, i1, i2 = tris[t], tris[t + 1], tris[t + 2]
                _fill_tri(cv, at, v[i0], v[i1], v[i2],
                          u[i0], u[i1], u[i2], opa, tint, op)
        else:
            _fill_tri(cv, at, v[0], v[1], v[2], u[0], u[1], u[2], opa, tint, op)

    out = np.clip(cv * 255.0 + 0.5, 0, 255).astype(np.uint8)
    return Image.fromarray(out, "RGBA")


def _sx(pt, W):
    return (W / 2.0 + pt[0], W / 2.0 - pt[1])


def _mat_pt(m, x, y):
    a, b, c, d, e, f = m
    return (a * x + b * y + c, d * x + e * y + f)


def _mat_vec(m, x, y):
    a, b, _, d, e, _ = m
    return (a * x + b * y, d * x + e * y)


def render_frames_box(unit, tree, anim, box, frames_idx, W):
    """按**给定取景框**渲染若干帧（六动作共用同一个 box，画面不抖不跳）。"""
    out = []
    for f in frames_idx:
        c = _render_canvas(unit, tree, anim, f, max(1, int(anim.get("CountFrame") or 0)), W)
        out.append(c.crop(box))
    return out


def render_frames(unit, tree, anim, scale: float = SCALE, step: int = ANIM_STEP):
    """把一小段动画渲染成**逐帧图**（共用同一个裁剪框，否则播起来会抖）。

    ``step`` 在这里就直接跳帧，不再"先画 60 帧、最后 [::3] 丢掉 40 帧"——
    那 2/3 的合成是白做的。裁剪框由采样帧决定（待机是微动，差别极小），
    所以额外留出 8px padding，避免某一帧的手臂被裁掉一个角。
    """
    total = int(anim.get("CountFrame") or 0)
    if total <= 0:
        return []
    W = int(760 * scale)
    canvases = []
    boxes = []
    for f in range(0, total, max(1, step)):
        c = _render_canvas(unit, tree, anim, f, total, W)
        canvases.append(c)
        bb = c.getbbox()
        if bb:
            boxes.append(bb)
    if not boxes:
        return []
    u = (min(b[0] for b in boxes), min(b[1] for b in boxes),
         max(b[2] for b in boxes), max(b[3] for b in boxes))
    pad = int(8 * scale)   # 跳帧后裁剪框可能少一两像素，多留点边
    u = (max(0, u[0] - pad), max(0, u[1] - pad),
         min(W, u[2] + pad), min(W, u[3] + pad))
    return [c.crop(u) for c in canvases]


def _flatten(tree, anim, cells, frame_ratio):
    """把某段动画的某一帧摊平成 ``[(cell, wx, wy, scale, opacity)]``。

    坐标直接算成**世界坐标**（父级的位移/缩放已经乘进去），画图时只按顺序贴。
    """
    total = int(anim.get("CountFrame") or 0)
    if total <= 0:
        return []
    frame = max(0, min(int(total * frame_ratio), total - 1))
    parts = tree.get("TableParts") or []
    parent_of = {}
    for p in parts:
        pid, par = p.get("ID"), p.get("IDParent")
        parent_of[-1 if pid is None else int(pid)] = -1 if par is None else int(par)

    local = {}
    for pi, ap in enumerate(anim.get("TableParts") or []):
        bp = parts[pi] if pi < len(parts) else {}
        pid = int(bp.get("ID") if bp.get("ID") is not None else pi)
        pos = _track(ap, "Position", total) or [{"x": 0, "y": 0}] * total
        scl = _track(ap, "Scaling", total) or [{"x": 1, "y": 1}] * total
        opa = _track(ap, "RateOpacity", total) or [1.0] * total
        vis = _status_vis(ap, total)
        cv = (ap.get("Cell") or {}).get("TableValue") or []
        cell = None
        if cv:
            ci = cv[0].get("IndexCell")
            ci = -1 if ci is None else int(ci)
            if 0 <= ci < len(cells):
                cell = cells[ci]
        prv = (ap.get("Priority") or {}).get("TableValue") or []
        local[pid] = {
            "pos": pos, "scl": scl, "opa": opa, "vis": vis, "cell": cell,
            "pr": int(prv[0]) if prv else 0,
        }
    cache = {}

    def world(pid):
        if pid in cache:
            return cache[pid]
        it = local[pid]
        x = float(it["pos"][frame].get("x") or 0)
        y = float(it["pos"][frame].get("y") or 0)
        s = float(it["scl"][frame].get("x") or 1)
        o = float(it["opa"][frame] if frame < len(it["opa"]) else 1)
        o *= (it["vis"][frame] if frame < len(it["vis"]) else 1)
        par = parent_of.get(pid, -1)
        if par in local and par != pid:
            px, py, ps, po = world(par)
            x, y = x * ps + px, y * ps + py
            s *= ps
            o *= po
        cache[pid] = (x, y, s, o)
        return cache[pid]

    out = []
    for pid in sorted(local, key=lambda i: (local[i]["pr"], i)):
        it = local[pid]
        if not it["cell"]:
            continue
        wx, wy, ws, wo = world(pid)
        out.append((it["cell"], wx, wy, ws, wo))
    return out


def render(unit: Unit, tree: dict, anim: dict, ratio: float, scale: float = SCALE):
    """合成一帧 → 裁掉透明边 → 返回 ``PIL.Image``（失败返回 None）。"""
    total = int(anim.get("CountFrame") or 0)
    if total <= 0:
        return None
    frame = max(0, min(int(total * ratio), total - 1))
    W = int(760 * scale)
    canvas = _render_canvas(unit, tree, anim, frame, total, W)
    if canvas is None:
        return None
    bb = canvas.getbbox()
    if not bb:
        return None
    pad = int(4 * scale)
    bb = (max(0, bb[0] - pad), max(0, bb[1] - pad),
          min(W, bb[2] + pad), min(W, bb[3] + pad))
    return canvas.crop(bb)


# ---------------------------------------------------------------- 导出单元
def export_unit(num, idx: dict, force: bool = False):
    """导出 5 位编号 ``num`` 的 SD。返回 ``{id, k, poses:[...]}`` 或 None。"""
    num = str(num).zfill(5)
    name = f"bundle_unit_{num}_bc"
    ent = idx.get(name)
    if not ent:
        _log(f"  ! 清单里没有 {name}")
        return None
    path = os.path.join(SRC, name)
    if not os.path.exists(path) or os.path.getsize(path) != int(ent.get("size") or 0):
        _log(f"  ! 包不在本地：{name}（应 {ent.get('size')} 字节）")
        return None
    os.makedirs(OUT_IMG, exist_ok=True)
    # 旧格式（静态 4 张 / 拿错图集）的条目直接重做，不用等用户手动清目录
    try:
        old = {u.get("id"): u for u in (load_sdmap().get("units") or [])}.get(int(num))
        if old is not None and int(old.get("sv") or 1) < SV:
            force = True
    except Exception:                                            # noqa: BLE001
        pass
    try:
        unit = Unit(path)
    except Exception as e:                                       # noqa: BLE001
        _log(f"  ! {name} 解析失败：{str(e)[:100]}")
        return None
    if unit.atlas is None or not unit.cells:
        _log(f"  ! {name} 没有图集/图集格（已登记为导不出 SD）")
        _set_unavailable(int(num), True)
        return None
    _tname, tree = unit.body_tree(num)
    if tree is None:
        _log(f"  ! {name} 没有可合成的待机动作（已登记为导不出 SD）")
        _set_unavailable(int(num), True)
        return None
    poses = []
    # 六个动作一律导出成**循环动画 WebP**，顺序 = 页面卡片/灯箱里的顺序：
    # stand(待机) / move(出击) / attack1(攻击1) / attack2(攻击2) /
    # victory(胜利) / damage(受击)。
    # 文件名 = <编号><后缀>.webp，poses 每项带帧数，前端据此显示「· 动态」标签。
    #
    # 统一取景：六个动作必须共用**同一个纵向范围**，否则灯箱一行平铺时人物会
    # 上下跳、大小还不一样。横向则各自贴边裁（box_x 按本动作自己的包围盒算）——
    # 只用一个"六动作并集"当框的话，长鞭/长枪那个动作会把框撑得极大，另外五张
    # 全变成小人（408 待机 206px 站在 455px 的框里，00032 更极端到 737px）。
    # 只共用 y ⇒ 站着/跳起/下蹲的高低关系还在，横向又不留大片空白。
    W = int(760 * SCALE)
    plan = []
    # 2026-11-16：改为**枚举包内全部动画**（原名即键名），不再按 ANIM_POSES 只取 6 个。
    clips = all_clips(tree)
    anims = {a.get("Name"): a for a in (tree.get("TableAnimation") or [])}
    for clip_name in clips:
        anim = anims.get(clip_name)
        if anim is None:
            continue
        total = int(anim.get("CountFrame") or 0)
        if total <= 0:
            continue
        step = clip_step(clip_name)
        # 文件名/键名安全化。待机**统一叫 stand**：包内原名五花八门
        # （mot_stand / stand_mot / m_stand / stand_1 / F_stand，134 个单位都
        # 不是干净的 "stand"），照原名写会让页面卡片上显示成 mot_stand，
        # 文件名也五花八门。当前只上线待机，所以直接归一。
        if "stand" in clip_name.lower():
            suffix = "stand"
        else:
            suffix = re.sub(r"[^A-Za-z0-9_-]", "_", clip_name)
        frames = list(range(0, total, max(1, step)))
        plan.append((suffix, step, anim, frames))
    if not plan:                     # 极端兜底：动画表读不出名字时退回旧候选词
        for suffix, word, step in ANIM_POSES:
            anim = finding_pose(tree, word)
            if anim is None:
                continue
            total = int(anim.get("CountFrame") or 0)
            if total <= 0:
                continue
            frames = list(range(0, total, max(1, step)))
            plan.append((suffix, step, anim, frames))
    y0 = y1 = None
    xr = {}                                # suffix -> (x0, x1) 本动作自己的横向范围
    for suffix, step, anim, frs in plan:
        xs0 = xs1 = None
        for f in frs:
            bb = _frame_box(unit, tree, anim, f, int(anim["CountFrame"]), W)
            if not bb:
                continue
            y0 = bb[1] if (y0 is None or bb[1] < y0) else y0
            y1 = bb[3] if (y1 is None or bb[3] > y1) else y1
            xs0 = bb[0] if (xs0 is None or bb[0] < xs0) else xs0
            xs1 = bb[2] if (xs1 is None or bb[2] > xs1) else xs1
        if xs0 is not None:
            xr[suffix] = (xs0, xs1)
    if y0 is None:
        _log(f"  ! {name} 一张都没合成出来（已登记为导不出 SD）")
        _set_unavailable(int(num), True)
        return None
    pad = 6
    pad_y = (max(0, int(math.floor(y0)) - pad), min(W, int(math.ceil(y1)) + pad))

    for suffix, step, anim, frs in plan:
        x0, x1 = xr.get(suffix, (0, W))
        box = (max(0, int(math.floor(x0)) - pad), pad_y[0],
               min(W, int(math.ceil(x1)) + pad), pad_y[1])
        out_name = f"{int(num)}{suffix}.webp"
        out_path = os.path.join(OUT_IMG, out_name)
        rel = f"media/img/sd/{out_name}"
        if os.path.exists(out_path) and not force:
            try:
                with Image.open(out_path) as im:
                    n = int(getattr(im, "n_frames", 1) or 1)
                    w0, h0 = im.size
                if n > 1:
                    poses.append([suffix, rel, w0, h0, n])
                    continue
            except Exception:                                    # noqa: BLE001
                pass
        try:
            frames = render_frames_box(unit, tree, anim, box, frs, W)
        except Exception as e:                                   # noqa: BLE001
            _log(f"  ! {name} 合成 {suffix} 失败：{str(e)[:100]}")
            frames = []
        if not frames:
            continue
        # 按角色整体镜像（见 FLIP_CHARS）
        if int(num) in FLIP_CHARS:
            frames = [im.transpose(Image.Transpose.FLIP_LEFT_RIGHT) for im in frames]

        # SV17：播放速度回到数据帧率。SV15 曾按旧测量改成 80ms/帧（2.4 倍慢），
        # 那是错测量：用 6 段动作实拍复测，待机在 lag=41 帧处 MSE=0（精确重复），
        # 即 41/30 = 1.37s，而 stand 是 40 帧 = 1.33s —— 数据帧率与游戏一致。
        fps = int(anim.get("FramePerSecond") or 30) or 30
        dur = max(20, int(round(1000 / fps))) * step
        try:
            frames[0].save(out_path, "WEBP", save_all=True,
                           append_images=frames[1:], duration=dur,
                           loop=0, quality=ANIM_Q, method=WEBP_METHOD)
            # 内容完全相同的帧会被 WebP 编码器合并（静止的 ready 只需 1 帧），
            # 所以读回真实帧数再写 poses，别让界面挂着「· 动态」却不动。
            try:
                with Image.open(out_path) as chk:
                    nf = int(getattr(chk, "n_frames", 1) or 1)
            except Exception:                                    # noqa: BLE001
                nf = len(frames)
            row = [suffix, rel, frames[0].width, frames[0].height]
            if nf > 1:
                row.append(nf)
            poses.append(row)
        except Exception as e:                                   # noqa: BLE001
            _log(f"  ! {name} 写 {suffix} 动画失败（退回静态）：{str(e)[:80]}")
            try:
                frames[0].save(out_path, "WEBP", quality=90, method=WEBP_METHOD)
                poses.append([suffix, rel, frames[0].width, frames[0].height])
            except Exception:                                    # noqa: BLE001
                pass
    if not poses:
        _log(f"  ! {name} 一张都没合成出来（已登记为导不出 SD）")
        _set_unavailable(int(num), True)
        return None
    # k: 角色 / NPC(1xxxx) / 敌(2xxxx) —— 画廊按它分三个范围。
    # 以前 1xxxx 和 2xxxx 都记成 "npc"，两类混在一格里就分不清了。
    v = int(num)
    kind = "char" if v < 10000 else ("npc" if v < 20000 else "enemy")
    _set_unavailable(v, False)     # 后来又能导出了 → 撤销"官方无 SD"登记
    # sv = 导出格式版本（见文件头 SV）：老条目下次跑就会自动重做
    return {"id": v, "k": kind, "sv": SV, "poses": poses}


# ---------------------------------------------------------------- sdmap.js
def load_sdmap() -> dict:
    if not os.path.exists(OUT_JS):
        return {"v": 1, "units": []}
    try:
        with open(OUT_JS, encoding="utf-8-sig") as f:
            s = f.read()
        i = s.index("{")
        return json.loads(s[i:s.rindex("}") + 1])
    except Exception:                                            # noqa: BLE001
        return {"v": 1, "units": []}


def save_sdmap(data: dict) -> None:
    """**合并写回**：只增改本次导出的编号，别的条目原样保留。

    本机 ``sdmap.js`` 里可能有出厂包没有的条目（用户自己补的），整份重写会把
    它们删掉（``sync_advindex`` / ``regen_names`` 都踩过这个坑）。
    """
    os.makedirs(os.path.dirname(OUT_JS), exist_ok=True)
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    tmp = OUT_JS + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("window.__SDMAP=" + body + ";")
    os.replace(tmp, OUT_JS)


def merge(units: list) -> int:
    data = load_sdmap()
    have = {u.get("id"): u for u in data.get("units") or []}
    for u in units:
        have[u["id"]] = u
    data["v"] = SV   # 版本戳：前端拿它给 .webp 加 ?v=，改渲染口径后能强制换图
    data["units"] = [have[k] for k in sorted(have)]
    save_sdmap(data)
    return len(data["units"])


#: 清单缓存：子进程各自读一次即可（821 个包 / 8 进程，不会重复读）。
_IDX_CACHE = None


def _manifest_cached():
    global _IDX_CACHE
    if _IDX_CACHE is None:
        _IDX_CACHE = manifest_index()
    return _IDX_CACHE


def _export_worker(num, force):
    """``ProcessPoolExecutor`` 的任务体（必须在模块顶层，spawn 才能 pickle）。"""
    return export_unit(num, _manifest_cached(), force=force)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="导出角色 / 敌人 SD 小人")
    ap.add_argument("--chars", default="", help="角色 id，逗号分隔")
    ap.add_argument("--units", default="", help="5 位编号，逗号分隔（敌人/NPC 用）")
    ap.add_argument("--npc", action="store_true", help="清单里全部 NPC / 敌人")
    ap.add_argument("--all", action="store_true",
                    help="清单里全部编号（角色 + NPC + 敌人），配合 --force 可整体重做")
    ap.add_argument("--force", action="store_true", help="重做已存在的图")
    ap.add_argument("--jobs", type=int, default=0,
                    help="并行进程数（默认按 CPU 核数，最多 12；1 = 单进程）")
    ap.add_argument("--tidy", type=int, nargs="?", const=400, default=0,
                    help="清理已不再引用的动作图（可跟一个每批上限，默认 400）")
    ap.add_argument("--nearest", action="store_true",
                    help="纹理采样用最近邻：栅格化快约 1/3，像素画更锐；"
                         "默认仍是官方口径的双线性，观感要不要换自己 A/B 定")
    ap.add_argument("--catalog", action="store_true",
                    help="只登记「清单里有哪些编号的 SD」，不下载也不合成")
    a = ap.parse_args(argv)

    idx = manifest_index()
    if a.catalog:
        # 全量目录：让画廊页能把"还没装的角色"也列出来（灰色占位），
        # 而不是只显示已经装过的那几个。poses 留空 = 没有图。
        data = load_sdmap()
        have = {u.get("id"): u for u in data.get("units") or []}
        n = 0
        for name, ent in idx.items():
            m = re.match(r"^bundle_unit_(\d+)_bc$", name)
            if not m:
                continue
            num = int(m.group(1))
            if num in have and have[num].get("poses"):
                continue
            have[num] = {"id": num,
                         "k": ("char" if num < 10000
                               else ("npc" if num < 20000 else "enemy")),
                         "poses": [],
                         "bytes": int(ent.get("size") or 0)}
            n += 1
        data["v"] = SV   # 版本戳：前端拿它给 .webp 加 ?v=，改渲染口径后能强制换图
        data["units"] = [have[k] for k in sorted(have)]
        save_sdmap(data)
        _log(f"目录登记 {n} 个新编号，共 {len(data['units'])} 个")
        return 0

    nums: list[str] = []
    for part in (a.chars or "").replace(" ", "").split(","):
        if part.isdigit():
            nums.append(part.zfill(5))
    for part in (a.units or "").replace(" ", "").split(","):
        if part.isdigit():
            nums.append(part.zfill(5))
    if a.npc:
        nums += sorted({m.group(1) for m in
                        (re.match(r"^bundle_unit_(\d+)_", n) for n in idx) if m
                        and int(m.group(1)) >= 10000})
    if a.all:
        nums = sorted({m.group(1) for n in idx
                       for m in [re.match(r"^bundle_unit_(\d+)_bc$", n)] if m})
    nums = list(dict.fromkeys(nums))
    if not nums:
        ap.error("没有指定要导出谁：用 --chars / --units / --npc / --all")

    global SAMPLE_NEAREST
    if a.nearest:
        SAMPLE_NEAREST = True

    _log(f"要导出 {len(nums)} 个 SD 包（动作：{'/'.join(EXPORT_CLIP_NAMES)}"
         f"{'，最近邻采样' if SAMPLE_NEAREST else ''}）")
    done = []
    ok_total = 0
    jobs = int(a.jobs or 0)
    if jobs <= 0:
        # 12 是实测拐点（8845HS / 8 核 16 线程）：16 个角色在 jobs=8/12/16 下
        # 分别 22.7s / 13.7s / 14.1s —— 再多就只剩 IO 争抢和内存换页。
        jobs = min(12, max(1, (os.cpu_count() or 4)))
    jobs = min(jobs, len(nums))

    def _flush(final=False):
        if done:
            merge(done)
            done.clear()

    t_start = time.time()
    if jobs > 1:
        # 逐包并行：821 个包互不依赖（各读各的 bundle、各写各的 webp），
        # 单进程跑约 25 分钟，8 进程降到 3~4 分钟。子进程各自解析 Unit，
        # 结果回主进程统一 merge（sdmap 的合并写只允许主进程做）。
        _log(f"  并行 {jobs} 进程")
        with concurrent.futures.ProcessPoolExecutor(max_workers=jobs) as ex:
            futs = {ex.submit(_export_worker, num, a.force): num for num in nums}
            for i, fut in enumerate(concurrent.futures.as_completed(futs), 1):
                try:
                    r = fut.result()
                except Exception as e:                        # noqa: BLE001
                    _log(f"  ! {futs[fut]} 失败：{str(e)[:120]}")
                    r = None
                if r:
                    done.append(r)
                    ok_total += 1
                if i % 40 == 0 or i == len(nums):
                    _flush()
                    el = time.time() - t_start
                    eta = el / i * (len(nums) - i)
                    _log(f"  [{i}/{len(nums)}] 成功 {ok_total}  已用 {el:.0f}s 预计剩余 {eta / 60:.1f} 分")
        _flush()
    else:
        for i, num in enumerate(nums, 1):
            r = export_unit(num, idx, force=a.force)
            if r:
                done.append(r)
                ok_total += 1
            # 每 40 个先落一次盘：中途刷新页面也能看到已经导好的
            if done and (i % 40 == 0 or i == len(nums)):
                _flush()
            if i % 20 == 0 or i == len(nums):
                _log(f"  [{i}/{len(nums)}] 成功 {ok_total}")
        _flush()
    total = len(load_sdmap().get("units") or [])
    _log(f"sdmap.js 共 {total} 个 SD 单元")
    removed = cleanup_legacy()
    if removed:
        _log(f"清理旧后缀文件 {removed} 个（ready / attack 已不再引用）")
    if a.tidy:
        gone = cleanup_unused(limit=max(0, int(a.tidy)))
        if gone:
            _log(f"清理未引用的动作图 {gone} 个（当前只保留 {'/'.join(EXPORT_CLIP_NAMES)}）")
    return 0


def cleanup_unused(limit: int = 0) -> int:
    """删掉 ``<编号><后缀>.webp`` 里**已经不在 poses 中**的那些（``--tidy`` 才跑）。

    只导 ``stand`` 之后，以前导出的 move / attack_1_1 / attack_2_1 / damage /
    victory_1 / special_* / action 都成了无人引用的孤儿（821 个角色 × 10 个
    ≈ 0.9 GB）。只清理**已升级到本 SV** 的条目 —— 没导成功的单位可能还在引用
    旧文件，删了就变成破图。

    ``limit`` > 0 时每批最多删这么多个：宿主环境的批量删除保护（一次 500 个
    就要求二次确认）会在超限时中断整个调用，分几次跑更稳。删完提示还有多少。
    """
    if not os.path.isdir(OUT_IMG):
        return 0
    removed = 0
    left = 0
    for u in (load_sdmap().get("units") or []):
        if int(u.get("sv") or 1) < SV:
            continue
        uid = int(u.get("id") or 0)
        if not uid:
            continue
        keep = {os.path.basename(str(p[1])) for p in (u.get("poses") or [])
                if p and len(p) > 1}
        pat = re.compile(r"^%d(?!\d)" % uid)
        for fn in os.listdir(OUT_IMG):
            if not fn.lower().endswith(".webp") or fn in keep or not pat.match(fn):
                continue
            if limit and removed >= limit:
                left += 1
                continue
            try:
                os.remove(os.path.join(OUT_IMG, fn))
                removed += 1
            except OSError:
                left += 1
    if left:
        _log(f"  还有 {left} 个孤儿图未删（可用 --tidy 再跑一次）")
    return removed


def cleanup_legacy() -> int:
    """删掉**已经升到 SV 且新 poses 里没有**的旧后缀图（ready / attack）。

    ``ready``（構え）与 ``attack``（只到攻击1）在新方案里被 move / attack1 /
    attack2 取代。只删条目已经更新过的单位 —— 没导成功的单位还在引用旧文件，
    删了就变成破图。
    """
    removed = 0
    data = load_sdmap()
    for u in data.get("units") or []:
        if int(u.get("sv") or 1) < SV:
            continue
        have = {p[0] for p in (u.get("poses") or []) if p}
        for suf in LEGACY_SUFFIX:
            if suf in have:
                continue
            p = os.path.join(OUT_IMG, f"{int(u.get('id'))}{suf}.webp")
            if os.path.exists(p):
                try:
                    os.remove(p)
                    removed += 1
                except OSError:
                    pass
    return removed


if __name__ == "__main__":
    raise SystemExit(main())

