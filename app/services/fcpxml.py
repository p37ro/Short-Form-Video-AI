"""
Final Cut Pro 导出：把 timeline-N.json 剪辑清单转换为 FCPXML 项目。

导入 Final Cut Pro 后得到一条可编辑的时间线：
- 主故事情节：旁白音频
- 连接故事情节（lane 1）：按成片顺序排列的素材片段，保留完整源文件可重新修剪
- 字幕（lane 2）：按 subtitle.srt 生成的 ITT 字幕，可在 FCP 中编辑并在导出时烧录
- 背景音乐（lane -1）：与成片相同的配乐与音量

转场、逐词弹出动画等 MoviePy 渲染效果不会导出，由用户在 FCP 中自行添加。
"""

import json
import math
import os
import re
import subprocess
from fractions import Fraction
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import imageio_ffmpeg
from loguru import logger
from moviepy.video.io.ffmpeg_reader import ffmpeg_parse_infos

from app.services.video import timeline_sidecar_path

FCPXML_VERSION = "1.11"
EVENT_NAME = "Short-Form Video AI"
CAPTION_ROLE = "iTT?captionFormat=ITT.en"
# 字幕按短语分组：逐词字幕在 FCP 中会变成数百个极短的字幕片段，难以编辑。
CAPTION_MAX_WORDS = 3
CAPTION_MAX_GAP = 0.4

# NTSC 帧率需要用精确分数表示，否则 FCP 会提示片段不在帧边界上。
_NTSC_RATES = {
    23.976: Fraction(1001, 24000),
    29.97: Fraction(1001, 30000),
    59.94: Fraction(1001, 60000),
}


def _frame_duration(fps: float) -> Fraction:
    for rate, duration in _NTSC_RATES.items():
        if abs(fps - rate) < 0.01:
            return duration
    return Fraction(1, max(1, round(fps)))


def _t(value: Fraction) -> str:
    value = Fraction(value)
    if value.denominator == 1:
        return f"{value.numerator}s"
    return f"{value.numerator}/{value.denominator}s"


def _floor_to(seconds: float, frame: Fraction) -> Fraction:
    return math.floor(Fraction(seconds) / frame + Fraction(1, 1000)) * frame


def _round_to(seconds: float, frame: Fraction) -> Fraction:
    return round(Fraction(seconds) / frame) * frame


def _read_timecode(path: str) -> str:
    # 相机素材常带 tmcd 时间码轨道，FCP 会把媒体起点放在该时间码上。
    result = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", path],
        capture_output=True,
        text=True,
    )
    match = re.search(r"timecode\s*:\s*(\d+:\d+:\d+[:;.]\d+)", result.stderr)
    return match.group(1) if match else ""


def _timecode_to_seconds(timecode: str, frame: Fraction) -> Fraction:
    """把 HH:MM:SS:FF（丢帧为 HH:MM:SS;FF）转换为秒，结果落在素材帧边界上。"""
    if not timecode:
        return Fraction(0)
    hours, minutes, seconds, frames = (int(x) for x in re.split(r"[:;.]", timecode))
    nominal = round(1 / frame)
    total_frames = ((hours * 60 + minutes) * 60 + seconds) * nominal + frames
    if ";" in timecode and nominal in (30, 60):
        drop = nominal // 15
        total_minutes = hours * 60 + minutes
        total_frames -= drop * (total_minutes - total_minutes // 10)
    return total_frames * frame


def _probe(path: str) -> dict:
    infos = ffmpeg_parse_infos(path)
    return {
        "timecode": _read_timecode(path),
        "duration": float(infos.get("duration") or 0),
        "has_video": bool(infos.get("video_found")),
        "fps": float(infos.get("video_fps") or 30),
        "size": infos.get("video_size") or [0, 0],
        "has_audio": bool(infos.get("audio_found")),
        "audio_rate": int(infos.get("audio_fps") or 48000),
    }


def _hex_to_fcp_color(hex_color: str, default: str = "1 1 1 1") -> str:
    match = re.fullmatch(r"#?([0-9a-fA-F]{6})", (hex_color or "").strip())
    if not match:
        return default
    value = match.group(1)
    r, g, b = (int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return f"{r:.4g} {g:.4g} {b:.4g} 1"


def _parse_srt(path: str) -> list[tuple[float, float, str]]:
    def to_seconds(stamp: str) -> float:
        h, m, rest = stamp.replace(",", ".").split(":")
        return int(h) * 3600 + int(m) * 60 + float(rest)

    with open(path, "r", encoding="utf-8-sig") as f:
        blocks = re.split(r"\n\s*\n", f.read().strip())
    items = []
    for block in blocks:
        lines = block.strip().splitlines()
        for i, line in enumerate(lines):
            if "-->" in line:
                start, end = (part.strip() for part in line.split("-->"))
                text = " ".join(lines[i + 1 :]).strip()
                if text:
                    items.append((to_seconds(start), to_seconds(end), text))
                break
    return items


def _group_captions(items):
    groups = []
    for start, end, text in items:
        if groups:
            g_start, g_end, words = groups[-1]
            ends_sentence = words[-1].endswith((".", "!", "?", "。", "！", "？"))
            if (
                len(words) < CAPTION_MAX_WORDS
                and start - g_end <= CAPTION_MAX_GAP
                and not ends_sentence
            ):
                groups[-1] = (g_start, end, words + [text])
                continue
        groups.append((start, end, [text]))
    return [(start, end, " ".join(words)) for start, end, words in groups]


class _Resources:
    def __init__(self):
        # FCP 导出的文件总是先列出 format 再列出 asset，这里保持相同顺序。
        self._format_lines = []
        self._asset_lines = []
        self._assets = {}
        self._formats = {}
        self._next_id = 1

    def new_id(self) -> str:
        rid = f"r{self._next_id}"
        self._next_id += 1
        return rid

    def format(self, width: int, height: int, frame: Fraction) -> str:
        key = (width, height, frame)
        if key not in self._formats:
            rid = self.new_id()
            self._formats[key] = rid
            self._format_lines.append(
                f'<format id="{rid}" frameDuration="{_t(frame)}" '
                f'width="{width}" height="{height}" colorSpace="1-1-1 (Rec. 709)"/>'
            )
        return self._formats[key]

    def asset(self, path: str) -> tuple[str, dict]:
        path = os.path.abspath(path)
        if path in self._assets:
            return self._assets[path]
        info = _probe(path)
        rid = self.new_id()
        frame = (
            _frame_duration(info["fps"])
            if info["has_video"]
            else Fraction(1, info["audio_rate"])
        )
        info["media_start"] = _timecode_to_seconds(info["timecode"], frame)
        attrs = [
            f'id="{rid}"',
            f"name={quoteattr(Path(path).stem)}",
            f'start="{_t(info["media_start"])}"',
        ]
        if info["has_video"]:
            width, height = info["size"]
            fmt = self.format(width, height, frame)
            attrs += [
                f'duration="{_t(_floor_to(info["duration"], frame))}"',
                'hasVideo="1"',
                f'format="{fmt}"',
                'videoSources="1"',
            ]
        else:
            attrs.append(f'duration="{_t(_floor_to(info["duration"], frame))}"')
        if info["has_audio"]:
            attrs += [
                'hasAudio="1"',
                'audioSources="1"',
                'audioChannels="2"',
                f'audioRate="{info["audio_rate"]}"',
            ]
        info["frame"] = frame
        self._asset_lines.append(
            f"<asset {' '.join(attrs)}>"
            f'<media-rep kind="original-media" src={quoteattr(Path(path).as_uri())}/>'
            "</asset>"
        )
        self._assets[path] = (rid, info)
        return self._assets[path]

    def xml(self) -> str:
        return "\n    ".join(self._format_lines + self._asset_lines)


def build_fcpxml(timeline: dict, project_name: str) -> str:
    fps = timeline.get("fps", 30)
    seq_frame = _frame_duration(fps)
    width, height = timeline["width"], timeline["height"]
    res = _Resources()
    seq_format = res.format(width, height, seq_frame)

    if timeline.get("speed", 1.0) != 1.0:
        logger.warning(
            "clip speed is not 1.0x; the FCPXML export uses normal speed with the same cut points"
        )
    conform = "fill" if timeline.get("fit_mode", "cover") == "cover" else "fit"

    # B-roll：按帧累加偏移，保证片段首尾相接且都落在序列帧边界上。
    broll = []
    offset = Fraction(0)
    for entry in timeline.get("clips", []):
        if not os.path.exists(entry["source"]):
            logger.warning(f"missing source clip, skipped: {entry['source']}")
            continue
        rid, info = res.asset(entry["source"])
        source_start = _floor_to(entry.get("source_start", 0), info["frame"])
        start = info["media_start"] + source_start
        # 不能超出源素材剩余长度，否则 FCP 会报告“没有对应媒体的剪辑”。
        available = _floor_to(info["duration"] - float(source_start), seq_frame)
        duration = min(_round_to(entry["duration"], seq_frame), available)
        if duration <= 0:
            continue
        src_enable = ' srcEnable="video"' if info["has_audio"] else ""
        broll.append(
            f'<asset-clip ref="{rid}" offset="{_t(offset)}" start="{_t(start)}" '
            f'duration="{_t(duration)}" name={quoteattr(Path(entry["source"]).stem)}'
            f"{src_enable}>"
            f'<adjust-conform type="{conform}"/>'
            "</asset-clip>"
        )
        offset += duration
    total = offset

    vo_id, vo_info = res.asset(timeline["audio_file"])
    vo_length = _floor_to(vo_info["duration"], seq_frame)
    total = min(total, vo_length) if total else vo_length

    anchored = []
    if broll:
        anchored.append(
            '<spine lane="1" offset="0s" name="B-roll">\n          '
            + "\n          ".join(broll)
            + "\n        </spine>"
        )

    subtitle_file = timeline.get("subtitle_file") or ""
    if subtitle_file and os.path.exists(subtitle_file):
        style = timeline.get("subtitle_style") or {}
        color = _hex_to_fcp_color(style.get("color", ""))
        previous_end = Fraction(0)
        for n, (start, end, text) in enumerate(
            _group_captions(_parse_srt(subtitle_file)), start=1
        ):
            c_start = max(_round_to(start, seq_frame), previous_end)
            c_end = min(_round_to(end, seq_frame), total)
            if c_end <= c_start:
                continue
            previous_end = c_end
            anchored.append(
                f'<caption lane="2" offset="{_t(c_start)}" duration="{_t(c_end - c_start)}" '
                f"name={quoteattr(text)} role={quoteattr(CAPTION_ROLE)}>"
                f'<text placement="bottom"><text-style ref="ts{n}">{escape(text)}</text-style></text>'
                f'<text-style-def id="ts{n}"><text-style font=".AppleSystemUIFont" '
                f'fontSize="13" fontFace="Regular" fontColor="{color}" '
                f'backgroundColor="0 0 0 1"/></text-style-def>'
                "</caption>"
            )

    bgm = timeline.get("bgm") or {}
    bgm_file = bgm.get("file") or ""
    if bgm_file and os.path.exists(bgm_file):
        bgm_id, bgm_info = res.asset(bgm_file)
        bgm_length = _floor_to(bgm_info["duration"], seq_frame)
        volume = float(bgm.get("volume", 0.2) or 0.2)
        gain_db = 20 * math.log10(volume) if volume > 0 else -96
        position = Fraction(0)
        while bgm_length > 0 and position < total:
            duration = min(bgm_length, total - position)
            anchored.append(
                f'<asset-clip ref="{bgm_id}" lane="-1" offset="{_t(position)}" start="0s" '
                f'duration="{_t(duration)}" name={quoteattr(Path(bgm_file).stem)} '
                f'audioRole="music"><adjust-volume amount="{gain_db:.1f}dB"/></asset-clip>'
            )
            position += duration
            if not bgm.get("loop", True):
                break

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="{FCPXML_VERSION}">
  <resources>
    {res.xml()}
  </resources>
  <event name={quoteattr(EVENT_NAME)}>
    <project name={quoteattr(project_name)}>
      <sequence format="{seq_format}" duration="{_t(total)}" tcStart="0s" tcFormat="NDF" audioLayout="stereo" audioRate="48k">
        <spine>
          <asset-clip ref="{vo_id}" offset="0s" start="0s" duration="{_t(total)}" name="Voiceover" audioRole="dialogue">
        {chr(10).join("        " + item for item in anchored)}
          </asset-clip>
        </spine>
      </sequence>
    </project>
  </event>
</fcpxml>
"""


def export_video_project(combined_video_path: str, project_name: str) -> str:
    """根据 combined-N.mp4 旁的剪辑清单写出 final-N.fcpxml，返回其路径。"""
    sidecar = timeline_sidecar_path(combined_video_path)
    with open(sidecar, "r", encoding="utf-8") as f:
        timeline = json.load(f)
    final_video = timeline.get("final_video") or combined_video_path
    output = os.path.splitext(final_video)[0] + ".fcpxml"
    with open(output, "w", encoding="utf-8") as f:
        f.write(build_fcpxml(timeline, project_name))
    logger.info(f"Final Cut Pro project exported: {output}")
    return output
