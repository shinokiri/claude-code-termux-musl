#!/usr/bin/env python3
"""Add a runtime dependency in the existing ELF header gap without shifting Bun.

patchelf's general DT_NEEDED rewrite changes this large ET_EXEC's load layout.
After its interpreter-only patch there is a separate writable header LOAD with
room for a copy of .dynstr/.dynamic. Keep every original program byte at its
file offset and virtual address; update only the loader metadata and sections.
Unsupported ELF layouts are rejected before any bytes are changed.
"""
import argparse
import mmap
from pathlib import Path
import struct

EHDR = struct.Struct('<16sHHIQQQIHHHHHH')
PHDR = struct.Struct('<IIQQQQQQ')
SHDR = struct.Struct('<IIQQQQIIQQ')
DYN = struct.Struct('<qQ')
PT_LOAD, PT_DYNAMIC = 1, 2
DT_NULL, DT_NEEDED, DT_STRTAB, DT_STRSZ = 0, 1, 5, 10


def add_dependency(binary, library):
    name = str(library).encode() + b'\0'
    if not library.is_absolute() or len(name) > 4096 or b'\0' in name[:-1]:
        raise RuntimeError('Compatibility library must have an absolute path under 4096 bytes')
    with binary.open('r+b') as stream, mmap.mmap(stream.fileno(), 0) as data:
        if len(data) < EHDR.size:
            raise RuntimeError('Truncated ELF header')
        header = EHDR.unpack_from(data)
        if header[0][:7] != b'\x7fELF\x02\x01\x01' or header[1:3] != (2, 183):
            raise RuntimeError('Expected an AArch64 little-endian ELF64 ET_EXEC')
        phoff, shoff = header[5:7]
        ehsize, phsize, phnum, shsize, shnum, shstrndx = header[8:14]
        if (ehsize, phsize, shsize) != (EHDR.size, PHDR.size, SHDR.size) or not phnum or not shnum:
            raise RuntimeError('Unsupported ELF table format')
        if phoff + phnum * phsize > len(data) or shoff + shnum * shsize > len(data) or shstrndx >= shnum:
            raise RuntimeError('ELF tables are outside the file')
        programs = [list(PHDR.unpack_from(data, phoff + i * phsize)) for i in range(phnum)]
        sections = [list(SHDR.unpack_from(data, shoff + i * shsize)) for i in range(shnum)]
        string_header = sections[shstrndx]
        if string_header[4] + string_header[5] > len(data):
            raise RuntimeError('ELF section string table is outside the file')
        section_names = bytes(data[string_header[4]:string_header[4] + string_header[5]])
        by_name = {}
        for index, section in enumerate(sections):
            end = section_names.find(b'\0', section[0])
            if end < 0:
                raise RuntimeError('Invalid section name')
            by_name[section_names[section[0]:end]] = index
        if b'.dynamic' not in by_name or b'.dynstr' not in by_name:
            raise RuntimeError('Required dynamic sections are missing')
        dynamic_index, strings_index = by_name[b'.dynamic'], by_name[b'.dynstr']
        dynamic, strings = sections[dynamic_index], sections[strings_index]
        if dynamic[5] % DYN.size or dynamic[4] + dynamic[5] > len(data) or strings[4] + strings[5] > len(data):
            raise RuntimeError('Invalid dynamic section extent')
        entries = []
        terminated = False
        for offset in range(dynamic[4], dynamic[4] + dynamic[5], DYN.size):
            tag, value = DYN.unpack_from(data, offset)
            if tag == DT_NULL:
                terminated = True
                break
            entries.append([tag, value])
        if not terminated or sum(t == DT_STRTAB for t, _ in entries) != 1 or sum(t == DT_STRSZ for t, _ in entries) != 1:
            raise RuntimeError('Invalid dynamic table')
        old_strings = bytes(data[strings[4]:strings[4] + strings[5]])
        for tag, offset in entries:
            if tag == DT_NEEDED and old_strings[offset:offset + len(name)] == name:
                return False
        if next(value for tag, value in entries if tag == DT_STRTAB) != strings[3]:
            raise RuntimeError('Dynamic string section and table disagree')
        if next(value for tag, value in entries if tag == DT_STRSZ) != len(old_strings):
            raise RuntimeError('Dynamic string table size disagrees')
        loads = sorted((i, p) for i, p in enumerate(programs) if p[0] == PT_LOAD)
        if not loads:
            raise RuntimeError('ELF has no LOAD segments')
        first_index, first = min(loads, key=lambda item: item[1][2])
        later = [p for _, p in loads if p[2] > first[2]]
        dynamic_programs = [i for i, p in enumerate(programs) if p[0] == PT_DYNAMIC]
        if first[2] != 0 or first[1] & 3 != 2 or first[5] != first[6] or not later or len(dynamic_programs) != 1:
            raise RuntimeError('Expected a separate writable non-executable header LOAD')
        alignment = first[7]
        if alignment < 4096 or alignment & (alignment - 1):
            raise RuntimeError('Unsupported LOAD alignment')
        cursor = (max(first[5], ehsize, phoff + phnum * phsize) + 15) & ~15
        limit = min(min(p[2] // alignment * alignment, (p[3] - first[3]) // alignment * alignment) for p in later)
        strings_offset = cursor
        new_strings = old_strings + name
        dynamic_offset = (strings_offset + len(new_strings) + 15) & ~15
        new_entries = [[DT_NEEDED, len(old_strings)], *entries, [DT_NULL, 0]]
        for entry in new_entries:
            if entry[0] == DT_STRTAB:
                entry[1] = first[3] + strings_offset
            elif entry[0] == DT_STRSZ:
                entry[1] = len(new_strings)
        new_dynamic = b''.join(DYN.pack(*entry) for entry in new_entries)
        end = dynamic_offset + len(new_dynamic)
        if end > limit or end > len(data) or any(data[cursor:end]):
            raise RuntimeError('No safe zero-filled header gap for the dependency metadata')
        if shoff < end and shoff + shnum * shsize > cursor:
            raise RuntimeError('ELF section table overlaps the header gap')
        for section in sections:
            if section[1] != 8 and section[5] and section[4] < end and section[4] + section[5] > cursor:
                raise RuntimeError('ELF section overlaps the header gap')
        # All layout and bounds checks finish before the first write.
        data[strings_offset:strings_offset + len(new_strings)] = new_strings
        data[dynamic_offset:end] = new_dynamic
        first[5] = first[6] = end
        PHDR.pack_into(data, phoff + first_index * phsize, *first)
        dyn_program = programs[dynamic_programs[0]]
        dyn_program[2:7] = [dynamic_offset, first[3] + dynamic_offset, first[3] + dynamic_offset,
                            len(new_dynamic), len(new_dynamic)]
        PHDR.pack_into(data, phoff + dynamic_programs[0] * phsize, *dyn_program)
        strings[3:6] = [first[3] + strings_offset, strings_offset, len(new_strings)]
        dynamic[3:6] = [first[3] + dynamic_offset, dynamic_offset, len(new_dynamic)]
        SHDR.pack_into(data, shoff + strings_index * shsize, *strings)
        SHDR.pack_into(data, shoff + dynamic_index * shsize, *dynamic)
        data.flush()
        return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    parser.add_argument('library', type=Path)
    arguments = parser.parse_args()
    print('Dependency added' if add_dependency(arguments.binary, arguments.library) else 'Dependency already present')
