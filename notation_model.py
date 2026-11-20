"""A lossless mapping from visual notes back to locations in score strings."""
from dataclasses import dataclass
import re

from midi_preview import phrase_attacks, pitch_number
from score_editor import (NOTE_FORM, REST_SIZES, SIZES, SOUND_ATOM, SPECIAL_SOUNDS,
                          Token, inspect_score, parse_score, replace_fragment)


@dataclass(frozen=True)
class Cell:
    top: int
    child: int | None
    tick: int
    token: Token
    barline: bool = False

    @property
    def form(self):
        return NOTE_FORM.fullmatch(self.token.text)

    @property
    def atoms(self):
        match = self.form
        return list(re.finditer(SOUND_ATOM, match[1])) if match else []

    @property
    def lengths(self):
        match = self.form
        return match[2] if match else length_letters(self.token.ticks) if self.token.ticks else ""


def visual_cells(line):
    cells, tick, previous_end = [], 0, 0
    for top, token in enumerate(inspect_score(line)[0]):
        barline = ";" in line[previous_end:token.start]
        if token.children:
            inner = token.text.partition("<")[2]
            end = 0
            for child, part in enumerate(token.children):
                cells.append(Cell(top, child, tick, part,
                                  barline if child == 0 else ";" in inner[end:part.start]))
                tick += part.ticks
                end = part.end
        else:
            cells.append(Cell(top, None, tick, token, barline))
            tick += token.ticks
        previous_end = token.end
    return cells


@dataclass(frozen=True)
class VisualAttack:
    tick: float
    index: int
    atom: int
    group: str
    connector: str
    group_count: int


def visual_attacks(cells, bpm):
    tick_seconds = 60 / bpm / 32
    attacks = []
    for index, cell in enumerate(cells):
        if not cell.form:
            attacks.append(VisualAttack(cell.tick, index, 0, "", "", 1))
            continue
        groups, connectors, onsets = phrase_attacks(cell.form[1], cell.token.ticks * tick_seconds)
        first_atom = 0
        for j, (group, at) in enumerate(zip(groups, onsets)):
            attacks.append(VisualAttack(round(cell.tick + at / tick_seconds, 7), index,
                                        first_atom, group, connectors[j - 1] if j else "", len(groups)))
            first_atom += len(group.split("."))
    return attacks


def length_letters(ticks, alphabet=SIZES):
    letters = []
    for letter, size in alphabet.items():
        count, ticks = divmod(ticks, size)
        letters.append(letter * count)
    return "".join(letters)


def signature_histogram(section):
    histogram = [0.0] * 12
    for line in section["scores"]:
        for cell in visual_cells(line):
            form = cell.form
            if not form:
                continue
            groups = re.split(r"[~@]", form[1].strip("()"))
            weight = cell.token.ticks / len(groups)
            for atom in cell.atoms:
                if atom[0] not in SPECIAL_SOUNDS:
                    histogram[pitch_number(atom[0], piano_range=False) % 12] += weight
    return histogram


def edit_cell(data, section, layer, cells, index, atom_index, sound, lengths, borrow=1):
    """Replace one sound and optionally trade time with a neighbor."""
    cell = cells[index]
    line = data["musics"][section]["scores"][layer]
    top_tokens = parse_score(line)
    original_top = top_tokens[cell.top]
    form = cell.form
    if not lengths or any(letter not in SIZES for letter in lengths):
        raise ValueError("时值请使用 H–P，例如 L 或 MN")
    ticks = sum(SIZES[letter] for letter in lengths)
    if form and not re.fullmatch(SOUND_ATOM, sound):
        raise ValueError("请输入一个谱面音名，例如 #c1、A-1 或 mute")
    if form and sound not in SPECIAL_SOUNDS:
        pitch_number(sound, piano_range=False)

    def rewrite(target, duration, target_sound=None):
        note = target.form
        if not note:
            return length_letters(sum(SIZES[c] for c in duration), REST_SIZES)
        phrase = note[1]
        if target_sound is not None:
            atom = target.atoms[atom_index]
            phrase = phrase[:atom.start()] + target_sound + phrase[atom.end():]
        return f"{phrase}[{duration}]{note[3] or ''}"

    if ticks == cell.token.ticks:
        replacement = rewrite(cell, lengths, sound if form else None) if form else cell.token.text
        changes = {index: replacement}
    else:
        neighbor_index = index + borrow
        if not 0 <= neighbor_index < len(cells):
            raise ValueError("此方向没有相邻音符可补偿时值")
        neighbor = cells[neighbor_index]
        if cells[max(index, neighbor_index)].barline:
            raise ValueError("时值补偿不能跨过小节线")
        if ((cell.child is None) != (neighbor.child is None)
                or cell.child is not None and cell.top != neighbor.top):
            raise ValueError("时值补偿须在同一音符组内，或在两个独立音符之间进行")
        remaining = neighbor.token.ticks + cell.token.ticks - ticks
        if remaining <= 0:
            raise ValueError("相邻音符剩余时值必须大于零")
        changes = {index: rewrite(cell, lengths, sound if form else None),
                   neighbor_index: rewrite(neighbor, length_letters(remaining))}
    if cell.child is not None:
        prefix, _, inside = original_top.text.partition("<")
        body = inside[:-1]
        for i in sorted(changes, reverse=True):
            part = cells[i].token
            body = body[:part.start] + changes[i] + body[part.end:]
        replacement = prefix + "<" + body + ">"
        first = last = cell.top
    else:
        ordered = sorted(changes)
        first, last = cells[ordered[0]].top, cells[ordered[-1]].top
        replacement = ",".join(changes[i] for i in ordered)
    if re.sub(r"\s+", "", line[top_tokens[first].start:top_tokens[last].end]) == re.sub(r"\s+", "", replacement):
        return False
    replace_fragment(data, section, first, last, re.sub(r"\s+", "", replacement), layer=layer)
    return True
