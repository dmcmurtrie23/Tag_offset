"""
Tag Offset Tool
---------------
Upload a Sportscode/Focus package JSON, set a time offset per player,
and download a new JSON with those players' tags shifted.

Only tag-start, tag-end and tag-position are changed. Everything else in the
file (other tags, quarter timings, formatting) is left exactly as it was.

Run:  streamlit run tag_offset_app.py
"""

import difflib
import io
import json
import re
from collections import Counter

import pandas as pd
import streamlit as st

TIME_KEYS = ("tag-start", "tag-end", "tag-position")
PLAYER_RE = re.compile(r"^[1-9]\d{0,2}\s+[A-Za-z]")  # Catapult player tags look like "1 Davis"

TIME_LINE_RE = re.compile(r'^(\s*"tag-(?:start|end|position)"\s*:\s*)(-?\d+)(\s*,?\s*)$')
NAME_LINE_RE = re.compile(r'^\s*"tag-name"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,?\s*$')


# ---------------------------------------------------------------- core logic
def shift_text(text: str, offsets_ms: dict) -> tuple[str, Counter]:
    """Shift times line-by-line so the file's original layout is kept.

    Works on the pretty-printed layout Sportscode/Focus exports, where each tag
    is its own {...} block and each field is on its own line.
    """
    lines = text.split("\n")
    out, buf, depth_open = [], None, None
    counts = Counter()

    in_tags = False
    for ln in lines:
        stripped = ln.strip()
        if not in_tags:
            out.append(ln)
            if stripped.startswith('"tags"'):
                in_tags = True
            continue

        if buf is None:
            if stripped == "{":
                buf, depth_open = [ln], ln[: len(ln) - len(ln.lstrip())]
            else:
                out.append(ln)
                if stripped.startswith("]"):
                    in_tags = False
            continue

        buf.append(ln)
        indent = ln[: len(ln) - len(ln.lstrip())]
        if stripped in ("}", "},") and indent == depth_open:
            name = None
            for b in buf:
                m = NAME_LINE_RE.match(b)
                if m:
                    name = json.loads(f'"{m.group(1)}"')
                    break
            off = offsets_ms.get(name, 0)
            if off:
                counts[name] += 1
                buf = [
                    TIME_LINE_RE.sub(lambda m: f"{m.group(1)}{int(m.group(2)) + off}{m.group(3)}", b)
                    for b in buf
                ]
            elif name in offsets_ms:
                counts[name] += 1
            out.extend(buf)
            buf = None
    if buf:
        out.extend(buf)
    return "\n".join(out), counts


def shift_json(data: dict, offsets_ms: dict) -> tuple[str, Counter]:
    """Fallback: shift via the parsed JSON (layout is re-written)."""
    counts = Counter()
    for t in data.get("tags", []):
        off = offsets_ms.get(t.get("tag-name"))
        if off is None:
            continue
        counts[t["tag-name"]] += 1
        for k in TIME_KEYS:
            if isinstance(t.get(k), int):
                t[k] += off
    return json.dumps(data, indent="\t", separators=(",", " : "), ensure_ascii=False), counts


def verify(original: dict, new_text: str, offsets_ms: dict) -> str | None:
    """Return None if the output is correct, otherwise a description of the problem."""
    try:
        new = json.loads(new_text)
    except json.JSONDecodeError as e:
        return f"Output is not valid JSON ({e})."
    a, b = original.get("tags", []), new.get("tags", [])
    if len(a) != len(b):
        return "Tag count changed."
    for x, y in zip(a, b):
        off = offsets_ms.get(x.get("tag-name"), 0)
        if x.keys() != y.keys():
            return f"Fields changed on a '{x.get('tag-name')}' tag."
        for k in x:
            want = x[k] + off if (k in TIME_KEYS and isinstance(x[k], int)) else x[k]
            if y[k] != want:
                return f"Unexpected value for {k} on a '{x.get('tag-name')}' tag."
    for k in original:
        if k != "tags" and original[k] != new.get(k):
            return f"Section '{k}' changed."
    return None


def parse_pasted_table(text: str, names: list[str]) -> tuple[dict, list[str]]:
    """Read 'Athlete  Offset' rows (tab, comma or multi-space separated).

    Extra columns (e.g. Game) are ignored: the athlete is the first column that
    matches a tag name, the offset is the last number on the row.
    Returns {tag_name: seconds} and a list of notes for the user.
    """
    result, notes = {}, []
    for raw in text.strip().splitlines():
        parts = [p.strip() for p in re.split(r"\t|,|\s{2,}", raw.strip()) if p.strip()]
        if len(parts) < 2:
            continue
        try:
            secs = float(parts[-1])
        except ValueError:
            continue  # header row
        athlete = None
        for p in parts[:-1]:
            if p in names:
                athlete = p
                break
        if athlete is None:
            candidate = parts[-2]
            close = difflib.get_close_matches(candidate, names, n=1, cutoff=0.6)
            if close:
                athlete = close[0]
                notes.append(f"Matched “{candidate}” to “{athlete}”.")
            else:
                notes.append(f"No tag found for “{candidate}”; skipped.")
                continue
        result[athlete] = secs
    return result, notes


# ---------------------------------------------------------------- UI
st.set_page_config(page_title="Tag Offset Tool", layout="centered")
st.title("Tag Offset Tool")
st.caption("Shift each player's Catapult tags in a Sportscode/Focus package JSON.")

up = st.file_uploader("Package JSON", type=["json"])
if not up:
    st.info("Upload a package JSON (e.g. `25_Package.json`) to start.")
    st.stop()

raw_text = up.getvalue().decode("utf-8-sig")
try:
    data = json.loads(raw_text)
except json.JSONDecodeError as e:
    st.error(f"That file isn't valid JSON: {e}")
    st.stop()

tags = data.get("tags", [])
if not tags:
    st.error("No `tags` found in this file. Is it a package export?")
    st.stop()

sess = data.get("session", {})
end_ms = sess.get("session-end_time")
st.write(
    f"**{len(tags):,} tags** · session length "
    f"{end_ms/60000:.1f} min" if isinstance(end_ms, int) else f"**{len(tags):,} tags**"
)

name_counts = Counter(t.get("tag-name", "") for t in tags)
show_all = st.toggle("Show all tag names (not just Catapult players)", value=False)
names = sorted(
    (n for n in name_counts if show_all or PLAYER_RE.match(n)),
    key=lambda n: (int(n.split()[0]) if n.split()[0].isdigit() else 999, n),
)

# ---- paste a table
with st.expander("Paste an offset table (optional)"):
    st.caption("Columns like `Game  Athlete  Time Offset (ss.ms)`. Copy straight from Excel.")
    pasted = st.text_area("Table", height=180, key="pasted", label_visibility="collapsed")
    pasted_offsets, notes = parse_pasted_table(pasted, list(name_counts)) if pasted.strip() else ({}, [])
    for n in notes:
        st.write("• " + n)

direction = st.radio(
    "Direction",
    ["Later (add time)", "Earlier (subtract time)"],
    horizontal=True,
)
sign = 1 if direction.startswith("Later") else -1

table = pd.DataFrame(
    {
        "Player": names,
        "Tags": [name_counts[n] for n in names],
        "Offset (s)": [float(pasted_offsets.get(n, 0.0)) for n in names],
    }
)
edited = st.data_editor(
    table,
    hide_index=True,
    disabled=["Player", "Tags"],
    column_config={"Offset (s)": st.column_config.NumberColumn(step=0.1, format="%.3f")},
    key=f"editor_{hash(pasted)}_{show_all}",
)

offsets_ms = {
    row["Player"]: sign * int(round(float(row["Offset (s)"]) * 1000))
    for _, row in edited.iterrows()
    if pd.notna(row["Offset (s)"]) and float(row["Offset (s)"]) != 0
}
missing = [n for n in names if n not in offsets_ms and PLAYER_RE.match(n)]
if missing:
    st.info("Offset is 0 for: " + ", ".join(missing) + ". Their tags stay where they are.")

if not offsets_ms:
    st.stop()

if st.button("Apply offsets", type="primary"):
    new_text, counts = shift_text(raw_text, offsets_ms)
    problem = verify(data, new_text, offsets_ms)
    if problem:
        # Layout wasn't the expected one; rewrite via parsed JSON instead.
        new_text, counts = shift_json(json.loads(raw_text), offsets_ms)
        problem = verify(data, new_text, offsets_ms)
    if problem:
        st.error(f"Check failed, no file produced: {problem}")
        st.stop()

    summary = pd.DataFrame(
        [
            {"Player": n, "Offset (ms)": f"{o:+d}", "Tags moved": counts.get(n, 0)}
            for n, o in offsets_ms.items()
        ]
    )
    st.success(f"Moved {sum(counts.values()):,} tags. Checked: only start/end/position times changed.")
    st.dataframe(summary, hide_index=True)

    out_name = re.sub(r"\.json$", "", up.name, flags=re.I) + "_offset.json"
    st.download_button(
        "Download " + out_name,
        data=new_text.encode("utf-8"),
        file_name=out_name,
        mime="application/json",
    )
