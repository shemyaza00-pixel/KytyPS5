#!/usr/bin/env python3
"""Apply only the Ocarina cutscene frame-sync logic from Lichii PR #251.

Base is EXACT Frizti/Fritchii tag fritchii-v4.1.
No audio changes, profiling counters, SDL changes or other emulator modifications.
"""
import difflib
from pathlib import Path
import sys

if len(sys.argv) != 2:
    raise SystemExit("Usage: python apply_pr251_video_only.py <emulator/source/root>")
root = Path(sys.argv[1])
source = root / "src/core/hle/service/nx2_gsv.h"
before = source.read_text(encoding="utf-8")
modified = before

def patch(old: str, new: str, label: str):
    global modified
    occurrences = modified.count(old)
    if occurrences != 1:
        raise RuntimeError(f"Cannot safely apply {label}: expected 1 match, found {occurrences}")
    modified = modified.replace(old, new, 1)
    print(f"Applied: {label}")

# The only additional helper: a real-time clock used by OoT's video decoder.
# No instrumentation and no audio-core changes.
patch(
    "    static constexpr u32 NoSyncpoint = ~0U;",
    """    // Lichii PR #251, minimal movie-only synchronization for Ocarina of Time.
    static constexpr u64 OcarinaOfTimeMovieProgramId = 0x0400486024DEC000ULL;
    static constexpr auto MovieClockPause = std::chrono::seconds{1};

    bool SyncsToClock() {
        return system.GetApplicationProcessProgramID() == OcarinaOfTimeMovieProgramId;
    }

    bool IsLate(u64 pts) const {
        if (!m_clock_running) {
            return false;
        }
        const auto now = std::chrono::steady_clock::now();
        if (now - m_clock_last_take > MovieClockPause) {
            return false;
        }
        const auto elapsed =
            std::chrono::duration_cast<std::chrono::microseconds>(now - m_clock_start).count();
        const u64 due = m_clock_start_pts + static_cast<u64>((std::max<s64>)(elapsed, 0));
        return pts + 2 * m_frame_us < due;
    }

    void EndMovieClock() {
        m_clock_running = false;
    }

    static constexpr u32 NoSyncpoint = ~0U;""",
    "Ocarina-only decoder clock")

patch(
    "            m_height = w[6];",
    """            m_height = w[6];
            m_frame_us = w[7] != 0 && w[7] <= 240 ? u64{1'000'000} / w[7] : u64{1'000'000} / 60;
            m_clock_running = false;""",
    "read video frame duration")

patch(
    "                m_outputs.pop_front();",
    """                m_outputs.pop_front();
                const auto now = std::chrono::steady_clock::now();
                if (m_clock_running && now - m_clock_last_take > MovieClockPause) {
                    m_clock_start += now - m_clock_last_take;
                }
                m_clock_last_take = now;""",
    "pause clock when cutscene video is not being consumed")

patch(
    "                m_decoded.clear();",
    """                m_decoded.clear();
                EndMovieClock();""",
    "reset movie clock on flush")

patch(
    "                Decoded frame = std::move(m_decoded.front());",
    """                while (m_decoded.size() > 1 && !m_decoded.front().end &&
                       IsLate(m_decoded.front().pts)) {
                    m_decoded.pop_front();
                }
                Decoded frame = std::move(m_decoded.front());""",
    "discard superseded decoded movie frames")

patch(
    """                m_decoded.pop_front();
                const u32 image = m_free.front();""",
    """                m_decoded.pop_front();
                if (!m_clock_running && frame.picture && !frame.end && SyncsToClock()) {
                    m_clock_running = true;
                    m_clock_start = std::chrono::steady_clock::now();
                    m_clock_last_take = m_clock_start;
                    m_clock_start_pts = frame.pts;
                }
                const u32 image = m_free.front();""",
    "start movie clock only for OoT")

patch(
    """                    m_ready->room.store(false, std::memory_order_relaxed);
                }
                system.Host1x().GetSyncpointManager().IncrementGuest(m_syncpoint);""",
    """                    m_ready->room.store(false, std::memory_order_relaxed);
                    EndMovieClock();
                }
                system.Host1x().GetSyncpointManager().IncrementGuest(m_syncpoint);""",
    "reset movie clock at end of stream")

patch(
    """            for (auto& picture : pictures) {
                const u64 pts = picture.pts;
                m_decoded.push_back({std::move(picture), pts, false});
            }""",
    """            for (size_t i = 0; i < pictures.size(); ++i) {
                const u64 pts = pictures[i].pts;
                if (IsLate(pts)) {
                    const bool last = input.end && i + 1 == pictures.size();
                    const bool newer_follows = i + 1 < pictures.size() || !m_inputs.Empty();
                    if (!last && newer_follows) {
                        continue;
                    }
                    while (!m_decoded.empty() && !m_decoded.back().end &&
                           IsLate(m_decoded.back().pts)) {
                        m_decoded.pop_back();
                    }
                }
                m_decoded.push_back({std::move(pictures[i]), pts, false});
            }""",
    "drop late decoded images only when a newer image is available")

patch(
    """    bool m_quit{};
    std::thread m_worker;""",
    """    bool m_quit{};
    u64 m_frame_us{1'000'000 / 60};
    bool m_clock_running{};
    std::chrono::steady_clock::time_point m_clock_start{};
    std::chrono::steady_clock::time_point m_clock_last_take{};
    u64 m_clock_start_pts{};
    std::thread m_worker;""",
    "video-clock state only")

if "Nx2MovieClock" in modified and "Nx2MovieClock" not in before:
    raise RuntimeError("Unexpected diagnostic instrumentation")
if modified == before:
    raise RuntimeError("No change!")
source.write_text(modified, encoding="utf-8")
print("Changed file: src/core/hle/service/nx2_gsv.h")
print("Patch changes:")
print("".join(list(difflib.unified_diff(before.splitlines(True), modified.splitlines(True),
    fromfile="original-v4.1/nx2_gsv.h", tofile="pr251-video-only/nx2_gsv.h"))[:5000]))
